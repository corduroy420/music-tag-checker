"""Read audio file tags with mutagen and report which fields are present or missing.

Supports ID3 (MP3/WAV/AIFF), MP4 (M4A/AAC/ALAC), Vorbis comments (FLAC/Ogg/Opus),
APEv2 (APE/WavPack/Musepack) and ASF (WMA).
"""
from __future__ import annotations

import base64
import os
import struct
from dataclasses import dataclass, field

import mutagen
from mutagen.apev2 import APEv2
from mutagen.asf import ASFTags
from mutagen.flac import Picture
from mutagen.id3 import ID3
from mutagen.mp4 import MP4Cover, MP4Tags
from mutagen._vorbis import VCommentDict

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".aac", ".ogg", ".oga", ".opus",
    ".wma", ".wav", ".aif", ".aiff", ".ape", ".wv", ".mpc",
}

ARTWORK = "artwork"

# (key, display label) for every field the app checks, in display order.
FIELDS = [
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("albumartist", "Album Artist"),
    ("genre", "Genre"),
    ("year", "Year"),
    ("track", "Track #"),
    ("disc", "Disc #"),
    ("composer", "Composer"),
    (ARTWORK, "Artwork"),
]
FIELD_LABELS = dict(FIELDS)
TEXT_FIELDS = [key for key, _ in FIELDS if key != ARTWORK]

# Fields that count as "missing" by default (the GUI lets the user change this).
DEFAULT_REQUIRED = {"title", "artist", "album", "genre", "year", "track", ARTWORK}

# Per-format tag keys for each text field.
ID3_FRAMES = {
    "title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
    "genre": "TCON", "year": "TDRC", "track": "TRCK", "disc": "TPOS", "composer": "TCOM",
}
MP4_ATOMS = {
    "title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "albumartist": "aART",
    "genre": "\xa9gen", "year": "\xa9day", "track": "trkn", "disc": "disk", "composer": "\xa9wrt",
}
VORBIS_KEYS = {
    "title": ["title"], "artist": ["artist"], "album": ["album"],
    "albumartist": ["albumartist", "album artist", "album_artist"],
    "genre": ["genre"], "year": ["date", "year", "originaldate"],
    "track": ["tracknumber"], "disc": ["discnumber"], "composer": ["composer"],
}
APE_KEYS = {
    "title": ["Title"], "artist": ["Artist"], "album": ["Album"],
    "albumartist": ["Album Artist", "AlbumArtist"], "genre": ["Genre"], "year": ["Year"],
    "track": ["Track"], "disc": ["Disc"], "composer": ["Composer"],
}
ASF_KEYS = {
    "title": ["Title"], "artist": ["Author"], "album": ["WM/AlbumTitle"],
    "albumartist": ["WM/AlbumArtist"], "genre": ["WM/Genre"], "year": ["WM/Year", "date"],
    "track": ["WM/TrackNumber", "WM/Track"], "disc": ["WM/PartOfSet"], "composer": ["WM/Composer"],
}

FORMAT_NAMES = {
    "MP3": "MP3", "EasyMP3": "MP3", "FLAC": "FLAC", "MP4": "MP4/M4A", "EasyMP4": "MP4/M4A",
    "OggVorbis": "Ogg Vorbis", "OggOpus": "Opus", "OggFLAC": "Ogg FLAC", "OggSpeex": "Speex",
    "ASF": "WMA", "WAVE": "WAV", "AIFF": "AIFF", "MonkeysAudio": "APE", "WavPack": "WavPack",
    "Musepack": "Musepack", "AAC": "AAC",
}


@dataclass
class TrackInfo:
    path: str
    tags: dict[str, str] = field(default_factory=dict)  # only fields that have a value
    has_artwork: bool = False
    artwork_info: str = ""
    format: str = ""
    duration: float | None = None
    bitrate: int | None = None
    size: int = 0
    error: str | None = None

    @property
    def filename(self) -> str:
        return os.path.basename(self.path)

    def missing(self, required) -> list[str]:
        """Field keys from `required` that this track lacks, in display order."""
        out = []
        for key, _ in FIELDS:
            if key not in required:
                continue
            present = self.has_artwork if key == ARTWORK else key in self.tags
            if not present:
                out.append(key)
        return out


def find_audio_files(folder: str, recursive: bool = True) -> list[str]:
    """All audio files under `folder`, sorted. Skips hidden files such as macOS '._' files."""
    paths = []
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if not name.startswith(".") and os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS:
                    paths.append(os.path.join(root, name))
    else:
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            if (not name.startswith(".") and os.path.isfile(full)
                    and os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS):
                paths.append(full)
    return sorted(paths, key=str.lower)


def _first_text(values) -> str | None:
    for value in values or []:
        text = str(value).strip()
        if text:
            return text
    return None


def _number_pair(pair) -> str | None:
    """Format an MP4 (number, total) tuple like '3/12'; 0 means unset."""
    number, total = (tuple(pair) + (0, 0))[:2]
    if not number:
        return None
    return f"{number}/{total}" if total else str(number)


def _parse_asf_picture(raw: bytes):
    # WM/Picture: type(1) + data size(4, LE) + mime (UTF-16LE, NUL) + description (UTF-16LE, NUL) + data
    def read_wstr(pos):
        end = pos
        while end + 1 < len(raw) and raw[end:end + 2] != b"\0\0":
            end += 2
        return raw[pos:end].decode("utf-16-le", "replace"), end + 2

    size = struct.unpack_from("<I", raw, 1)[0]
    mime, pos = read_wstr(5)
    _, pos = read_wstr(pos)
    return raw[pos:pos + size], mime


def _extract(audio):
    """Return ({field: text}, [(image_bytes, mime), ...]) for a mutagen file object."""
    tags = audio.tags
    text: dict[str, str] = {}
    pictures: list[tuple[bytes, str]] = []

    if isinstance(tags, ID3):
        for key, frame_id in ID3_FRAMES.items():
            frame = tags.get(frame_id)
            value = _first_text(frame.text) if frame is not None else None
            if value:
                text[key] = value
        pictures = [(f.data, f.mime) for f in tags.getall("APIC") if f.data]

    elif isinstance(tags, MP4Tags):
        for key, atom in MP4_ATOMS.items():
            values = tags.get(atom)
            if not values:
                continue
            value = _number_pair(values[0]) if atom in ("trkn", "disk") else _first_text(values)
            if value:
                text[key] = value
        for cover in tags.get("covr", []):
            mime = "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
            pictures.append((bytes(cover), mime))

    elif isinstance(tags, VCommentDict):
        for key, names in VORBIS_KEYS.items():
            for name in names:
                value = _first_text(tags.get(name))
                if value:
                    text[key] = value
                    break
        for b64 in tags.get("metadata_block_picture", []):
            try:
                pic = Picture(base64.b64decode(b64))
                pictures.append((pic.data, pic.mime))
            except Exception:
                pass
        for b64 in tags.get("coverart", []):  # legacy Ogg cover tag
            try:
                pictures.append((base64.b64decode(b64), "image/jpeg"))
            except Exception:
                pass

    elif isinstance(tags, APEv2):
        for key, names in APE_KEYS.items():
            for name in names:
                if name in tags:
                    value = str(tags[name]).strip()
                    if value:
                        text[key] = value
                        break
        for name in tags.keys():
            if name.lower().startswith("cover art"):
                raw = tags[name].value
                data = raw.split(b"\0", 1)[1] if b"\0" in raw else raw
                if data:
                    pictures.append((data, ""))

    elif isinstance(tags, ASFTags):
        for key, names in ASF_KEYS.items():
            for name in names:
                value = _first_text(attr.value for attr in tags.get(name, []))
                if value:
                    text[key] = value
                    break
        for attr in tags.get("WM/Picture", []):
            try:
                data, mime = _parse_asf_picture(attr.value)
                if data:
                    pictures.append((data, mime))
            except Exception:
                pass

    # FLAC stores pictures in their own metadata blocks, outside the tags.
    pictures.extend((p.data, p.mime) for p in getattr(audio, "pictures", []) or [] if p.data)
    return text, pictures


def _sniff_mime(data: bytes, mime: str) -> str:
    if mime:
        return mime
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:3] == b"GIF":
        return "image/gif"
    return "image"


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def read_track(path: str) -> TrackInfo:
    info = TrackInfo(path=path)
    try:
        info.size = os.path.getsize(path)
        audio = mutagen.File(path)
    except Exception as exc:
        info.error = f"Could not read file: {exc}"
        return info
    if audio is None:
        info.error = "Unrecognised audio format"
        return info

    info.format = FORMAT_NAMES.get(type(audio).__name__, type(audio).__name__)
    info.duration = getattr(audio.info, "length", None)
    info.bitrate = getattr(audio.info, "bitrate", None) or None
    try:
        info.tags, pictures = _extract(audio)
    except Exception as exc:
        info.error = f"Could not parse tags: {exc}"
        return info

    if pictures:
        info.has_artwork = True
        data, mime = pictures[0]
        kind = _sniff_mime(data, mime).split("/")[-1].upper()
        extra = f" (+{len(pictures) - 1} more)" if len(pictures) > 1 else ""
        info.artwork_info = f"{kind}, {_human_size(len(data))}{extra}"
    return info


def load_artwork(path: str) -> tuple[bytes, str] | None:
    """The first embedded picture of a file as (bytes, mime), or None."""
    try:
        audio = mutagen.File(path)
        if audio is None:
            return None
        _, pictures = _extract(audio)
    except Exception:
        return None
    if not pictures:
        return None
    data, mime = pictures[0]
    return data, _sniff_mime(data, mime)

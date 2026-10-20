"""Write tag values and cover artwork into audio files, in each format's native tag style."""
from __future__ import annotations

import base64
import io
import os
import shutil
import struct
import time

import mutagen
from mutagen import id3
from mutagen.apev2 import APEv2, APEValue, BINARY
from mutagen.asf import ASFByteArrayAttribute, ASFTags
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4Cover, MP4Tags
from mutagen._vorbis import VCommentDict

from tag_reader import APE_KEYS, ASF_KEYS, ID3_FRAMES, MP4_ATOMS, VORBIS_KEYS

try:
    from PIL import Image
except ImportError:
    Image = None

BACKUP_DIR_NAME = ".tag_backups"  # hidden, so the scanner skips it


def backup_file(path: str) -> str:
    """Copy `path` into a hidden .tag_backups folder next to it; returns the copy's path."""
    folder = os.path.join(os.path.dirname(path), BACKUP_DIR_NAME)
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, os.path.basename(path))
    if os.path.exists(target):  # keep the oldest original; number later copies
        stem, ext = os.path.splitext(target)
        target = f"{stem}.{time.strftime('%Y%m%d-%H%M%S')}{ext}"
    shutil.copy2(path, target)
    return target


def _number_pair(value: str) -> tuple[int, int]:
    number, _, total = value.partition("/")
    return int(number or 0), int(total or 0) if total.strip().isdigit() else 0


def _picture_block(data: bytes, mime: str) -> Picture:
    pic = Picture()
    pic.type = 3  # front cover
    pic.mime = mime
    pic.desc = "Cover"
    pic.data = data
    if Image is not None:
        try:
            img = Image.open(io.BytesIO(data))
            pic.width, pic.height = img.size
            pic.depth = 24
        except Exception:
            pass
    return pic


def _asf_picture(data: bytes, mime: str) -> bytes:
    return (struct.pack("<bI", 3, len(data)) + mime.encode("utf-16-le") + b"\0\0"
            + "Cover".encode("utf-16-le") + b"\0\0" + data)


def write_tags(path: str, values: dict[str, str], artwork: tuple[bytes, str] | None = None,
               backup: bool = True):
    """Set text fields (keys as in tag_reader.FIELDS) and optionally the front cover."""
    if backup:
        backup_file(path)
    audio = mutagen.File(path)
    if audio is None:
        raise ValueError("Unrecognised audio format")
    had_tags = audio.tags is not None
    if not had_tags:
        try:
            audio.add_tags()
        except Exception as exc:
            raise ValueError(f"This file type cannot hold tags ({exc})") from exc
    tags = audio.tags
    save_kwargs = {}

    if isinstance(tags, id3.ID3):
        for key, value in values.items():
            frame_id = ID3_FRAMES[key]
            tags.setall(frame_id, [getattr(id3, frame_id)(encoding=3, text=[value])])
        if artwork:
            data, mime = artwork
            tags.setall("APIC", [id3.APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data)])
        # Keep v2.4 files as v2.4; otherwise write v2.3, which every player understands.
        save_kwargs["v2_version"] = 4 if had_tags and tags.version >= (2, 4, 0) else 3

    elif isinstance(tags, MP4Tags):
        for key, value in values.items():
            atom = MP4_ATOMS[key]
            tags[atom] = [_number_pair(value)] if atom in ("trkn", "disk") else [value]
        if artwork:
            data, mime = artwork
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            tags["covr"] = [MP4Cover(data, imageformat=fmt)]

    elif isinstance(tags, VCommentDict):
        for key, value in values.items():
            tags[VORBIS_KEYS[key][0]] = [value]
        if artwork:
            pic = _picture_block(*artwork)
            if isinstance(audio, FLAC):
                audio.clear_pictures()
                audio.add_picture(pic)
            else:
                tags["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]

    elif isinstance(tags, APEv2):
        for key, value in values.items():
            tags[APE_KEYS[key][0]] = value
        if artwork:
            data, mime = artwork
            ext = "png" if mime == "image/png" else "jpg"
            tags["Cover Art (Front)"] = APEValue(f"cover.{ext}".encode() + b"\0" + data, BINARY)

    elif isinstance(tags, ASFTags):
        for key, value in values.items():
            tags[ASF_KEYS[key][0]] = [value]
        if artwork:
            tags["WM/Picture"] = [ASFByteArrayAttribute(_asf_picture(*artwork))]

    else:
        raise ValueError(f"Writing {type(tags).__name__} tags is not supported")

    audio.save(**save_kwargs)

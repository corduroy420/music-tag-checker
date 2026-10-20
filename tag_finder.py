"""Suggest values for missing tags: guess them from the file name and look them up online.

Sources, in the order they are tried (none needs an account or API key):
  Deezer, Apple Music (iTunes Search API), MusicBrainz + Cover Art Archive,
  and optionally SoundCloud / YouTube thumbnails via yt-dlp for artwork.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, fields as dc_fields
from difflib import SequenceMatcher

from tag_reader import ARTWORK, FIELDS, TrackInfo

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

HAS_YTDLP = yt_dlp is not None

USER_AGENT = "MusicTagChecker/1.0 (personal music library tag fixer)"
AUTO_ACCEPT = 0.7    # suggestions at or above this confidence are pre-ticked in the review window
MATCH_MIN = 0.6      # online results scoring below this are ignored
STRONG_MATCH = 0.8   # above this, online artist/title spelling beats the file-name guess
VIDEO_MATCH_MIN = 0.75  # uploads on SoundCloud/YouTube need a closer match than store results
MAX_ARTWORK_SIZE = 1200


# ---------------------------------------------------------------- data types
@dataclass
class Suggestion:
    field: str
    value: str | None = None          # text value (None for artwork or when nothing was found)
    source: str = ""
    confidence: float = 0.0
    match: str = ""                   # what was matched, shown to the user
    image: bytes | None = None
    image_mime: str = ""
    image_info: str = ""

    @property
    def found(self) -> bool:
        return bool(self.value) or self.image is not None


@dataclass
class Candidate:
    source: str
    artist: str
    title: str
    album: str | None = None
    albumartist: str | None = None
    genre: str | None = None
    year: str | None = None
    track: str | None = None
    disc: str | None = None
    duration: float | None = None
    artwork_url: str | None = None
    crop_square: bool = False
    ref: object = None
    score: float = 0.0

    def describe(self) -> str:
        parts = [f"{self.artist} – {self.title}"]
        if self.album:
            parts.append(self.album)
        if self.year:
            parts.append(self.year)
        return " · ".join(parts)


@dataclass
class FilenameGuess:
    artist: str | None = None
    title: str | None = None
    track: str | None = None


# ---------------------------------------------------------------- HTTP
class _Throttle:
    """Keeps a minimum gap between requests to one host (APIs have rate limits)."""

    def __init__(self, interval: float):
        self.interval = interval
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self):
        with self.lock:
            delay = self.last + self.interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


_THROTTLES = {
    "api.deezer.com": _Throttle(0.15),         # 50 requests / 5 s
    "itunes.apple.com": _Throttle(3.1),        # ~20 requests / minute
    "musicbrainz.org": _Throttle(1.1),         # 1 request / second
}


def _http_get(url: str, timeout: float = 15) -> bytes:
    throttle = _THROTTLES.get(urllib.parse.urlsplit(url).hostname or "")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        if throttle:
            throttle.wait()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503) or attempt == 2:  # retry only "slow down" responses
                raise
            time.sleep(2 * (attempt + 1))


def _get_json(url: str, **params):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return json.loads(_http_get(url))


# ---------------------------------------------------------------- file names
_JUNK_WORDS = (r"official|music video|lyrics?|visuali[sz]er|audio|video|hd|hq|4k|free download|"
               r"out now|premiere|explicit|clean version")
_JUNK_RE = re.compile(rf"\s*[(\[【][^)\]】]*\b(?:{_JUNK_WORDS})\b[^)\]】]*[)\]】]", re.I)
_SEPARATOR_RE = re.compile(r"\s+[-–—~|]\s+")
# "01 - ", "1. ", "03) " or a zero-padded "07 " at the start of a file name.
# Unpadded "99 Problems" is left alone.
_TRACK_PREFIX_RE = re.compile(r"^\s*(?:(\d{1,3})\s*[-.)]\s*|(0\d)\s+)(?=\S)")


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" -_.")


_CHANNEL_SUFFIX_RE = re.compile(r"(tv|vevo|official|oficial|music|musica|records|channel|topic)$", re.I)


def _looks_like_channel(part: str) -> bool:
    """True for uploader names YouTube downloaders append, e.g. 'ElAlfaElJefeTV', 'AdeleVEVO', 'X - Topic'."""
    part = part.strip()
    if not part:
        return False
    if " " not in part:
        return bool(_CHANNEL_SUFFIX_RE.search(part) or (len(part) >= 6 and re.search(r"[a-z][A-Z]", part)))
    return bool(re.search(r"\b(vevo|topic)$", part, re.I))


def _split_parts(text: str) -> list[str]:
    """Split on ' - ', dropping junk like '(Official Video)' and a trailing channel name."""
    parts = [p.strip() for p in _SEPARATOR_RE.split(_collapse(_JUNK_RE.sub("", text))) if p.strip()]
    if len(parts) > 2 and parts[-1].lower() == "topic":  # "… - Artist - Topic" (YouTube auto channel)
        parts = parts[:-2]
    while len(parts) > 1 and _looks_like_channel(parts[-1]):
        parts.pop()
    return parts


def clean_title(title: str) -> str:
    """'Song (Video Oficial) - SomeChannelTV' -> 'Song'."""
    return " - ".join(_split_parts(title)) or title


def split_artists(artist: str) -> list[str]:
    """'El Alfa, Yandel & Myke Towers feat. X' -> ['El Alfa', 'Yandel', 'Myke Towers', 'X']."""
    names = re.split(r"\s*(?:,|&|\+|\bfeat\.?|\bft\.?|\bfeaturing\b|\bx\b|\bvs\.?|\band\b|\by\b)\s*",
                     artist, flags=re.I)
    return [n.strip() for n in names if n.strip()]


def split_artist_title(text: str) -> tuple[str | None, str]:
    """'Artist - Title (Official Video) - ChannelTV' -> ('Artist', 'Title')."""
    parts = _split_parts(text)
    if len(parts) >= 2:
        return parts[0], " - ".join(parts[1:])
    return None, parts[0] if parts else text


def parse_filename(path: str) -> FilenameGuess:
    stem = _collapse(os.path.splitext(os.path.basename(path))[0].replace("_", " "))
    track = None
    match = _TRACK_PREFIX_RE.match(stem)
    if match:
        track = str(int(match.group(1) or match.group(2)))
        stem = stem[match.end():]
    artist, title = split_artist_title(stem)
    return FilenameGuess(artist=artist or None, title=title or None, track=track)


# ---------------------------------------------------------------- matching
_VERSION_RE = re.compile(r"\b(remix|mix|bootleg|vip|flip|live|acoustic|instrumental|cover|rework|mashup|edit)\b")
_ORIGINAL_RE = re.compile(r"\b(original mix|original version|album version)\b")


def _versions(text: str) -> set[str]:
    return set(_VERSION_RE.findall(_ORIGINAL_RE.sub("", text.lower())))


def _norm(text: str, strip_brackets: bool = False) -> str:
    text = text.lower()
    if strip_brackets:
        text = re.sub(r"[(\[].*?[)\]]", "", text)
    text = re.sub(r"\b(feat|ft|featuring)\b\.?.*$", "", text)
    text = text.replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


def _similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    best = 0.0
    for strip in (False, True):
        x, y = _norm(a, strip), _norm(b, strip)
        if x and y:
            ratio = SequenceMatcher(None, x, y).ratio()
            best = max(best, ratio * (0.92 if strip else 1.0))
    return best


def _artist_similarity(ours: str | None, theirs: str | None) -> float:
    """Like _similarity, but 'El Alfa, Yandel, Myke Towers' also matches a store crediting just 'El Alfa'."""
    base = _similarity(ours, theirs)
    if not ours or not theirs:
        return base
    a, b = split_artists(ours), split_artists(theirs)
    if len(a) > 1 or len(b) > 1:
        hits = sum(1 for y in b if any(_similarity(x, y) >= 0.85 for x in a))
        if hits:
            base = max(base, 0.85 + 0.15 * hits / max(len(a), len(b)))
    return base


def _title_variants(title: str) -> list[str]:
    cleaned = clean_title(title)
    return list(dict.fromkeys([cleaned, title, cleaned.split(" - ")[0]]))


def score_candidate(c: Candidate, artist: str | None, title: str, duration: float | None):
    """Score 0..1 for how well a search result matches the track. Tries artist/title both ways round."""
    titles = _title_variants(title)
    title_sim = max(_similarity(t, c.title) for t in titles)
    if artist:
        straight = 0.55 * title_sim + 0.45 * _artist_similarity(artist, c.artist)
        swapped = 0.55 * _similarity(artist, c.title) + 0.45 * max(_artist_similarity(t, c.artist) for t in titles)
        score = max(straight, swapped)
    else:
        # Without an artist the "title" may really be "Artist Title" run together.
        score = 0.85 * max(title_sim, max(_similarity(t, f"{c.artist} {c.title}") for t in titles))
    if _versions(f"{artist or ''} {titles[0]}") != _versions(c.title):  # a remix/edit is not the original
        score -= 0.2
    if duration and c.duration and c.duration > 31:  # ignore 30-second previews
        diff = abs(duration - c.duration)
        if diff <= 3:
            score += 0.05
        elif diff > 20:
            score -= 0.15
    c.score = max(0.0, min(1.0, score))


def _number(n, total=None) -> str | None:
    if not n:
        return None
    return f"{n}/{total}" if total else str(n)


# ---------------------------------------------------------------- sources
def search_deezer(artist: str | None, title: str) -> list[Candidate]:
    query = f'artist:"{artist}" track:"{title}"' if artist else title
    data = _get_json("https://api.deezer.com/search", q=query, limit=5)
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "error"))
    results = data.get("data") or []
    if not results and artist:  # also catches files named "Title - Artist"
        results = _get_json("https://api.deezer.com/search", q=f"{artist} {title}", limit=5).get("data") or []
    return [
        Candidate("Deezer", r["artist"]["name"], r["title"], album=r["album"].get("title"),
                  duration=r.get("duration"), artwork_url=r["album"].get("cover_xl"),
                  ref=(r["id"], r["album"]["id"]))
        for r in results
    ]


def enrich_deezer(c: Candidate):
    """Deezer search results lack genre/year/track number; fetch them for a chosen result."""
    track_id, album_id = c.ref
    track = _get_json(f"https://api.deezer.com/track/{track_id}")
    album = _get_json(f"https://api.deezer.com/album/{album_id}")
    genres = (album.get("genres") or {}).get("data") or []
    c.genre = genres[0]["name"] if genres else None
    date = album.get("release_date") or track.get("release_date") or ""
    c.year = date[:4] or None
    c.track = _number(track.get("track_position"), album.get("nb_tracks"))
    c.disc = _number(track.get("disk_number"))
    c.albumartist = (album.get("artist") or {}).get("name")


def search_itunes(artist: str | None, title: str) -> list[Candidate]:
    data = _get_json("https://itunes.apple.com/search", term=f"{artist or ''} {title}".strip(),
                     entity="song", limit=8)
    out = []
    for r in data.get("results", []):
        art = r.get("artworkUrl100")
        out.append(Candidate(
            "Apple Music", r.get("artistName", ""), r.get("trackName", ""),
            album=r.get("collectionName"),
            albumartist=r.get("collectionArtistName") or r.get("artistName"),
            genre=r.get("primaryGenreName"),
            year=(r.get("releaseDate") or "")[:4] or None,
            track=_number(r.get("trackNumber"), r.get("trackCount")),
            disc=_number(r.get("discNumber"), r.get("discCount")),
            duration=(r["trackTimeMillis"] / 1000) if r.get("trackTimeMillis") else None,
            artwork_url=art.replace("100x100bb", "1000x1000bb") if art else None,
        ))
    return out


def _pick_release(releases: list[dict]) -> dict | None:
    def rank(rel):
        group = rel.get("release-group") or {}
        return (
            rel.get("status") != "Official",
            group.get("primary-type") not in ("Album", "EP", "Single"),
            bool(group.get("secondary-types")),  # compilations, live, soundtracks…
            group.get("primary-type") != "Album",
        )
    return min(releases, key=rank) if releases else None


def search_musicbrainz(artist: str | None, title: str) -> list[Candidate]:
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')
    query = f'recording:"{esc(title)}"' + (f' AND artist:"{esc(artist)}"' if artist else "")
    data = _get_json("https://musicbrainz.org/ws/2/recording", query=query, fmt="json", limit=5)
    out = []
    for rec in data.get("recordings", []):
        name = "".join(ac.get("name", "") + ac.get("joinphrase", "") for ac in rec.get("artist-credit", []))
        c = Candidate("MusicBrainz", name, rec.get("title", ""),
                      duration=rec["length"] / 1000 if rec.get("length") else None)
        tags = sorted(rec.get("tags") or [], key=lambda t: -t.get("count", 0))
        if tags:
            c.genre = tags[0]["name"].title()
        release = _pick_release(rec.get("releases") or [])
        if release:
            c.album = release.get("title")
            c.year = (release.get("date") or "")[:4] or None
            credit = release.get("artist-credit")
            c.albumartist = credit[0]["name"] if credit else name
            media = (release.get("media") or [{}])[0]
            number = ((media.get("track") or [{}])[0]).get("number")
            c.track = _number(number, media.get("track-count"))
            c.disc = _number(media.get("position"))
            group_id = (release.get("release-group") or {}).get("id")
            c.artwork_url = (f"https://coverartarchive.org/release-group/{group_id}/front-500" if group_id
                             else f"https://coverartarchive.org/release/{release['id']}/front-500")
        out.append(c)
    return out


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _best_thumbnail(entry: dict, site: str) -> str | None:
    thumbs = [t for t in entry.get("thumbnails") or [] if t.get("url")]
    if site == "SoundCloud":
        art = [t["url"] for t in thumbs if "artworks-" in t["url"]]  # skip uploader avatars
        for preferred in ("-original.", "-t500x500."):
            for url in art:
                if preferred in url:
                    return url
        return art[-1].replace("-large.", "-t500x500.") if art else None
    if not thumbs:
        return None
    return max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))["url"]


def search_video_sites(artist: str | None, title: str, log) -> list[Candidate]:
    """Search SoundCloud and YouTube (via yt-dlp) for artwork. Only metadata/thumbnails are fetched."""
    if yt_dlp is None:
        return []
    query = f"{split_artists(artist)[0] if artist else ''} {clean_title(title)}".strip()
    options = {"quiet": True, "no_warnings": True, "skip_download": True,
               "extract_flat": "in_playlist", "logger": _QuietLogger()}
    out = []
    for prefix, site in (("scsearch3", "SoundCloud"), ("ytsearch3", "YouTube")):
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(f"{prefix}:{query}", download=False)
        except Exception as exc:
            log(f"{site} search failed: {exc}")
            continue
        for entry in info.get("entries") or []:
            thumb = _best_thumbnail(entry, site)
            if not thumb:
                continue
            found_artist, found_title = split_artist_title(entry.get("title") or "")
            uploader = re.sub(r"\s*-\s*Topic$", "", entry.get("uploader") or entry.get("channel") or "")
            out.append(Candidate(site, found_artist or uploader, found_title, duration=entry.get("duration"),
                                 artwork_url=thumb, crop_square=(site == "YouTube")))
    return out


# ---------------------------------------------------------------- artwork
def _sniff_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    return "image/jpeg"


def prepare_artwork(data: bytes, crop_square: bool = False) -> tuple[bytes, str, str]:
    """Normalise downloaded artwork to a JPEG of at most MAX_ARTWORK_SIZE px; crop video thumbnails square."""
    if Image is None:
        return data, _sniff_mime(data), ""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if crop_square:
        box = img.convert("L").point(lambda p: 255 if p > 18 else 0).getbbox()  # trim black bars
        if box:
            img = img.crop(box)
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    if max(img.size) > MAX_ARTWORK_SIZE:
        img.thumbnail((MAX_ARTWORK_SIZE, MAX_ARTWORK_SIZE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue(), "image/jpeg", f"{img.width}×{img.height} JPEG"


def _fetch_artwork(c: Candidate, log):
    try:
        data = _http_get(c.artwork_url)
        if len(data) < 1000:
            return None
        return prepare_artwork(data, c.crop_square)
    except Exception as exc:
        log(f"Artwork download from {c.source} failed: {exc}")
        return None


# ---------------------------------------------------------------- orchestration
_SOURCES = [("Deezer", search_deezer), ("Apple Music", search_itunes), ("MusicBrainz", search_musicbrainz)]
_CANDIDATE_FIELDS = {f.name for f in dc_fields(Candidate)}


def _covers(candidates: list[Candidate], wanted: set[str]) -> bool:
    good = [c for c in candidates if c.score >= MATCH_MIN]
    if not good or good[0].score < STRONG_MATCH:
        return False
    for field in wanted:
        if field == ARTWORK:
            if not any(c.artwork_url for c in good):
                return False
        elif field in _CANDIDATE_FIELDS and not any(getattr(c, field) for c in good):
            return False
    return True


def _search_queries(artist: str | None, title: str) -> list[tuple[str | None, str]]:
    """Search terms to try, best first: cleaned title with the full artist, then with only the main artist, …"""
    artists = list(dict.fromkeys([artist, split_artists(artist)[0]])) if artist else [None]
    return [(a, t) for t in _title_variants(title) for a in artists][:4]


def _search_online(artist, title, duration, wanted: set[str], log) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen = set()
    failed: set[str] = set()  # don't keep hammering a service that is down or rate-limiting us
    for query_artist, query_title in _search_queries(artist, title):
        for name, search in _SOURCES:
            if name in failed:
                continue
            try:
                found = search(query_artist, query_title)
            except Exception as exc:
                log(f"{name} lookup failed: {exc}")
                failed.add(name)
                continue
            found = [c for c in found if (c.source, c.artist, c.title, c.album) not in seen]
            seen.update((c.source, c.artist, c.title, c.album) for c in found)
            for c in found:
                score_candidate(c, artist, title, duration)
            found.sort(key=lambda c: c.score, reverse=True)
            if (name == "Deezer" and found and found[0].score >= MATCH_MIN
                    and wanted & {"genre", "year", "track", "disc", "albumartist"}):
                try:
                    enrich_deezer(found[0])
                except Exception as exc:
                    log(f"Deezer details failed: {exc}")
            candidates.extend(found)
            candidates.sort(key=lambda c: c.score, reverse=True)
            if _covers(candidates, wanted):
                return candidates
    return candidates


def _find_artwork(good, artist, title, duration, use_video_sites, log) -> Suggestion:
    for c in good:
        if c.artwork_url:
            art = _fetch_artwork(c, log)
            if art:
                return Suggestion(ARTWORK, None, c.source, c.score, c.describe(), *art)
    if use_video_sites and artist and title:  # title-only searches return random uploads
        videos = search_video_sites(artist, title, log)
        for c in videos:
            score_candidate(c, artist, title, duration)
        for c in sorted(videos, key=lambda c: c.score, reverse=True):
            if c.score < VIDEO_MATCH_MIN:
                break
            art = _fetch_artwork(c, log)
            if art:
                # Uploaded thumbnails are less trustworthy than store artwork.
                return Suggestion(ARTWORK, None, c.source, c.score * 0.9, f"“{c.title}” by {c.artist}", *art)
    return Suggestion(ARTWORK, match="No artwork found")


def find_for_track(track: TrackInfo, wanted, use_video_sites: bool = False, log=lambda msg: None) -> list[Suggestion]:
    """One Suggestion per wanted field (in display order); `found` is False where nothing turned up."""
    wanted = set(wanted)
    guess = parse_filename(track.path)
    artist = track.tags.get("artist") or guess.artist
    title = track.tags.get("title") or guess.title

    candidates = _search_online(artist, title, track.duration, wanted, log) if title else []
    good = [c for c in candidates if c.score >= MATCH_MIN]
    strong = good[0] if good and good[0].score >= STRONG_MATCH else None
    filename = os.path.basename(track.path)

    suggestions = []
    for field, _ in FIELDS:
        if field not in wanted:
            continue
        if field == ARTWORK:
            suggestions.append(_find_artwork(good, artist, title, track.duration, use_video_sites, log))
            continue
        suggestion = None
        if field in ("artist", "title", "track"):
            if field == "track" and guess.track:  # the file's own numbering beats a store's album
                suggestion = Suggestion(field, guess.track, "File name", 0.75, f"Parsed from “{filename}”")
            elif (field == "artist" and strong and guess.artist
                  and len(split_artists(guess.artist)) > len(split_artists(strong.artist))
                  and _artist_similarity(guess.artist, strong.artist) >= 0.85):
                # The store credits fewer artists than the file name: keep the fuller credit.
                suggestion = Suggestion(field, guess.artist, "File name", strong.score,
                                        f"Parsed from “{filename}”, confirmed by {strong.source}")
            elif strong and getattr(strong, field):
                suggestion = Suggestion(field, getattr(strong, field), strong.source, strong.score, strong.describe())
            elif getattr(guess, field):
                confidence = 0.75 if field == "track" or guess.artist else 0.5
                suggestion = Suggestion(field, getattr(guess, field), "File name", confidence,
                                        f"Parsed from “{filename}”")
        if suggestion is None and field in _CANDIDATE_FIELDS:
            for c in good:
                if getattr(c, field):
                    suggestion = Suggestion(field, getattr(c, field), c.source, c.score, c.describe())
                    break
        suggestions.append(suggestion or Suggestion(field, match="No match found"))
    return suggestions

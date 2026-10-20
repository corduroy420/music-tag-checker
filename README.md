# Music Tag Checker

A desktop app that scans a folder of music files, reads their tags, and shows which
tracks are missing title, artist, album, album artist, genre, year, track #, disc #,
composer or cover artwork.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python music_tag_checker.py            # then click Browse…
.venv/bin/python music_tag_checker.py ~/Music    # scan a folder straight away
```

## Features

- Formats: MP3, FLAC, M4A/MP4/AAC, Ogg Vorbis, Opus, WMA, WAV, AIFF, APE, WavPack, Musepack
- Optionally scans subfolders; skips hidden files (including macOS `._` files)
- Rows are colour-coded: green = complete, red = missing something, grey = unreadable
- Choose which fields count as "missing" with the checkboxes
- Filter to incomplete/complete/errors, by a specific missing field, or by search text
- Click a column header to sort; click a track to see all its tags and its cover art
- Double-click a track (or use the button) to show it in Finder/Explorer
- Export the current view to CSV
- Status bar summarises totals and how many tracks miss each field

## Setting a tag on many tracks (e.g. a genre for a whole folder)

1. Right-click (or Control-click) any track in the folder → **Select all in this folder**
   (or "…and its subfolders"). The **Select all in this folder** button in the side panel does the same.
   The *Folder* column shows where each track lives; click its header to group tracks by folder.
2. Click **Set** next to *Genre* in the side panel, or right-click → **Set genre on selected…**.
3. Type the genre or pick one already used in your library. Choose whether to replace existing genres
   or only fill tracks where it is empty. The window shows how many files will change before you click **Write**.

The same window can set artist, album, album artist, year, disc #, composer or **artwork**
(pick an image file to give a whole album folder the same cover). With a single track selected,
it can also change the title and track number.

## Removing emojis

**Remove emojis…** in the toolbar (or right-click → *Remove emojis from selected…*) strips emojis from
every text tag and from file names, for the selected tracks or, if none are selected, every shown track.
A preview lists each change as before → after; untick any you want to keep, then **Apply**.
Accents, non-Latin scripts and symbols like © are kept. A tag that is *only* emojis is left alone,
since removing them would leave it blank.

## Finding missing tags

Select tracks (shift-click for a range, ⌘/Ctrl-click to add, ⌘/Ctrl-A for all) and click
**Find all missing tags**, or click **Find** next to a single field in the side panel.
**Find missing tags…** in the toolbar works on the selection, or on every shown track if nothing is selected.

1. Artist, title and track number are guessed from the file name
   (`03 - Artist - Title (Official Video).mp3`). The online lookup also tries the reverse order, so
   `Title - Artist` files are corrected.
2. Deezer, then Apple Music, then MusicBrainz are searched for album, album artist, genre, year,
   track/disc number and 1000×1000 cover art. None of them needs an account or API key.
3. If the *SoundCloud/YouTube* box is ticked and no store has the artwork, yt-dlp searches those sites
   and uses the track's artwork or the video thumbnail (cropped square). Only images are downloaded, never audio.
4. A **review window** lists every suggestion with its source and match score. Strong matches start ticked.
   Untick anything wrong, double-click to edit a value or pick your own image file, then **Write**.
   Originals are backed up to a hidden `.tag_backups` folder next to each file unless you turn that off.

Composer is rarely available from these sources, so it usually has to be entered by hand.

## Files

`tag_reader.py` holds the tag-reading logic and has no GUI dependency.
Pillow is optional: without it, PNG covers still preview, JPEG covers are just reported.

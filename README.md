# MP3 Tag Checker

A Windows desktop application that audits and fixes the ID3 tags of an MP3
library — local or on a NAS over Samba (a mapped network drive). Nothing is
ever written without your review: the app scans, proposes changes, and writes
only what you approve. Every write is snapshotted first and can be reverted
via History.

Written by **Claude Fable 5** (Anthropic's AI model) in collaboration with
Jakub Konopásek.

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

## What it does

- **Scans** the artist folders you list, reads every MP3's tags, and stores a
  snapshot in a local SQLite database. Rescans only touch new/changed files.
- **Finds problems** with a configurable rule engine, including:
  - broken text (mojibake) from wrong code pages — repaired only on hard
    evidence, never by guessing
  - legacy ID3v1 tags: safe removal, rescue of data missing from ID3v2, and
    conflict detection when the two tags disagree
  - old ID3v2 versions and non-UTF-8 text encodings
  - missing required fields (title, artist, album, track, year, genre, …)
  - track-number format, gaps in track numbering
  - inconsistent album name or year inside one album folder
  - multi-value fields glued into one string (splitting is configurable)
  - album-artist unification, artist ↔ album artist consistency
  - missing or too-small embedded cover art, missing `folder.jpg` /
    `artist.jpg`
- **Proposes fixes** — you review them per album, per artist, or per change
  type, edit anything by hand, and apply only what you select.
- **Keeps history**: every write creates a tag snapshot and a changelog
  entry; any file can be reverted to any earlier state.
- **Online metadata on demand** (never automatic): MusicBrainz release
  search, Cover Art Archive covers, Deezer artist images.
- **Multiple libraries**, each with its own root folder and database.
- Light/Dark/custom **themes** and translatable field labels (Czech set
  included).

## Quick start

1. Install [Python 3.9+](https://www.python.org/downloads/) (check *"Add
   python.exe to PATH"* during installation).
2. Download or clone this repository.
3. Double-click **`run.bat`**. On first run it creates a virtual environment
   and installs the dependencies (a few minutes); afterwards it starts
   instantly.
4. In the app, open **Libraries…**, point it at your music root, and list
   your artist folders. Then **Scan library…** and review what it found.

See the [User Guide](docs/USER_GUIDE.md) for a full walkthrough and the
[Development Guide](docs/DEVELOPMENT.md) for the architecture and how to
contribute.

## Safety model

- The scanner and rule engine never write to your files.
- Writes happen only when you click Apply, and each one is snapshotted
  beforehand and logged, so it can be reverted.
- File modification times are preserved on write (configurable).
- An unresolved ID3v1/ID3v2 conflict blocks any write that would destroy the
  old tag's data.

## Requirements

- Windows, Python 3.9+
- Dependencies (installed automatically by `run.bat`):
  [mutagen](https://mutagen.readthedocs.io/) (tag I/O),
  [PySide6](https://doc.qt.io/qtforpython/) (GUI),
  [Pillow](https://python-pillow.org/) (cover images),
  [requests](https://requests.readthedocs.io/) (online metadata)

## License

[MIT](LICENSE) — free for personal and commercial use.

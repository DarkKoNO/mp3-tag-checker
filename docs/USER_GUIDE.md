# MP3 Tag Checker — User Guide

This guide walks through everything from installation to reverting a change.
The application was written by **Claude Fable 5** (Anthropic's AI model) in
collaboration with Jakub Konopásek.

## 1. Installation and first run

1. Install [Python 3.9 or newer](https://www.python.org/downloads/). During
   installation, check **"Add python.exe to PATH"**.
2. Download this repository (green **Code** button → *Download ZIP*, then
   unpack) or clone it with git.
3. Double-click **`run.bat`**.
   - On the first run it creates a Python virtual environment in `.venv\` and
     installs the dependencies. This takes a few minutes and needs an
     internet connection.
   - Every later start is instant; dependencies are reinstalled only when
     `requirements.txt` changes.

If the window never appears, check `error.log` next to `run.bat` — see
[Troubleshooting](#9-troubleshooting).

## 2. Setting up a library

A *library* is one music collection: a root folder (for example a mapped NAS
drive such as `H:\Music`), a list of artist folders inside it, and its own
database file. You can have several libraries and switch between them with
the selector in the top-left corner.

1. Click **Libraries…**.
2. Add a library: give it a name and pick the root folder.
3. Each library reads its artist list from a text file (by default
   `folders.txt` next to the app). One artist folder per line, relative to
   the root:

   ```
   Waits, Tom\
   Zappa, Frank\
   Sleepytime Gorilla Museum\
   ```

   Only the folders listed there are scanned — the app never wanders through
   the rest of the drive.

The expected layout on disk is `root \ artist folder \ album folder \ *.mp3`.

## 3. Scanning

Click **Scan library…**. Scanning:

- walks the listed artist folders and reads the tags of files that are new
  or changed since the last scan (an unchanged library rescans quickly),
- stores a *snapshot* of every file's tags in the library database,
- runs the rule engine and turns its findings into *proposals*.

**Scanning never modifies your files.** You can also rescan a single artist
or album from the right-click menu in the tree.

## 4. Reviewing problems

The **Library** page shows everything the rules found. Group the tree **By
artist** (artist → album → change type) or **By change type** (rule → the
files it affects). Problems come in two severities: red for serious issues
(broken text, missing core fields, conflicting tags…) and yellow for
cosmetic ones.

Selecting an item shows the details on the right: the current value, the
proposed value, and an explanation of the rule (hover any change-type name
for a detailed tooltip). You can:

- **check or uncheck** individual proposals — nothing unchecked is written,
- **edit values by hand** before applying,
- **postpone** an issue you want to decide later, or add an **exception**
  so a rule stops flagging a particular case.

Some findings have no automatic fix (missing cover, gaps in track numbers,
ID3v1/v2 conflicts…). They appear under *needs attention* and wait for a
manual decision.

## 5. Applying changes

**Apply selected** writes the checked proposals to the MP3 files. Before you
confirm, the app tells you exactly how many files and fields are affected.
Every write:

1. writes the new tag to the file (preserving the file's modification time,
   unless disabled in Settings),
2. snapshots the file's new state,
3. records each changed field in the change log,
4. re-runs the rules so the tree is immediately up to date.

## 6. History and revert

Because every state of every file is snapshotted, any file (or a whole
album) can be reverted to any earlier version via its **History**. Reverts
are themselves logged writes — so they too can be undone.

## 7. Search and Change log pages

- **Search** finds tracks by any field, with saved search expressions.
- **Change log** lists every field ever written by the app: when, which
  file, which field, old value → new value, and why.

## 8. Internet features (all on demand)

Nothing is ever contacted automatically. When you ask for it:

- **Internet check…** searches **MusicBrainz** for the selected album and
  shows the differences against your tags — you decide what to take over.
- Missing covers can be fetched from the **Cover Art Archive** and artist
  images from **Deezer**, again only per your explicit request.

## 9. Settings

The **Settings** page controls the rule engine and the look of the app.
Highlights:

- **Rules** — every rule can be *enabled*, *postponed*, or *disabled*.
- **Required fields** — which tags count as missing when empty.
- **Multi-value fields and splitting** — which fields may hold several
  values (artist, genre, …) and which separators (`;`, `\`, `/`, `,`,
  custom) should split a combined value.
- **Field patterns** — e.g. year must match `^\d{4}$`, with a list of
  allowed exceptions (`unknown`, `undated`, …).
- **Covers** — minimum acceptable embedded cover size, whether to write
  `folder.jpg` next to the files.
- **Themes** — Light and Dark are built in; you can create your own themes
  (colors and fonts) in the theme editor.
- **Field labels** — the names shown for tag fields; a Czech set ships with
  the app and you can define your own.

## 10. Files the app creates

All of these live next to `run.bat` and are personal — they are not part of
the repository:

| File | Purpose |
|---|---|
| `config.json` | libraries and all settings (recreated with defaults if deleted) |
| `folders.txt` | artist folder list of the default library |
| `library*.db` | per-library scan database: snapshots, proposals, changelog |
| `themes.json` | your custom themes |
| `error.log` | details of any unexpected error |
| `.venv\` | the Python environment built by `run.bat` |

## 11. Troubleshooting

- **The app doesn't start / closes immediately** — open `error.log`. If it
  mentions *"DLL load failed"* or *shiboken*, install the [Microsoft Visual
  C++ Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe)
  and try again.
- **Dependency installation fails** — usually offline or a proxy blocking
  python.org; run `run.bat` again once connected.
- **A NAS library scans slowly** — the first scan reads every file over the
  network; later scans only read new/changed files and are much faster.
- **Something was applied that shouldn't have been** — open the file's or
  album's History and revert; nothing the app writes is unrecoverable.

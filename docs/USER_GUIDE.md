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

When a scan finishes, a **scan log** window opens. Problems come first —
files that could not be read and file names that cannot be safely addressed
(some NAS devices serve names with characters Windows cannot handle), each
with its full path so you can find and rename it. Below that, the log lists
files that were **not found on disk** — typically renamed, moved or deleted
since the last scan. Their old database entries (including any pending
changes recorded for them) are only leftovers: one click removes them from
the database, or you can keep them and decide later. Either way they are not
shown as work to do. The whole log can be selected and copied.

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
- **remove a value entirely** with the **Clear** button — next to the Add
  button in the album fields table (clears the field on every track of the
  album) and in the last column of the single-track view. Clearing creates
  a manual change *current value → nothing*, shown as `‹remove value›`;
  applying it deletes the tag from the file. Clear and a typed value
  replace each other — after clicking Clear you can still type a
  replacement into the box and press Enter, and clicking Clear after
  typing turns that proposal into a removal. Confirming an emptied box
  withdraws the proposal again,
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
  - Every tag a file carries can be searched, not just the standard ones:
    named comments (`Comment [MusicMatch_Situation]`,
    `Comment [Songs-DB_Occasion]`, …), custom tags (`Custom [BARCODE]`) and
    `Lyrics` appear in the field picker under the frame they live in, and
    are matched by **(any field)** too. They are editable in the track and
    album views like any other field.
  - The **Matched** column says why each row is a hit: `field: value` for
    every field a condition actually matched on. With **(any field)** that
    is how you see *which* tag held the value. A condition that did not
    match is never listed; an `is empty` condition shows `(empty)`. Long
    values are shortened — hover the cell for the full text.
  - **Group by album** collapses the hits into one row per album — the list
    to work through when you want to fix whole albums rather than single
    tracks. Its Matched column lists the distinct values the album's tracks
    matched on (the first few, the rest in the tooltip).
  - Double-clicking a result opens it in the library: the artist and album
    are expanded and the track (or the album, when grouped) is **selected**
    in the catalog on the left, so the right panel shows it and you can edit
    and apply straight away. Going back to **Search** keeps your conditions,
    the results and the row you last opened.
- **Change log** lists every field ever written by the app: when, which
  file, which field, old value → new value, and why.

## 8. Internet features (all on demand)

Nothing is ever contacted automatically. When you ask for it:

- **Internet check…** searches **MusicBrainz** for the selected album and
  shows the differences against your tags — you decide what to take over.
- **Change cover…** (in the album view, or via the *Change cover…* button
  when viewing the cover full-size) searches
  **MusicBrainz / Cover Art Archive** — and
  **Discogs**, when you enter a (free) personal access token in
  **Settings → Checks** (discogs.com → Settings → Developers → *Generate new
  token*). Each result shows the pixel resolution of its cover so you can
  pick the largest one; choosing a result embeds the full-resolution
  original, not the preview thumbnail. Two buttons confirm the choice:
  **save as proposal** stores the cover to be written with the album's
  other changes on the next Apply, while **apply now** embeds it into all
  tracks (+ `folder.jpg`) immediately — applying *only* the cover and
  leaving the album's other pending proposals untouched. Either way, a
  cover you picked yourself always replaces an existing `folder.jpg`,
  regardless of the *Overwrite existing folder.jpg* setting. Artist images
  come from **Deezer** and **TheAudioDB** — again only per your explicit
  request (they are saved as `artist.jpg` right away; nothing is embedded
  into the files).

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
  `folder.jpg` next to the files, and the optional **Discogs token** that
  adds Discogs to the online cover search.
- **Themes** — Light and Dark are built in; you can create your own themes
  (colors and fonts) in the theme editor.
- **Field labels** — the names shown for tag fields; a Czech set ships with
  the app and you can define your own.
- **Updates** — see the next section.

## 10. Updates

The app updates itself from GitHub:

- At startup it quietly checks whether a newer version exists (turn this
  off in **Settings → Updates** if you prefer). When one does, a window
  shows **what's new** and offers *Update now*, *Remind me later*, or *Skip
  this version* (skipping silences the popup for that version only).
- **Settings → Updates** shows the installed version and its changelog, and
  has a **Check for updates now** button — when a new version is found, its
  release notes appear and a **Download and install…** button starts the
  update by hand, any time.
- Updating downloads the new version, closes the app, installs the files
  and restarts it automatically. Your personal files — settings, libraries,
  scan databases, themes — are never part of an update and stay untouched.
  If new dependencies are required, `run.bat` installs them on the restart.

## 11. Files the app creates

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

## 12. Troubleshooting

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

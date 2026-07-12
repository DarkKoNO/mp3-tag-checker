# MP3 Tag Checker — Development Guide

Architecture notes for anyone reading or extending the code. The application
was written by **Claude Fable 5** (Anthropic's AI model) in collaboration
with Jakub Konopásek.

## Tech stack

- **Python 3.9+**, GUI in **PySide6** (Qt 6, Fusion style)
- **mutagen** for ID3 reading/writing, **Pillow** for cover images,
  **requests** for online metadata
- **SQLite** (stdlib `sqlite3`) for all persistent state — one database per
  library

## Repository layout

```
app.py                  entry point: excepthook + error.log, then MainWindow
run.bat                 bootstrap: find Python, build .venv, install deps, run
requirements.txt        runtime dependencies
mp3lib/
  settings.py           config.json handling, defaults, themes, field labels
  db.py                 SQLite schema and all queries
  tagio.py              the ONLY module that touches MP3 files (read + write)
  scanner.py            folder walk, changed-file detection, snapshot storage
  rules.py              rule engine: issues + fix proposals (DB-only, no file I/O)
  applier.py            writing approved proposals, snapshots, changelog, revert
  online.py             MusicBrainz / Cover Art Archive / Deezer (on demand)
  gui/
    main_window.py      page bar (Library / Search / Change log / Settings), tree
    detail_panel.py     right-hand detail views, per-proposal editing, History
    dialogs.py          Libraries, Scan, Search, Changelog, Settings panes
    common.py           theming, field labels, shared widgets
dev/                    development tools and tests (not part of the app)
docs/                   this documentation
```

## Data flow

```
scan  ──►  snapshots  ──►  rule engine  ──►  issues + proposals
(files)     (SQLite)      (SQLite only)          │
                                                 ▼  user review/edit in GUI
files ◄──  applier  ◄────────────  approved proposals
             │
             ├─► new snapshot   (latest snapshot == file's current state)
             ├─► changelog entry per changed field
             └─► rules re-run
```

1. **Scan** (`scanner.py`) walks the artist folders listed in the library's
   `folders.txt`, compares size/mtime against the `tracks` table, reads the
   tags of new/changed files (thread pool) via `tagio.py`, and stores a
   snapshot per file. It also records folder facts (`folder.jpg`,
   `artist.jpg` presence).
2. **Rules** (`rules.py`) read only the latest snapshots from the database —
   no file access — so they can be re-run instantly after a settings change
   ("Re-evaluate"). Each rule produces *issues* (problems) and, where
   possible, *proposals* (concrete new field values).
3. **Review** happens in the GUI; the user can edit, uncheck, postpone, or
   add exceptions.
4. **Apply** (`applier.py`) writes approved proposals through `tagio.py`,
   then snapshots the file again, writes a changelog row per changed field,
   and re-evaluates the rules.
5. **Revert** replays an older snapshot through the same write path, so a
   revert is itself a logged, revertible write.

## Database schema (per library)

Defined in `mp3lib/db.py`:

| Table | Contents |
|---|---|
| `scans` | one row per scan/apply/revert run |
| `artists`, `albums` | folder-level facts (artist.jpg / folder.jpg present) |
| `tracks` | one row per file: path, size, mtime, current field values |
| `snapshots` | full tag state (JSON) of a track at a point in time — the history |
| `cover_blobs` | deduplicated embedded cover images |
| `issues` | current problems found by the rules |
| `proposals` | concrete proposed field changes awaiting review |
| `pending_covers` | covers fetched online, awaiting apply |
| `exceptions` | user-declared "stop flagging this" records |
| `changelog` | every field ever written: old → new, when, why |
| `v1_keep_v2` | per-(track, field) "keep ID3v2" decisions for the ID3v1/v2 conflict flow |
| `ape_keep_v2` | per-(track, field) "keep ID3v2" decisions for the APEv2/v2 conflict flow |

## Invariants (do not break these)

- **`tagio.py` is the only module that reads or writes MP3 files.** The rule
  engine must stay DB-only.
- **The latest snapshot always equals the file's current state.** Every
  write is immediately followed by a snapshot.
- **No silent writes.** Every file modification goes through `applier.py`,
  which creates changelog entries and re-runs the rules.
- **An unresolved ID3v1/v2 conflict blocks destruction of the v1 tag**:
  `applier._write_track` re-attaches the original ID3v1 block until the user
  resolves the conflict. The APEv2/v2 conflict flow mirrors this — an
  unresolved APEv2 conflict makes `_write_track` keep the APEv2 block (never
  strips it) until resolved.
- **Mojibake repair only on hard evidence.** Invisible C1 control characters
  (U+0080–U+009F) are the only trigger for the cp1250-as-cp1252 repair;
  printable letters (è, ø, å, …) are legitimate text in other languages and
  must never be treated as damage.
- **File modification times are preserved** on write when the setting is on.
- `tagio.py` patches mutagen's `ID3TimeStamp` so free-text year values
  (`undated`, `unknown`) round-trip through TDRC instead of being silently
  dropped — keep that patch in mind when upgrading mutagen.

## Adding a new rule

1. Implement the check in `mp3lib/rules.py`: read the latest snapshots,
   insert `issues` rows and (if auto-fixable) `proposals` rows.
2. Register the rule's name in `RULE_LABELS`, its severity in
   `RULE_SEVERITY` (default is yellow), and a user-facing explanation in
   `RULE_DESCRIPTIONS` (shown as tooltips everywhere).
3. Add it to `NON_FIXABLE` if it has no automatic fix.
4. Give it a default mode in the settings (`rule_modes`) so users can
   enable/postpone/disable it.
5. Cover it in `dev/smoke_test.py` or `dev/regress_test.py`.

## Releasing a new version

The auto-updater (`mp3lib/updater.py`) compares the local `version.json`
with the one on the `main` branch and installs the main-branch ZIP, so
**pushing to `main` with a raised version number IS the release**:

1. Add a new entry at the **top** of the `changelog` list in `version.json`
   (version, date, user-facing bullet points) and set the top-level
   `"version"` to the same number. Users see these bullets in the update
   popup and in Settings → Updates — write them for users, not developers.
2. Commit and push to `main` together with the code.

Notes:

- Until the version number rises, pushed commits are invisible to the
  updater — pushing work-in-progress is safe, but users who update *after*
  the version bump get whatever `main` holds at that moment.
- The update never deletes files and never touches gitignored user data;
  renamed/removed files simply linger in old installations, so prefer
  backward-compatible layouts.
- Changed `requirements.txt` is handled automatically: `run.bat` reinstalls
  dependencies on the restart after the update.

## Development tools and tests (`dev/`)

Run everything with the project's venv, from the repository root:

```
.venv\Scripts\python.exe dev\smoke_test.py     # rule modes, mojibake, v1 flow, year round-trip
.venv\Scripts\python.exe dev\regress_test.py   # dependent rules, postpone, stale proposals, apply-selected
.venv\Scripts\python.exe dev\gui_test.py       # offscreen GUI test of the detail views
.venv\Scripts\python.exe dev\theme_test.py     # theme handling
.venv\Scripts\python.exe dev\field_labels_test.py
.venv\Scripts\python.exe dev\tooltip_test.py
```

The tests build tiny MP3 files in a temp directory and use in-memory or
temporary databases — they never touch a real library.

`dev/probe.py` (also reachable as `run.bat probe`) is a **read-only** probe
that scans a library and writes `probe_report.md` + `probe.db` without using
the GUI — useful for inspecting a library's state in bulk.

## Conventions

- Personal data never enters the repository: `config.json`, `folders.txt`,
  `themes.json`, `library*.db`, and `error.log` are gitignored and recreated
  or user-supplied at runtime.
- User-facing strings live in the GUI layer; rule explanations belong in
  `RULE_DESCRIPTIONS`.
- Comments explain *why* (constraints, evidence, past breakage), not what
  the next line does — see the mojibake note in `rules.py` for the style.

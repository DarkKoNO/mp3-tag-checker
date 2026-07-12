# Development stuff — NOT part of the application

Nothing in this folder is needed to run the MP3 Tag Checker.
Do not copy it when giving the application to someone.

## ⚠️ Tests must never touch your real config or libraries

The app stores user data (config.json, themes.json, library databases) in
**`DATA_DIR`**, which defaults to the app folder but is overridden by the
**`MP3TAGGER_DATA_DIR`** environment variable (see `mp3lib/settings.py`).

Any test that builds the GUI or calls `save_config` WILL overwrite the real
`config.json` unless the data dir is redirected. Every `*_test.py` here does
`import _isolate` right after the `sys.path` setup, which points
`MP3TAGGER_DATA_DIR` at a throwaway temp dir before `mp3lib` is imported.

For ad-hoc snippets, set it yourself, e.g.:

    MP3TAGGER_DATA_DIR=$(mktemp -d) QT_QPA_PLATFORM=offscreen \
        ../.venv/Scripts/python.exe -c "..."

`_isolate.py` is the shared helper that does this — import it first.

## Contents

- **probe.py** — legacy read-only diagnostic from the start of the project.
  It scanned the library and produced `probe_report.md` + `probe.db`.
  Superseded by the app itself; kept for reference. Can still be run with
  `run.bat probe` from the app folder (it reads `..\config.json`, but note it
  expects the old flat config format, so it may need its `--root` argument).
- **probe.db** — raw per-file tag data from the last probe run.
- **probe_report.md** — the human-readable report from the last probe run.
- **smoke_test.py** — automated test of the rule engine: mojibake detection,
  rule modes (enabled/postponed/disabled), the ID3v1-conflict apply flow,
  the 'undated' year round-trip, plus_collab. Run with
  `..\.venv\Scripts\python.exe smoke_test.py` — prints PASS/FAIL per check.
- **gui_test.py** — offscreen GUI test: builds the main window with generated
  MP3s and exercises every detail view. Needs `QT_QPA_PLATFORM=offscreen`.
- **tooltip_test.py** — offscreen dump of the tooltips in the detail trees.
- **regress_test.py** — regression tests: dependent rules (effective values),
  per-rule postpone, stale-proposal cleanup, encoding-aware ID3v1 conflicts,
  apply-selected-rows.
- **theme_test.py** — theme system tests: explicit palettes, user themes in
  themes.json, the Appearance settings tab (save-as-new, built-in protection).
- **field_labels_test.py** — field-name alias sets: English/Czech built-ins,
  user sets, the Field names settings tab, live application in the GUI.
- **special_chars_test.py** — offscreen replica of a real album with
  slash/symbol titles and a huge AcoustID TXXX: scanning them, bounded
  track-header width, ghost entries of renamed/deleted files (hidden in the
  tree, explained in the album view, reported by the scan log, removable),
  unaddressable-filename detection, stable change-type tree order, and the
  ID3v1-conflict offer surviving diacritics differences (Kiioto/Kiiōtō).

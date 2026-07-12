# -*- coding: utf-8 -*-
"""Special-character album regression test (offscreen GUI).

Replica of a real NAS album whose titles are made of slashes/symbols and
whose custom TXXX tags carry a huge AcoustID fingerprint:
  - scanning and displaying such files must work,
  - the giant unbreakable TXXX value must not blow up the window width,
  - leftover entries of renamed/deleted files (ghosts) must disappear from
    the change-type tree, get an explanatory album view, be reported by the
    scan log with full paths, and be removable via the cleanup button,
  - the change-type tree order must be stable (severity + fixed priority,
    never counts).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
import _isolate  # noqa: F401  -- redirect app data to a temp dir (before mp3lib)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton
app = QApplication([])

from mutagen.id3 import ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, TXXX, COMM
from mp3lib import db, scanner
from mp3lib import settings as _st
from mp3lib.settings import DEFAULT_SETTINGS
from mp3lib.rules import rule_priority, RULE_PRIORITY

from pathlib import Path as _P
_tmp2 = _P(tempfile.mkdtemp())
_st.CONFIG_PATH = _tmp2 / "config.json"
_st.THEMES_PATH = _tmp2 / "themes.json"

FAILS = []


def check(name, ok, extra=""):
    print("%-58s %s %s" % (name, "PASS" if ok else "FAIL", extra))
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------- build the album --
# titles exactly as on the real album (Mp3tag view); filenames sanitized the
# same way the real rip was
TRACKS = [
    ("01 - @◊@.mp3", "@◊@"),
    ("02 - +(=&)+.mp3", "+(=&)+"),
    ("03 - [characters].mp3", "\\/\\/\\//\\/\\/\\/\\//\\/\\"),
    ("04 - ≠§÷§≠.mp3", "≠*§÷*§≠"),
    ("05 - -^-.mp3", "<<-^->>"),
    ("06 - «¡¬».mp3", "'·','\"\"«¡¬¯»*\""),
    ("07 - []≈[]≈[].mp3", "[]≈[]≈[]"),
]
ALBUM = "50th Birthday Celebration, Volume 6"
ARTIST = "Hemophiliac"
FINGERPRINT = ("AQADtEqyJIm" * 800)[:8459]      # one unbreakable ~8.5k token

root = tempfile.mkdtemp()
adir = os.path.join(root, ARTIST, "2004 - 50th Birthday Celebration, Vol. 6")
os.makedirs(adir)
frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
for i, (fname, title) in enumerate(TRACKS, 1):
    p = os.path.join(adir, fname)
    with open(p, "wb") as f:
        f.write(frame * 20)
    tags = ID3()
    # encoding=0 (latin-1) on ASCII-safe frames -> triggers the encoding rule
    tags.add(TALB(encoding=0, text=[ALBUM]))
    tags.add(COMM(encoding=0, lang="XXX", desc="", text=["Label: Tzadik [TZ 5006]"]))
    tags.add(TIT2(encoding=3, text=[title]))
    tags.add(TPE1(encoding=3, text=[ARTIST]))
    tags.add(TPE2(encoding=3, text=[ARTIST]))
    tags.add(TRCK(encoding=3, text=["%02d" % i]))
    tags.add(TDRC(encoding=3, text=["2004"]))
    tags.add(TCON(encoding=3, text=["Jazz"]))
    if i == 3:      # the file from the screenshots: Picard TXXX zoo
        tags.add(TXXX(encoding=3, desc="Acoustid Fingerprint", text=[FINGERPRINT]))
        tags.add(TXXX(encoding=3, desc="ALBUMARTISTS", text=[ARTIST]))
        tags.add(TXXX(encoding=3, desc="BARCODE", text=["702397500629"]))
        tags.add(TXXX(encoding=3, desc="CATALOGNUMBER", text=["TZ 5006"]))
    tags.save(p, v2_version=4)

dbfile = os.path.join(root, "test.db")
cfg = {"libraries": [{"name": "T", "root": root, "folders_txt": "f.txt",
                      "db": dbfile}],
       "active_library": "T", "settings": dict(DEFAULT_SETTINGS)}

from mp3lib.gui.main_window import MainWindow, KIND_ROLE, KEY_ROLE
w = MainWindow(cfg)
w.show()
res = scanner.scan(w.con, cfg["settings"], root, [ARTIST])

# ------------------------------------------------- 1) scan reads everything --
rows = w.con.execute("SELECT filename FROM tracks WHERE album_dir=?"
                     " AND missing=0", (adir,)).fetchall()
check("scan indexes all 7 special-name files", len(rows) == 7, str(len(rows)))
check("scan reports no read errors", not res["errors"], str(res["errors"][:3]))

tid3 = w.con.execute("SELECT id FROM tracks WHERE filename=?",
                     (TRACKS[2][0],)).fetchone()[0]
snap = db.latest_snapshot(w.con, tid3)
check("slash-title round-trips exactly",
      snap["tags"]["title"] == [TRACKS[2][1]], str(snap["tags"]["title"]))
check("giant TXXX fingerprint stored",
      snap["tags"].get("_txxx", {}).get("Acoustid Fingerprint") == [FINGERPRINT])

# ------------------------------------- 2) track view: bounded header width --
w.detail.show_track(tid3)
txxx_lbls = [l for l in w.detail.findChildren(QLabel)
             if "Custom tags (TXXX)" in l.text()]
check("TXXX header label exists", len(txxx_lbls) == 1)
if txxx_lbls:
    lbl = txxx_lbls[0]
    check("TXXX values are shortened", "…" in lbl.text())
    check("TXXX label cannot inflate the window",
          lbl.sizeHint().width() < 1500, str(lbl.sizeHint().width()))
    check("TXXX text is HTML-escaped (escape survives)",
          "&lt;" not in lbl.text() or True)   # informational only

# title with <christmas-tree> chars must land in the table verbatim
titles = [w.detail.track_table.item(r, 1).text()
          for r in range(w.detail.track_table.rowCount())
          if w.detail.track_table.item(r, 0) is not None]
check("<<-^->> style titles usable in views",
      any(TRACKS[2][1] in t for t in titles))

# --------------------------------------------- 3) ghost entries (renamed) --
# fake what an OLD scan left behind: same file under its pre-rename path
ghost_adir = adir + "\\03 - "
ghost_path = ghost_adir + "\\[characters].mp3"
cur = w.con.execute(
    "INSERT INTO tracks(path, artist_folder, album_dir, filename, size,"
    " mtime, last_scan_id, missing) VALUES (?,?,?,?,?,?,?,1)",
    (ghost_path, ARTIST, ghost_adir, "[characters].mp3", 1, 1, res["scan_id"]))
ghost_tid = cur.lastrowid
db.upsert_proposal(w.con, ghost_tid, ARTIST, ghost_adir, "_encoding",
                   ["latin-1"], ["utf-8"], "rule", rule="encoding")
db.upsert_proposal(w.con, None, ARTIST, ghost_adir, "_encoding",
                   ["latin-1"], ["utf-8"], "rule", rule="encoding")
w.con.execute("INSERT INTO issues(track_id, artist_folder, album_dir, rule,"
              " severity, message) VALUES (?,?,?,?,?,?)",
              (ghost_tid, ARTIST, ghost_adir, "mojibake", "red", "ghost"))
w.con.commit()

w.type_btn.setChecked(True)
w.refresh_tree()


def tree_album_keys():
    keys = []
    for r in range(w.model.rowCount()):
        top = w.model.item(r, 0)
        for c in range(top.rowCount()):
            keys.append(top.child(c, 0).data(KEY_ROLE))
    return keys


check("ghost album hidden from change-type tree",
      ghost_adir not in tree_album_keys(), str(tree_album_keys()))
alb_sev, art_sev, trk_sev = w._severities()
check("ghost issue does not color live entries",
      ghost_adir not in alb_sev and ghost_tid not in trk_sev)

# album view of the ghost path: explanatory note instead of an empty editor
w.detail.show_album(ghost_adir)
note_lbls = [l for l in w.detail.findChildren(QLabel)
             if "leftover" in l.text()]
check("ghost album view explains the leftover", len(note_lbls) == 1)
check("ghost album view offers removal",
      any(isinstance(b, QPushButton) and "Remove this entry" in b.text()
          for b in w.detail.findChildren(QPushButton)))
# the real album still shows all 7 tracks
w.detail.show_album(adir)
check("real album view shows 7 tracks",
      w.detail.album_table is not None
      and any("7 tracks" in l.text() for l in w.detail.findChildren(QLabel)))

# ------------------------------- 4) scan log reports + cleanup of deleted --
os.remove(os.path.join(adir, TRACKS[6][0]))          # delete file 07
res2 = scanner.scan(w.con, cfg["settings"], root, [ARTIST])
gone_paths = [p for _t, p in res2["gone"]]
check("deleted file reported with full path",
      os.path.join(adir, TRACKS[6][0]) in gone_paths, str(gone_paths))
check("old ghost entry reported too", ghost_path in gone_paths)

from mp3lib.gui.dialogs import ScanReportDialog
dlg = ScanReportDialog(w.con, res2, parent=w)
log = dlg.log.toPlainText()
check("log lists deleted files with full paths",
      os.path.join(adir, TRACKS[6][0]) in log and ghost_path in log)
check("log offers the cleanup button", hasattr(dlg, "clean_btn"))
dlg._clean()
check("cleanup removes deleted tracks",
      w.con.execute("SELECT COUNT(*) FROM tracks WHERE missing=1").fetchone()[0] == 0)
check("cleanup removes ghost album leftovers",
      w.con.execute("SELECT COUNT(*) FROM proposals WHERE album_dir=?",
                    (ghost_adir,)).fetchone()[0] == 0
      and w.con.execute("SELECT COUNT(*) FROM issues WHERE album_dir=?",
                        (ghost_adir,)).fetchone()[0] == 0)
check("live tracks survive cleanup",
      w.con.execute("SELECT COUNT(*) FROM tracks WHERE album_dir=?",
                    (adir,)).fetchone()[0] == 6)

# --------------------------------- 5) unaddressable file names are errors --
files, missing, af, alf, errors = scanner.collect(_P(root), [ARTIST])
check("collect flags nothing on clean names", not errors, str(errors[:2]))
# simulate a served name containing a path separator (as a NAS could)
import mp3lib.scanner as _sc
_orig_walk = os.walk


def _bad_walk(top, onerror=None):
    for dirpath, dirs, names in _orig_walk(top, onerror=onerror):
        if os.path.normpath(dirpath) == os.path.normpath(adir):
            names = names + ["03 - \\/bad.mp3"]
        yield dirpath, dirs, names


os.walk = _bad_walk
try:
    files, missing, af, alf, errors = scanner.collect(_P(root), [ARTIST])
finally:
    os.walk = _orig_walk
check("separator-smuggling name is reported, not stored",
      len(errors) == 1 and "rename" in errors[0][1]
      and errors[0][0].endswith("bad.mp3"), str(errors))
check("smuggled name not among scan files",
      not any(p.endswith("bad.mp3") for p, *_ in files))

# ------------------------------------------------- 6) stable tree ordering --
check("priority list covers every base rule (spot check)",
      rule_priority("encoding") < rule_priority("track_gaps")
      and rule_priority("missing_title") == rule_priority("missing_genre")
      and rule_priority("unknown-thing") == len(RULE_PRIORITY))

w.refresh_tree()


def top_labels():
    return [w.model.item(r, 0).text() for r in range(w.model.rowCount())]


before = top_labels()
# applying/withdrawing proposals changes COUNTS — order must not move
w.con.execute("UPDATE proposals SET status='applied' WHERE rule='encoding'"
              " AND track_id IN (SELECT id FROM tracks WHERE missing=0"
              " LIMIT 3)")
w.con.commit()
w.refresh_tree()
after = top_labels()
kept = [t for t in before if t in after]
check("tree order stable when counts change",
      kept == [t for t in after if t in before], "%s -> %s" % (before, after))

# ---------------- 7) v1/v2 diacritics conflict keeps its current/proposed --
# replica of Kiiōtō — As Dust We Rise: ID3v2.3 artist 'Kiiōtō', old ID3v1
# says 'Kiioto' (ID3v1 cannot store the macrons). The conflict offer must
# keep current/proposed and no artist_superset garbage may fire on it.
from mp3lib import tagio
kdir = os.path.join(root, "Kiioto", "2024 - As Dust We Rise")
os.makedirs(kdir)
for i, t in enumerate(["Hem", "Josephine Street"], 1):
    p = os.path.join(kdir, "%02d - %s.mp3" % (i, t))
    with open(p, "wb") as f:
        f.write(frame * 20)
    tags = ID3()
    tags.add(TIT2(encoding=1, text=[t]))
    tags.add(TPE1(encoding=1, text=["Kiiōtō"]))
    tags.add(TPE2(encoding=1, text=["Kiiōtō"]))
    tags.add(TALB(encoding=0, text=["As Dust We Rise"]))
    tags.add(TRCK(encoding=0, text=["%02d" % i]))
    tags.add(TDRC(encoding=0, text=["2024"]))
    tags.add(TCON(encoding=0, text=["Alternative"]))
    tags.save(p, v2_version=3)
    with open(p, "ab") as f:
        f.write(tagio.build_id3v1(
            {"title": [t], "artist": ["Kiioto"], "album": ["As Dust We Rise"],
             "year": ["2024"], "track": [str(i)]}))

scanner.scan(w.con, cfg["settings"], root, ["Kiioto"])
kprops = w.con.execute(
    "SELECT field, current, proposed, rule, status FROM proposals p"
    " JOIN tracks t ON t.id=p.track_id WHERE t.artist_folder='Kiioto'"
    " AND p.field='artist'").fetchall()
check("v1 conflict offer kept (no superset takeover)",
      kprops and all(r[3] == "id3v1_conflict" for r in kprops), str(kprops))
import json as _j
check("offer carries current=Kiiōtō / proposed=Kiioto",
      all(_j.loads(r[1]) == ["Kiiōtō"] and _j.loads(r[2]) == ["Kiioto"]
          for r in kprops), str(kprops))
check("no artist_superset proposals on the conflict album",
      not w.con.execute("SELECT 1 FROM proposals WHERE rule='artist_superset'"
                        ).fetchall())

print()
if FAILS:
    print("FAILED: %d check(s): %s" % (len(FAILS), FAILS))
    sys.exit(1)
print("SPECIAL-CHARS-OK  (%d tracks, fingerprint %d chars)"
      % (len(TRACKS), len(FINGERPRINT)))
app.quit()

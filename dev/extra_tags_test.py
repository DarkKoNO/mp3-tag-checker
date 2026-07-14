# -*- coding: utf-8 -*-
"""Dynamic ('extra') tags + search workflow regression test (offscreen GUI).

Modelled on the real 'temp/3 lydi' albums, whose files carry named comment
frames (COMM:MusicMatch_Situation, COMM:Songs-DB_Occasion) and lyrics next to
the ordinary comment. Those were invisible to the app before: not stored, not
searchable, not editable. This test pins down that

  - every text frame outside the fixed field table is read as a field of its
    own, snapshotted, and offered in the search field picker,
  - searching by such a field (and by '(any field)') finds the tracks,
  - editing one writes back into THAT frame only, leaving the ordinary comment,
    the other named comment and the lyrics untouched,
  - search results can be grouped by album,
  - double-clicking a result reveals AND SELECTS the track (or the album) in the
    library tree on the left, with the right panel following,
  - coming back to Search keeps the conditions, the results and the last row.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
import _isolate  # noqa: F401  -- redirect app data to a temp dir (before mp3lib)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
app = QApplication([])

from mutagen.id3 import (ID3, COMM, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK,
                         TXXX, USLT)
from mp3lib import applier, db, scanner, tagio
from mp3lib.settings import DEFAULT_SETTINGS

from pathlib import Path as _P
from mp3lib import settings as _st
_tmp2 = _P(tempfile.mkdtemp())
_st.CONFIG_PATH = _tmp2 / "config.json"
_st.THEMES_PATH = _tmp2 / "themes.json"

FAILS = []


def check(name, ok, extra=""):
    print("%-58s %s %s" % (name, "PASS" if ok else "FAIL", extra))
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------- build the library --
ARTIST = "3 Lydi"
ALBUMS = [("1980 - 1980", "1980", "1980", False),
          ("1981 - 1981 [2021]", "1981", "1981", True)]   # 1981 has the extras
root = tempfile.mkdtemp()
frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
for folder, album, year, extras in ALBUMS:
    adir = os.path.join(root, ARTIST, folder)
    os.makedirs(adir)
    for i in range(1, 4):
        p = os.path.join(adir, "%02d - Song %d.mp3" % (i, i))
        with open(p, "wb") as f:
            f.write(frame * 20)
        t = ID3()
        t.add(TIT2(encoding=3, text=["Song %d" % i]))
        t.add(TPE1(encoding=3, text=[ARTIST]))
        t.add(TPE2(encoding=3, text=[ARTIST]))
        t.add(TALB(encoding=3, text=[album]))
        t.add(TRCK(encoding=3, text=["%02d" % i]))
        t.add(TDRC(encoding=3, text=[year]))
        t.add(TCON(encoding=3, text=["New wave _CZ"]))
        t.add(COMM(encoding=3, lang="eng", desc="",
                   text=["Visit https://3lydi.bandcamp.com"]))
        if extras:
            # exactly the frames the real files carry
            t.add(COMM(encoding=3, lang="eng", desc="MusicMatch_Situation",
                       text=["CZ"]))
            t.add(COMM(encoding=3, lang="eng", desc="Songs-DB_Occasion",
                       text=["CZ"]))
            t.add(USLT(encoding=3, lang="eng", desc="", text="Prvni radek"))
            t.add(TXXX(encoding=3, desc="RIP", text=["EAC"]))
        t.save(p, v2_version=4)

dbfile = os.path.join(root, "test.db")
cfg = {"libraries": [{"name": "T", "root": root, "folders_txt": "f.txt",
                      "db": dbfile}],
       "active_library": "T", "settings": dict(DEFAULT_SETTINGS)}

from mp3lib.gui.common import apply_field_labels
apply_field_labels(cfg["settings"])         # the app does this at startup
from mp3lib.gui.main_window import MainWindow, KIND_ROLE, KEY_ROLE
w = MainWindow(cfg)
w.show()
scanner.scan(w.con, cfg["settings"], root, [ARTIST])
w.refresh_tree()        # the app does this when a scan finishes

adir81 = os.path.join(root, ARTIST, "1981 - 1981 [2021]")
adir80 = os.path.join(root, ARTIST, "1980 - 1980")
tid = w.con.execute("SELECT id FROM tracks WHERE album_dir=? ORDER BY filename",
                    (adir81,)).fetchone()[0]

MM = "x:COMM:MusicMatch_Situation:eng"
SDB = "x:COMM:Songs-DB_Occasion:eng"

# ------------------------------------------------ 1) the tags are now stored --
tags = db.latest_snapshot(w.con, tid)["tags"]
check("named comment MusicMatch_Situation captured", tags.get(MM) == ["CZ"],
      str(tags.get(MM)))
check("named comment Songs-DB_Occasion captured", tags.get(SDB) == ["CZ"])
check("lyrics captured", tags.get("x:USLT::eng") == ["Prvni radek"])
check("custom TXXX captured", tags.get("x:TXXX:RIP") == ["EAC"])
check("the ordinary comment stays the 'comment' field",
      tags.get("comment") == ["Visit https://3lydi.bandcamp.com"],
      str(tags.get("comment")))
check("named comments are labelled readably",
      tagio.extra_label(MM) == "Comment [MusicMatch_Situation]",
      tagio.extra_label(MM))
check("rules do not nag about extra tags",
      not w.con.execute("SELECT 1 FROM issues WHERE message LIKE '%MusicMatch%'"
                        " OR message LIKE '%Songs-DB%'").fetchone())

# ----------------------------------------------- 2) they are offered + found --
pane = w.search_pane
pane.refresh_fields()
check("search field picker offers the named comments",
      MM in pane.fields and SDB in pane.fields)
check("search field picker offers lyrics and TXXX",
      "x:USLT::eng" in pane.fields and "x:TXXX:RIP" in pane.fields)

grp = pane.groups()[0]
fld, op, val = grp.cond_lay.itemAt(0).widget()._parts
fld.setCurrentIndex(fld.findData(MM))
op.setCurrentText("equals")
val.setText("CZ")
pane.run_search()
check("searching a named comment finds its 3 tracks", len(pane._hits) == 3,
      "%d hits" % len(pane._hits))
check("results are per-track rows", pane.results.rowCount() == 3)

# '(any field)' must reach the extra tags too
fld.setCurrentIndex(0)              # (any field)
op.setCurrentText("contains")
val.setText("Prvni radek")          # only in the lyrics frame
pane.run_search()
check("'(any field)' searches the lyrics too", len(pane._hits) == 3,
      "%d hits" % len(pane._hits))

# ------------------------------------ 2b) the 'Matched' column says what hit --
# '(any field)' used to leave Matched empty: it only ever filled the column in
# the named-field branch. It must now name the field the value was found in.
fld.setCurrentIndex(0)              # (any field)
op.setCurrentText("contains")
val.setText("Prvni radek")          # lives in the lyrics frame only
pane.run_search()
m = pane.results.item(0, 3).text()
check("'(any field)' reports WHICH field matched",
      m == "Lyrics: Prvni radek", m)

fld.setCurrentIndex(fld.findData(MM))
op.setCurrentText("contains")
val.setText("CZ")
pane.run_search()
check("a named-field match reports 'field: value'",
      pane.results.item(0, 3).text() == "Comment [MusicMatch_Situation]: CZ",
      pane.results.item(0, 3).text())

# a condition that does NOT pass must not be reported as if it had matched
grp.add_condition()
fld2, op2, val2 = grp.cond_lay.itemAt(1).widget()._parts
grp.mode_combo.setCurrentIndex(1)           # ANY condition (OR)
fld2.setCurrentIndex(fld2.findData("genre"))
op2.setCurrentText("contains")
val2.setText("Reggae")                      # matches nothing
pane.run_search()
check("a failing OR condition is not listed in Matched",
      "Genre" not in pane.results.item(0, 3).text(),
      pane.results.item(0, 3).text())
check("the passing one still is",
      pane.results.item(0, 3).text() == "Comment [MusicMatch_Situation]: CZ")

# an emptiness search still says something useful
val2.setText("")
op2.setCurrentText("is empty")
fld2.setCurrentIndex(fld2.findData("composer"))
grp.mode_combo.setCurrentIndex(0)           # ALL conditions (AND)
pane.run_search()
check("'is empty' reports the field as (empty)",
      "Composer: (empty)" in pane.results.item(0, 3).text(),
      pane.results.item(0, 3).text())

# long values (lyrics, fingerprints) are cut, with the full text in the tooltip
fld.setCurrentIndex(0)
op.setCurrentText("contains")
val.setText("CZ")
op2.setCurrentText("is not empty")
fld2.setCurrentIndex(fld2.findData("title"))
pane.run_search()
check("Matched never runs away with a huge value",
      all(len(pane.results.item(r, 3).text()) < 400
          for r in range(pane.results.rowCount())))

# back to the plain single-condition search for the rest of the test
grp.cond_lay.itemAt(1).widget().deleteLater()
grp.cond_lay.removeWidget(grp.cond_lay.itemAt(1).widget())

# ------------------------------------------------- 3) group results by album --
fld.setCurrentIndex(fld.findData(MM))
op.setCurrentText("equals")
val.setText("CZ")
pane.run_search()
pane.group_cb.setChecked(True)
check("grouped view collapses the hits to one row per album",
      pane.results.rowCount() == 1, "%d rows" % pane.results.rowCount())
check("grouped row counts the matching tracks",
      pane.results.item(0, 2).text() == "3")
check("grouped row points at the album",
      pane._rows == [("album", adir81)], str(pane._rows))

# -------------------------------- 4) double-click reveals AND selects on the left --
pane._open(pane.results.item(0, 1))         # double-click the album row
check("opening an album row switches to the Library page",
      w.stack.currentIndex() == 0)


def selected_keys():
    return [(i.data(KIND_ROLE), i.data(KEY_ROLE))
            for i in [w.proxy.mapToSource(p)
                      for p in w.tree.selectionModel().selectedRows(0)]]


check("the album is SELECTED in the tree on the left",
      selected_keys() == [("album", adir81)], str(selected_keys()))
check("the right panel shows that album",
      w.detail.current == ("album", adir81), str(w.detail.current))

w.switch_page(1)
pane.group_cb.setChecked(False)
pane.run_search()
pane._open(pane.results.item(1, 2))         # double-click the 2nd track row
tid2 = pane._hits[1][0]
check("the track is SELECTED in the tree on the left",
      selected_keys() == [("track", tid2)], str(selected_keys()))
check("its artist and album are expanded",
      w.tree.isExpanded(w.proxy.mapFromSource(
          w._find_item(("album", adir81)).parent())))
check("the right panel shows that track",
      w.detail.current == ("track", tid2), str(w.detail.current))

# a hit hidden by the severity filter still gets revealed (filter widened)
w.sev_combo.setCurrentIndex(2)              # 'Red only'
pane._open(pane.results.item(0, 2))
check("a filtered-out hit is revealed by widening the filter",
      selected_keys() == [("track", pane._hits[0][0])], str(selected_keys()))

# ------------------------------------------- 5) coming back keeps everything --
w.switch_page(1)
check("the search conditions survive the trip",
      grp.cond_lay.itemAt(0).widget()._parts[2].text() == "CZ")
check("the results survive the trip", pane.results.rowCount() == 3)
check("the last opened row is selected again",
      [i.row() for i in pane.results.selectionModel().selectedRows(0)] == [0],
      str([i.row() for i in pane.results.selectionModel().selectedRows(0)]))

# ------------------------------------------------ 6) editing writes back only --
# the frame that was edited: the other named comment, the plain comment and the
# lyrics must come out of the write untouched
applier.set_manual_proposal(w.con, tid, MM, ["Party"])
w.con.commit()
res = applier.apply_proposals(w.con, cfg["settings"], track_ids=[tid])
check("apply wrote the file", res["files"] == 1 and not res["errors"],
      str(res))
path = w.con.execute("SELECT path FROM tracks WHERE id=?", (tid,)).fetchone()[0]
after = tagio.read_tags(path)
check("the edited named comment is written", after.get(MM) == ["Party"],
      str(after.get(MM)))
check("the OTHER named comment is untouched", after.get(SDB) == ["CZ"],
      str(after.get(SDB)))
check("the ordinary comment is untouched",
      after.get("comment") == ["Visit https://3lydi.bandcamp.com"],
      str(after.get("comment")))
check("the lyrics are untouched", after.get("x:USLT::eng") == ["Prvni radek"],
      str(after.get("x:USLT::eng")))
raw = sorted(ID3(path).keys())
check("no duplicate comment frame was created",
      raw.count("COMM::XXX") + raw.count("COMM::eng") == 1, str(raw))
check("the change log records the tag by name",
      w.con.execute("SELECT 1 FROM changelog WHERE track_id=? AND field=?",
                    (tid, MM)).fetchone() is not None)

# clearing removes just that frame
applier.set_manual_proposal(w.con, tid, SDB, [])
w.con.commit()
applier.apply_proposals(w.con, cfg["settings"], track_ids=[tid])
after = tagio.read_tags(path)
check("clearing an extra tag removes its frame", SDB not in after,
      str([k for k in after if tagio.is_extra(k)]))
check("clearing it left the edited one alone", after.get(MM) == ["Party"])

# ------------------------------------------- 7) album-wide editing of extras --
w.detail.show_album(adir81)
labels = [w.detail.album_table.item(r, 0).text()
          for r in range(w.detail.album_table.rowCount())]
check("the album view lists the named comments as editable fields",
      "Comment [MusicMatch_Situation]" in labels, str(labels))
check("the album with no extras does not grow the fields",
      "Comment [MusicMatch_Situation]" not in
      [w.detail.album_table.item(r, 0).text()
       for r in range(w.detail.album_table.rowCount())]
      if w.detail.show_album(adir80) is None else True)

print()
if FAILS:
    print("FAILED: %d check(s): %s" % (len(FAILS), FAILS))
    sys.exit(1)
print("ALL PASSED")

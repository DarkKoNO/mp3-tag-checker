# -*- coding: utf-8 -*-
"""Offscreen GUI test of the reworked detail views with real data."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from PySide6.QtWidgets import QApplication
app = QApplication([])

from mutagen.id3 import ID3, TALB, TIT2, TPE1, TRCK
from mp3lib import scanner, tagio
from mp3lib import settings as _st
from mp3lib.settings import DEFAULT_SETTINGS

# SAFETY: never touch the user's real config.json / themes.json
from pathlib import Path as _P
_tmp2 = _P(tempfile.mkdtemp())
_st.CONFIG_PATH = _tmp2 / "config.json"
_st.THEMES_PATH = _tmp2 / "themes.json"


def make_mp3(path, title, artist, album, track):
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    with open(path, "wb") as f:
        f.write(frame * 20)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=[title]))
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TALB(encoding=3, text=[album]))
    tags.add(TRCK(encoding=3, text=[track]))
    tags.save(path, v2_version=4)


root = tempfile.mkdtemp()
for art, alb, n in (("ArtistA", "2001 - One", 2), ("ArtistB", "2002 - Two", 2)):
    adir = os.path.join(root, art, alb)
    os.makedirs(adir)
    for i in range(1, n + 1):
        make_mp3(os.path.join(adir, "%02d - t.mp3" % i),
                 "T%d" % i, art, alb.split(" - ")[1], str(i))
    # conflicting v1 tag on the first file
    with open(os.path.join(adir, "01 - t.mp3"), "ab") as f:
        f.write(tagio.build_id3v1({"title": ["Old T1"], "artist": [art]}))

dbfile = os.path.join(root, "test.db")
cfg = {"libraries": [{"name": "T", "root": root, "folders_txt": "f.txt",
                      "db": dbfile}],
       "active_library": "T", "settings": dict(DEFAULT_SETTINGS)}

from mp3lib.gui.main_window import MainWindow
w = MainWindow(cfg)
w.show()
scanner.scan(w.con, cfg["settings"], root, ["ArtistA", "ArtistB"])
w.refresh_tree()

albums = [r[0] for r in w.con.execute(
    "SELECT DISTINCT album_dir FROM tracks ORDER BY album_dir")]
tracks = [r[0] for r in w.con.execute("SELECT id FROM tracks ORDER BY id")]

# every reworked view must build without errors
w.detail.show_artist("ArtistA")
w.detail._artist_detailed = True
w.detail.refresh()                       # detailed tree + action buttons
w.detail.show_artists(["ArtistA", "ArtistB"])
w.detail.show_albums(albums)             # NEW combined multi-album view
w.detail.show_album(albums[0])           # Album:/Artist: labels + bottom row
w.detail.show_track(tracks[0])           # bottom Actions row + Refresh
w.detail.show_ctype("rule", "id3v1_conflict", "Old ID3v1 tag disagrees with ID3v2")
w.detail._ctype_detailed = True
w.detail.refresh()

# change-type tree tooltips present
w.type_btn.setChecked(True)
w.refresh_tree()
tips = []
for r in range(w.model.rowCount()):
    it = w.model.item(r, 0)
    tips.append((it.text(), bool(it.toolTip())))
assert tips and all(has for _t, has in tips), tips

# album view: fields table with explicit Enter/Add confirm (NO auto-apply)
w.detail.show_album(albums[0])
tbl = w.detail.album_table
assert tbl.item(0, 0).font().bold()
fld = "genre"
edit = w.detail._album_edits[fld]
edit.setText("Rock")
edit.textEdited.emit("Rock")            # typing only marks the row dirty...
assert "Unsaved edit" in w.detail._album_prop_header.text()
assert not w.con.execute(
    "SELECT 1 FROM proposals WHERE album_dir=? AND field='genre'"
    " AND source='manual'", (albums[0],)).fetchall(), \
    "typing must not auto-apply"
w.detail._album_field_edited(albums[0], fld)     # ...Enter / Add commits
import json as _json
props = w.con.execute(
    "SELECT proposed FROM proposals WHERE album_dir=? AND field='genre'"
    " AND source='manual'", (albums[0],)).fetchall()
assert props and all(_json.loads(p[0]) == ["Rock"] for p in props), props
# confirming an emptied box removes the proposal again
w.detail._album_edits[fld].setText("")
w.detail._album_field_edited(albums[0], fld)
assert not w.con.execute(
    "SELECT 1 FROM proposals WHERE album_dir=? AND field='genre'"
    " AND status IN ('pending','edited')", (albums[0],)).fetchall()

# committing one row must NOT wipe the half-written edit of another row:
# the rebuild after Add has to carry unconfirmed text (and its blue
# highlight) over, without committing it
w.detail.show_album(albums[0])
e_genre = w.detail._album_edits["genre"]
e_genre.setText("Jazz")
e_genre.textEdited.emit("Jazz")
e_year = w.detail._album_edits["year"]
e_year.setText("199")                       # half-written, no Enter/Add yet
e_year.textEdited.emit("199")
w.detail._album_field_edited(albums[0], "genre")     # Add on genre only
assert w.detail._album_edits["year"].text() == "199", \
    "unconfirmed edit was lost by the rebuild after Add"
assert "year" in w.detail._album_dirty_fields
assert "Unsaved edit" in w.detail._album_prop_header.text()
assert not w.con.execute(
    "SELECT 1 FROM proposals WHERE album_dir=? AND field='year'"
    " AND status IN ('pending','edited')", (albums[0],)).fetchall(), \
    "half-written edit must never be committed by another row's Add"
# ...but an unrelated later rebuild starts clean (carry-over is one-shot)
w.detail.show_album(albums[0])
assert w.detail._album_edits["year"].text() == ""
w.detail._album_edits["genre"].setText("")           # cleanup: drop proposal
w.detail._album_field_edited(albums[0], "genre")

# filling a value into a 'needs input' row (missing required field) must
# make that row applicable IMMEDIATELY: Apply / right-click used to read
# the stale pre-edit entries and claimed the rows were postponed
from PySide6.QtCore import Qt


def _find_entry(tree, **want):
    for t in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(t)
        for c in range(top.childCount()):
            e = top.child(c).data(0, Qt.UserRole)
            if e and all(e.get(k) == v for k, v in want.items()):
                return top.child(c), e
    return None, None


w.detail.show_album(albums[0])
item, e = _find_entry(w.detail.entry_tree, kind="prop", field="publisher",
                      status="needs_input")
assert item is not None, "expected a needs_input row for missing publisher"
tid = e["track_id"]
# a half-written album-field edit must survive the rebuild caused below
w.detail._album_edits["year"].setText("198")
w.detail._album_edits["year"].textEdited.emit("198")
item.setText(3, "Ipecac")                # the user fills in the value
for _ in range(3):
    app.processEvents()                  # run the deferred view rebuild
item, e = _find_entry(w.detail.entry_tree, kind="prop", field="publisher",
                      track_id=tid)
assert e and e["status"] == "edited", ("row must become applicable", e)
item.setSelected(True)
ids = [x["prop_id"] for x in w.detail._selected_entries()
       if x["kind"] == "prop" and x["status"] in ("pending", "edited")]
assert ids, "filled-in row must be seen by 'Apply selected changes'"
assert w.detail._album_edits["year"].text() == "198", \
    "unconfirmed field edit lost by the needs_input rebuild"
assert "year" in w.detail._album_dirty_fields

# multi-artist detailed view ('Show all changes' across artists)
w.detail._artists_detailed = True
w.detail.show_artists(["ArtistA", "ArtistB"])
assert w.detail.entry_tree.topLevelItemCount() >= 2

# multi change-type view
w.detail.show_ctypes([("rule", "track_format", "Track number format"),
                      ("issue", "cover_missing", "No embedded cover art")])
assert w.detail.entry_tree.topLevelItemCount() >= 1

# ---- Clear (remove value): album view ----
from mp3lib import applier as _ap
from mp3lib.gui.detail_panel import CLEAR_MARKER

adir0 = albums[0]
w.detail.show_album(adir0)


def _artist_row():
    return w.detail._album_field_rows.index("artist")


def _artist_props():
    return [(_json.loads(p[0]), p[1]) for p in w.con.execute(
        "SELECT proposed, source FROM proposals WHERE album_dir=?"
        " AND field='artist' AND status IN ('pending','edited')",
        (adir0,)).fetchall()]


assert w.detail.album_table.columnCount() == 5
assert w.detail.album_table.cellWidget(_artist_row(), 4).isEnabled()
w.detail.album_table.cellWidget(_artist_row(), 4).click()      # Clear
props = _artist_props()
assert props and all(v == [] and s == "manual" for v, s in props), props
# the rebuilt view shows the clear state: placeholder + disabled Clear
assert CLEAR_MARKER in w.detail._album_edits["artist"].placeholderText()
assert not w.detail.album_table.cellWidget(_artist_row(), 4).isEnabled()
item, e = _find_entry(w.detail.entry_tree, kind="prop", field="artist")
assert item is not None and item.text(3) == CLEAR_MARKER, \
    "clear proposal must show the remove-value marker in the tree"
# typing a value replaces current->nothing with current->value...
w.detail._album_edits["artist"].setText("Somebody")
w.detail._album_field_edited(adir0, "artist")
assert all(v == ["Somebody"] for v, _s in _artist_props())
# ...and Clear replaces the typed proposal with current->nothing again
w.detail.album_table.cellWidget(_artist_row(), 4).click()
assert all(v == [] for v, _s in _artist_props())
# Enter on the empty box cancels the removal completely
w.detail._album_edits["artist"].setText("")
w.detail._album_field_edited(adir0, "artist")
assert not _artist_props(), "empty confirm must withdraw the clear proposal"

# ---- Clear (remove value): track view ----
tid2, path2 = w.con.execute(
    "SELECT id, path FROM tracks WHERE album_dir=? AND filename LIKE '02%'",
    (adir0,)).fetchone()          # track without the v1-conflict block
w.detail.show_track(tid2)
assert w.detail.track_table.columnCount() == 4


def _row_of(field):
    return w.detail._track_fields.index(field)


def _track_props(field):
    return [_json.loads(p[0]) for p in w.con.execute(
        "SELECT proposed FROM proposals WHERE track_id=? AND field=?"
        " AND status IN ('pending','edited')", (tid2, field)).fetchall()]


w.detail.track_table.cellWidget(_row_of("album"), 3).click()   # Clear
assert _track_props("album") == [[]]
# _track_cleared rebuilt the view: marker shown, Clear disabled
assert w.detail.track_table.item(_row_of("album"), 2).text() == CLEAR_MARKER
assert not w.detail.track_table.cellWidget(_row_of("album"), 3).isEnabled()
# typing over the marker turns the clear into a value proposal
w.detail.track_table.item(_row_of("album"), 2).setText("Other")
for _ in range(3):
    app.processEvents()                  # deferred rebuild after the edit
assert _track_props("album") == [["Other"]]
# emptying the cell withdraws the proposal entirely
w.detail.track_table.item(_row_of("album"), 2).setText("")
for _ in range(3):
    app.processEvents()
assert _track_props("album") == []
# Clear + Apply: the frame must really disappear from the MP3
assert tagio.read_tags(path2).get("album"), "test file must have an album"
w.detail.track_table.cellWidget(_row_of("album"), 3).click()
pid = w.con.execute(
    "SELECT id FROM proposals WHERE track_id=? AND field='album'"
    " AND status IN ('pending','edited')", (tid2,)).fetchone()[0]
res = _ap.apply_proposals(w.con, cfg["settings"], prop_ids=[pid])
assert res["files"] == 1 and not res["errors"], res
assert not tagio.read_tags(path2).get("album"), \
    "applying a clear proposal must remove the tag frame from the file"

# remove from library (db only; files stay)
from mp3lib import db as _db
_db.remove_scope(w.con, artist_folders=["ArtistB"])
assert w.con.execute("SELECT COUNT(*) FROM tracks WHERE"
                     " artist_folder='ArtistB'").fetchone()[0] == 0
assert w.con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] > 0
assert os.path.exists(os.path.join(root, "ArtistB"))
w.refresh_tree()

# selection restore across refresh_tree
w.artist_btn.setChecked(True)
w.refresh_tree()
from PySide6.QtCore import QItemSelectionModel
idx = w.proxy.index(0, 0)
w.tree.selectionModel().select(idx, QItemSelectionModel.Select | QItemSelectionModel.Rows)
before = w._selected_keys()
w.refresh_tree()
after = w._selected_keys()
assert before and before == after, (before, after)

print("GUI-DETAIL-OK  (%d change types with tooltips)" % len(tips))
app.quit()

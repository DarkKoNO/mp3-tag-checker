# -*- coding: utf-8 -*-
"""Check which rows have tooltips in the 'Show all changes' trees."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
import _isolate  # noqa: F401  -- redirect app data to a temp dir (before mp3lib)

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
adir = os.path.join(root, "ArtistA", "2001 - One")
os.makedirs(adir)
for i in (1, 2):
    make_mp3(os.path.join(adir, "%02d - t.mp3" % i), "T%d" % i, "ArtistA",
             "One", str(i))
with open(os.path.join(adir, "01 - t.mp3"), "ab") as f:
    f.write(tagio.build_id3v1({"title": ["Old T1"], "artist": ["ArtistA"]}))

dbfile = os.path.join(root, "test.db")
cfg = {"libraries": [{"name": "T", "root": root, "folders_txt": "f.txt",
                      "db": dbfile}],
       "active_library": "T", "settings": dict(DEFAULT_SETTINGS)}

from mp3lib.gui.main_window import MainWindow
w = MainWindow(cfg)
w.show()
scanner.scan(w.con, cfg["settings"], root, ["ArtistA"])

def dump_tree(tree, label):
    print("== %s ==" % label)
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        print("  TOP %-45s tip=%s" % (top.text(0)[:45], bool(top.toolTip(0))))
        for j in range(top.childCount()):
            ch = top.child(j)
            e = ch.data(0, 32)  # Qt.UserRole
            print("    row %-40s rule=%-18s tip0=%s" % (
                ch.text(0)[:40], (e or {}).get("rule"), repr(ch.toolTip(0))[:80]))

# artist detailed view
w.detail._artist_detailed = True
w.detail.show_artist("ArtistA")
dump_tree(w.detail.entry_tree, "artist / Show all changes")

# ctype detailed view
w.detail._ctype_detailed = True
w.detail.show_ctype("issue", "id3v1_conflict", "Old ID3v1 tag disagrees with ID3v2")
dump_tree(w.detail.entry_tree, "ctype id3v1_conflict / Show all changes")

# album view for comparison
w.detail.show_album(adir)
dump_tree(w.detail.entry_tree, "album view")

app.quit()

# -*- coding: utf-8 -*-
"""Regression tests: dependent rules (effective values), per-rule postpone,
stale-proposal cleanup, mojibake-aware ID3v1 conflicts, apply-selected."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2, TRCK

from mp3lib import applier, db, rules, tagio
from mp3lib.settings import DEFAULT_SETTINGS

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("PASS %s" % name)
    else:
        fail += 1
        print("FAIL %s  %s" % (name, extra))


def make_mp3(path, title, artist, albumartist, album, track):
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    with open(path, "wb") as f:
        f.write(frame * 20)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=[title]))
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TPE2(encoding=3, text=[albumartist]))
    tags.add(TALB(encoding=3, text=[album]))
    tags.add(TRCK(encoding=3, text=[track]))
    tags.save(path, v2_version=4)


def add_track(con, scan_id, path, artist_folder, adir):
    t = tagio.read_tags(path)
    tid = con.execute(
        "INSERT INTO tracks(path, artist_folder, album_dir, filename, size,"
        " mtime) VALUES (?,?,?,?,1,1)",
        (path, artist_folder, adir, os.path.basename(path))).lastrowid
    db.add_snapshot(con, tid, scan_id, "scan", t)
    return tid


tmp = tempfile.mkdtemp()
con = db.connect(":memory:")
settings = dict(DEFAULT_SETTINGS)
settings["required_fields"] = ["title", "artist", "albumartist", "album", "track"]
settings["track_pad"] = False
scan_id = db.new_batch(con, "scan")

# ---- 1. dependent rules: combined artist AND albumartist ("A; B") ----------
adir = os.path.join(tmp, "Rodrigo Amado", "2023 - + The Bridge - Beyond")
os.makedirs(adir)
tids = []
for i in (1, 2):
    p = os.path.join(adir, "%02d - t.mp3" % i)
    make_mp3(p, "T%d" % i, "Rodrigo Amado; The Bridge",
             "Rodrigo Amado; The Bridge", "Beyond", "%d/2" % i)
    tids.append(add_track(con, scan_id, p, "Rodrigo Amado", adir))
con.execute("INSERT INTO albums(album_dir, artist_folder, folder_jpg)"
            " VALUES (?,?,1)", (adir, "Rodrigo Amado"))
con.commit()
rules.evaluate(con, settings, album_dirs=[adir])

props = db.open_proposals(con, statuses=db.ALL_OPEN_STATUSES)
by_rule_field = {(p["rule"], p["field"]): p for p in props if p["track_id"] == tids[0]}
sp_artist = by_rule_field.get(("multi_split", "artist"))
sp_aa = by_rule_field.get(("multi_split", "albumartist"))
check("artist split proposed", sp_artist is not None
      and sp_artist["proposed"] == ["Rodrigo Amado", "The Bridge"],
      str(sp_artist))
check("albumartist split proposed", sp_aa is not None
      and sp_aa["proposed"] == ["Rodrigo Amado", "The Bridge"], str(sp_aa))
garbage = [p for p in props if p["rule"] == "artist_superset"]
check("no artist_superset garbage", not garbage,
      str([(p["field"], p["proposed"]) for p in garbage]))
aa_over = [p for p in props if p["rule"] == "albumartist"]
check("albumartist rule does not fight the split", not aa_over,
      str([(p["field"], p["proposed"]) for p in aa_over]))

# ---- 2. per-rule postpone + stale cleanup -----------------------------------
# postpone the artist split, then pretend a DIFFERENT rule takes the field
applier.set_proposal_status(con, [sp_artist["id"]], "postponed")
rules.evaluate(con, settings, album_dirs=[adir])
props = db.open_proposals(con, statuses=db.ALL_OPEN_STATUSES)
sp2 = [p for p in props if p["rule"] == "multi_split" and p["field"] == "artist"
       and p["track_id"] == tids[0]]
check("same rule keeps postpone", sp2 and sp2[0]["status"] == "postponed",
      str(sp2))

# apply everything applicable (albumartist split etc.)
res = applier.apply_proposals(con, settings, album_dirs=[adir])
check("apply ok", not res["errors"], str(res["errors"]))
props = db.open_proposals(con, statuses=db.ALL_OPEN_STATUSES)
sp3 = [p for p in props
       if p["field"] == "artist" and p["track_id"] == tids[0]]
# artist proposal must still exist (the postponed split, refreshed, same rule)
check("postponed artist split survives apply (same rule)",
      sp3 and all(p["rule"] == "multi_split" and p["status"] == "postponed"
                  for p in sp3), str(sp3))

# now simulate the user's bug: different rule proposing on the same field must
# NOT inherit the postpone
rid = db.upsert_proposal(con, tids[0], "Rodrigo Amado", adir, "artist",
                         ["Rodrigo Amado; The Bridge"], ["X"], "rule",
                         status="pending", rule="artist_superset")
row = con.execute("SELECT status, rule FROM proposals WHERE id=?", (rid,)).fetchone()
check("different rule starts fresh (no inherited postpone)",
      row == ("pending", "artist_superset"), str(row))
con.execute("DELETE FROM proposals WHERE id=?", (rid,))
con.commit()

# stale cleanup: manually fix the artist tag in the file -> split rule stops
# firing -> the postponed split proposal must disappear on re-evaluate
p0 = con.execute("SELECT path FROM tracks WHERE id=?", (tids[0],)).fetchone()[0]
tagio.write_changes(p0, {"artist": ["Rodrigo Amado", "The Bridge"]}, settings)
db.add_snapshot(con, tids[0], db.new_batch(con, "apply"), "applied",
                tagio.read_tags(p0))
con.commit()
rules.evaluate(con, settings, album_dirs=[adir])
props = db.open_proposals(con, statuses=db.ALL_OPEN_STATUSES)
left = [p for p in props if p["field"] == "artist" and p["track_id"] == tids[0]]
check("stale postponed proposal cleaned up", not left, str(left))

# ---- 3. mojibake-aware v1 conflict ------------------------------------------
v1 = {"artist": ["Dan BÃ¡rta; Robert Balzar Trio"], "title": ["PÃ­seÅ\x88"]}
tags = {"artist": ["Dan Bárta", "Robert Balzar Trio"], "title": ["Píseň"]}
check("utf8-in-v1 is not a conflict", rules.v1_conflicts(v1, tags) == [],
      str(rules.v1_conflicts(v1, tags)))
v1b = {"artist": ["Someone Else"]}
check("real conflict still detected",
      rules.v1_conflicts(v1b, tags) != [], "")

# read_id3v1: UTF-8 bytes in the v1 block decode correctly now
pv = os.path.join(tmp, "v1utf8.mp3")
make_mp3(pv, "X", "Y", "Y", "Z", "1")
block = b"TAG" + "Píseň".encode("utf-8").ljust(30, b"\x00") \
    + "Dan Bárta".encode("utf-8").ljust(30, b"\x00") + b"\x00" * 30 \
    + b"2010" + b"\x00" * 30 + b"\xff"
with open(pv, "ab") as f:
    f.write(block)
got = tagio.read_id3v1(pv)
check("read_id3v1 decodes UTF-8 v1 text",
      got["title"] == ["Píseň"] and got["artist"] == ["Dan Bárta"], str(got))

# ---- 4. apply only selected proposal ids ------------------------------------
adir2 = os.path.join(tmp, "ArtistB", "2001 - Two")
os.makedirs(adir2)
p2 = os.path.join(adir2, "01 - a.mp3")
make_mp3(p2, "A", "X; Y", "X; Y", "Two", "1/1")
tid2 = add_track(con, scan_id, p2, "ArtistB", adir2)
con.execute("INSERT INTO albums(album_dir, artist_folder, folder_jpg)"
            " VALUES (?,?,1)", (adir2, "ArtistB"))
con.commit()
rules.evaluate(con, settings, album_dirs=[adir2])
props = db.open_proposals(con, album_dirs=[adir2])
split_artist = [p for p in props if p["field"] == "artist"]
check("two splits pending", len(props) >= 2 and split_artist, str(props))
res = applier.apply_proposals(con, settings, prop_ids=[split_artist[0]["id"]])
t2 = tagio.read_tags(p2)
check("only selected proposal applied",
      t2["artist"] == ["X", "Y"] and t2["albumartist"] == ["X; Y"],
      "%s / %s" % (t2["artist"], t2["albumartist"]))

print("\n%d passed, %d failed" % (ok, fail))
sys.exit(1 if fail else 0)

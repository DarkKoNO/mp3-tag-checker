# -*- coding: utf-8 -*-
"""Smoke test for the new rule modes, mojibake behavior, v1-conflict flow
and the 'undated' year round-trip. Runs against an in-memory DB and a tiny
generated MP3 file. No GUI."""
import io
import json
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

# isolate ALL user data (config, themes, databases) in a throwaway temp dir so
# a test can never read or write the real config.json / libraries. Must be set
# before mp3lib.settings is imported.
os.environ.setdefault("MP3TAGGER_DATA_DIR",
                      tempfile.mkdtemp(prefix="mp3tagger-test-"))

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

# ---- 1. mojibake -----------------------------------------------------------
check("nordic ø not flagged", rules.detect_mojibake("Mørketid") is None)
check("french not flagged", rules.detect_mojibake("Grâce à l'été") is None)
check("nordic å æ not flagged", rules.detect_mojibake("Håkan Æon") is None)
got = rules.detect_mojibake("KapelnÃ­k")   # UTF-8 read as cp1252
check("utf8-as-cp1252 repaired", got == "Kapelník", repr(got))
got = rules.detect_mojibake("Vlt\x9ava")   # C1 control char damage (cp1250 š)
check("C1 damage repaired or None (no crash)", got is None or "\x9a" not in got, repr(got))

# ---- 2. build a tiny MP3 with conflicting v1/v2 tags -----------------------
def make_mp3(path):
    # minimal MPEG1 Layer3 frame (silence), enough for mutagen
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    with open(path, "wb") as f:
        f.write(frame * 20)

tmp = tempfile.mkdtemp()
artist_dir = os.path.join(tmp, "Artist")
album_dir = os.path.join(artist_dir, "2005 - Album")
os.makedirs(album_dir)
p1 = os.path.join(album_dir, "01 - Song.mp3")
make_mp3(p1)

import mutagen.id3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TDRC
tags = ID3()
tags.add(TIT2(encoding=3, text=["New Title"]))
tags.add(TPE1(encoding=3, text=["Artist"]))
tags.add(TALB(encoding=3, text=["Album"]))
tags.add(TRCK(encoding=3, text=["1/1"]))
tags.save(p1, v2_version=4)
# append a conflicting ID3v1 tag
v1 = {"title": ["Old Title"], "artist": ["Artist"], "album": ["Album"]}
with open(p1, "ab") as f:
    f.write(tagio.build_id3v1(v1))

t = tagio.read_tags(p1)
check("v1 detected", t["_has_v1"] is True)
check("v1 conflict on title", any(c[0] == "title" for c in rules.v1_conflicts(t["_v1"], t)))

# ---- 3. db + evaluate ------------------------------------------------------
con = db.connect(":memory:")
settings = dict(DEFAULT_SETTINGS)
settings["required_fields"] = ["title", "artist", "album", "track"]
scan_id = db.new_batch(con, "scan")
tid = con.execute(
    "INSERT INTO tracks(path, artist_folder, album_dir, filename, size, mtime)"
    " VALUES (?,?,?,?,1,1)", (p1, "Artist", album_dir, "01 - Song.mp3")).lastrowid
db.add_snapshot(con, tid, scan_id, "scan", t)
con.execute("INSERT INTO albums(album_dir, artist_folder, folder_jpg) VALUES (?,?,1)",
            (album_dir, "Artist"))
con.execute("INSERT INTO artists(folder, artist_jpg) VALUES (?,1)", ("Artist",))
con.commit()

rules.evaluate(con, settings, album_dirs=[album_dir])
props = db.open_proposals(con, statuses=db.ALL_OPEN_STATUSES)
conf = [p for p in props if p["rule"] == "id3v1_conflict"]
check("v1 conflict offer created", len(conf) == 1, str(props))
check("offer default postponed (rule mode)", conf and conf[0]["status"] == "postponed",
      conf and conf[0]["status"])
issues = con.execute("SELECT rule FROM issues").fetchall()
check("conflict issue exists", ("id3v1_conflict",) in issues, str(issues))

# ---- 4. disabled mode wipes the type --------------------------------------
settings2 = dict(settings)
settings2["rule_modes"] = {"id3v1_conflict": "disabled"}
rules.evaluate(con, settings2, album_dirs=[album_dir])
props2 = db.open_proposals(con, statuses=db.ALL_OPEN_STATUSES)
check("disabled: no conflict proposals",
      not any(p["rule"] == "id3v1_conflict" for p in props2), str(props2))
issues2 = con.execute("SELECT rule FROM issues").fetchall()
check("disabled: no conflict issue", ("id3v1_conflict",) not in issues2, str(issues2))
check("disabled: plain id3v1 removal offered instead",
      any(p["rule"] == "id3v1" for p in props2), str(props2))

# ---- 5. apply the v1 value -> old tag removed in the same run --------------
rules.evaluate(con, settings, album_dirs=[album_dir])   # back to default modes
row = con.execute("SELECT id FROM proposals WHERE rule='id3v1_conflict'"
                  " AND status='postponed'").fetchone()
applier.set_proposal_status(con, [row[0]], "pending")   # user clicks Restore
res = applier.apply_proposals(con, settings, album_dirs=[album_dir])
check("apply ok", res["files"] == 1 and not res["errors"], str(res))
t2 = tagio.read_tags(p1)
check("v1 value written to v2", t2.get("title") == ["Old Title"], str(t2.get("title")))
check("old ID3v1 removed in same apply", t2["_has_v1"] is False)
log = con.execute("SELECT field, new FROM changelog").fetchall()
check("changelog has _id3v1 removal", any(f == "_id3v1" for f, _n in log), str(log))

# ---- 6. 'undated' year round-trip ------------------------------------------
applier.set_manual_proposal(con, tid, "year", ["undated"])
res = applier.apply_proposals(con, settings, album_dirs=[album_dir])
check("undated apply ok", res["files"] == 1 and not res["errors"], str(res))
t3 = tagio.read_tags(p1)
check("year 'undated' round-trips", t3.get("year") == ["undated"], str(t3.get("year")))
# value_format flags it (not in allowed list by default)
rules.evaluate(con, settings, album_dirs=[album_dir])
iss = [r[0] for r in con.execute("SELECT rule FROM issues")]
check("non-allowed free year flagged by value_format", "value_format" in iss, str(iss))
# once allowed, no complaint
settings3 = dict(settings)
settings3["field_patterns"] = {"year": {"regex": "^\\d{4}$", "allowed": "undated"}}
rules.evaluate(con, settings3, album_dirs=[album_dir])
iss3 = [r[0] for r in con.execute("SELECT rule FROM issues")]
check("allowed 'undated' passes validation", "value_format" not in iss3, str(iss3))

# ---- 7. plus_collab only artist -------------------------------------------
p2dir = os.path.join(artist_dir, "1969 - + Mothers - Uncle Meat")
os.makedirs(p2dir)
p2 = os.path.join(p2dir, "01 - x.mp3")
make_mp3(p2)
tags = ID3()
tags.add(TIT2(encoding=3, text=["X"]))
tags.add(TPE1(encoding=3, text=["Frank Zappa"]))
tags.add(mutagen.id3.TPE2(encoding=3, text=["Frank Zappa"]))
tags.add(TALB(encoding=3, text=["Uncle Meat"]))
tags.add(TRCK(encoding=3, text=["1/1"]))
tags.save(p2, v2_version=4)
t4 = tagio.read_tags(p2)
tid2 = con.execute(
    "INSERT INTO tracks(path, artist_folder, album_dir, filename, size, mtime)"
    " VALUES (?,?,?,?,1,1)", (p2, "Artist", p2dir, "01 - x.mp3")).lastrowid
db.add_snapshot(con, tid2, scan_id, "scan", t4)
con.execute("INSERT INTO albums(album_dir, artist_folder, folder_jpg) VALUES (?,?,1)",
            (p2dir, "Artist"))
con.commit()
rules.evaluate(con, settings, album_dirs=[p2dir])
plus_msgs = [r[0] for r in con.execute(
    "SELECT message FROM issues WHERE rule='plus_collab'")]
check("plus_collab fires for artist", len(plus_msgs) == 1, str(plus_msgs))
check("plus_collab not for albumartist",
      all("albumartist" not in m for m in plus_msgs), str(plus_msgs))

# ---- 8. rule descriptions complete -----------------------------------------
missing_desc = [r for r in rules.CONFIGURABLE_RULES if not rules.rule_description(r)]
check("all configurable rules have descriptions", not missing_desc, str(missing_desc))
missing_lbl = [r for r in rules.CONFIGURABLE_RULES if rules.rule_label(r) == r]
check("all configurable rules have labels", not missing_lbl, str(missing_lbl))
all_rules_desc = [r for r in rules.RULE_LABELS if not rules.rule_description(r)]
check("every RULE_LABELS entry has a description", not all_rules_desc, str(all_rules_desc))

print("\n%d passed, %d failed" % (ok, fail))
sys.exit(1 if fail else 0)

# -*- coding: utf-8 -*-
"""APEv2 handling: detection, conflict, rescue, clean removal, the same-write
removal on apply, the 'keep it while an unresolved conflict is open' guard, and
history revert restoring a removed APEv2 tag. Mirrors the ID3v1 flow. No GUI."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

# isolate user data (config/themes/databases) in a temp dir - never the real one
os.environ.setdefault("MP3TAGGER_DATA_DIR",
                      tempfile.mkdtemp(prefix="mp3tagger-test-"))

import mutagen.apev2
from mutagen.apev2 import APEv2, APENoHeaderError
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK

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


def make_mp3(path):
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    with open(path, "wb") as f:
        f.write(frame * 20)


def id3(path, **frames):
    tags = ID3()
    if "title" in frames:
        tags.add(TIT2(encoding=3, text=frames["title"]))
    if "artist" in frames:
        tags.add(TPE1(encoding=3, text=frames["artist"]))
    if "album" in frames:
        tags.add(TALB(encoding=3, text=frames["album"]))
    if "track" in frames:
        tags.add(TRCK(encoding=3, text=frames["track"]))
    tags.save(path, v2_version=4)


def ape(path, **keys):
    tag = APEv2()
    for k, v in keys.items():
        tag[k] = v
    tag.save(path)


def has_ape(path):
    try:
        APEv2(path)
        return True
    except APENoHeaderError:
        return False


def fresh_track(con, path, adir, fname):
    scan_id = db.new_batch(con, "scan")
    tid = con.execute(
        "INSERT INTO tracks(path, artist_folder, album_dir, filename, size, mtime)"
        " VALUES (?,?,?,?,1,1)", (path, "Artist", adir, fname)).lastrowid
    db.add_snapshot(con, tid, scan_id, "scan", tagio.read_tags(path))
    con.execute("INSERT OR IGNORE INTO albums(album_dir, artist_folder, folder_jpg)"
                " VALUES (?,?,1)", (adir, "Artist"))
    con.execute("INSERT OR IGNORE INTO artists(folder, artist_jpg) VALUES (?,1)",
                ("Artist",))
    con.commit()
    return tid


settings = dict(DEFAULT_SETTINGS)
settings["required_fields"] = ["title", "artist", "album", "track"]

# ---- 1. read_apev2 maps standard keys, ignores non-metadata ----------------
tmp = tempfile.mkdtemp()
adir = os.path.join(tmp, "Artist", "2005 - Album")
os.makedirs(adir)

p = os.path.join(adir, "01 - Song.mp3")
make_mp3(p)
id3(p, title=["V2 Title"], artist=["Artist"], album=["Album"], track=["1/1"])
ape(p, Title="Ape Title", Artist="Artist", Album="Album", Track="1/1",
    REPLAYGAIN_TRACK_GAIN="-3.21 dB")
t = tagio.read_tags(p)
check("APEv2 detected", t["_has_ape"] is True)
check("APEv2 title mapped", t.get("_ape", {}).get("title") == ["Ape Title"],
      str(t.get("_ape")))
check("APEv2 replaygain not mapped as metadata",
      "REPLAYGAIN_TRACK_GAIN" not in str(t.get("_ape")))
check("APEv2 conflict on title only",
      [c[0] for c in rules.ape_conflicts(t["_ape"], t)] == ["title"],
      str(rules.ape_conflicts(t["_ape"], t)))

# ---- 2. conflict -> offer created, postponed by default --------------------
con = db.connect(":memory:")
tid = fresh_track(con, p, adir, "01 - Song.mp3")
rules.evaluate(con, settings, album_dirs=[adir])
props = db.open_proposals(con, statuses=db.ALL_OPEN_STATUSES)
conf = [pr for pr in props if pr["rule"] == "apev2_conflict"]
check("APEv2 conflict offer created", len(conf) == 1, str(props))
check("APEv2 offer default postponed", conf and conf[0]["status"] == "postponed",
      conf and conf[0]["status"])
iss = [r[0] for r in con.execute("SELECT rule FROM issues")]
check("APEv2 conflict issue exists", "apev2_conflict" in iss, str(iss))

# ---- 3. keep-during-conflict guard: an unrelated write must NOT strip ------
# (edit a field APEv2 does not carry, so no NEW conflict is introduced)
applier.set_manual_proposal(con, tid, "comment", ["hello"])
res = applier.apply_proposals(con, settings, track_ids=[tid])
check("unrelated apply ok", res["files"] == 1 and not res["errors"], str(res))
check("APEv2 kept while conflict unresolved", has_ape(p) is True)

# ---- 4. apply the APEv2 value -> tag removed in the same run ---------------
rules.evaluate(con, settings, album_dirs=[adir])
row = con.execute("SELECT id FROM proposals WHERE rule='apev2_conflict'"
                  " AND field='title'").fetchone()
applier.set_proposal_status(con, [row[0]], "pending")   # user clicks Restore
res = applier.apply_proposals(con, settings, track_ids=[tid])
check("apply-ape ok", res["files"] == 1 and not res["errors"], str(res))
t2 = tagio.read_tags(p)
check("APEv2 value written to v2", t2.get("title") == ["Ape Title"],
      str(t2.get("title")))
check("APEv2 tag removed in same apply", t2["_has_ape"] is False)
log = [f for f, _n in con.execute("SELECT field, new FROM changelog")]
check("changelog has _apev2 removal", "_apev2" in log, str(log))

# ---- 5. clean case: APEv2 == ID3v2 -> straight removal proposal ------------
p2 = os.path.join(adir, "02 - Clean.mp3")
make_mp3(p2)
id3(p2, title=["Same"], artist=["Artist"], album=["Album"], track=["2/2"])
ape(p2, Title="Same", Artist="Artist", Album="Album", Track="2/2")
tid2 = fresh_track(con, p2, adir, "02 - Clean.mp3")
rules.evaluate(con, settings, album_dirs=[adir])
rm = con.execute("SELECT id FROM proposals WHERE track_id=? AND field='_apev2'"
                 " AND rule='apev2'", (tid2,)).fetchone()
check("clean APEv2 -> removal proposed (no conflict)", rm is not None)
check("clean APEv2 -> no conflict issue",
      con.execute("SELECT 1 FROM issues WHERE track_id=? AND rule='apev2_conflict'",
                  (tid2,)).fetchone() is None)
applier.apply_proposals(con, settings, track_ids=[tid2])
check("clean APEv2 removed on apply", tagio.read_tags(p2)["_has_ape"] is False)

# ---- 6. rescue: value only in APEv2 -> copied into v2 ----------------------
p3 = os.path.join(adir, "03 - Rescue.mp3")
make_mp3(p3)
id3(p3, artist=["Artist"], album=["Album"], track=["3/3"])   # no title in v2
ape(p3, Title="Rescued Title")
tid3 = fresh_track(con, p3, adir, "03 - Rescue.mp3")
rules.evaluate(con, settings, album_dirs=[adir])
resc = con.execute("SELECT proposed FROM proposals WHERE track_id=? AND"
                   " field='title' AND rule='apev2_rescue'", (tid3,)).fetchone()
check("APEv2 rescue proposed for missing title", resc is not None, str(resc))
applier.apply_proposals(con, settings, track_ids=[tid3])
t3 = tagio.read_tags(p3)
check("rescued title written to v2", t3.get("title") == ["Rescued Title"],
      str(t3.get("title")))
check("APEv2 removed after rescue", t3["_has_ape"] is False)

# ---- 7. 'keep the APEv2 tag': disable the apev2 rule -----------------------
p4 = os.path.join(adir, "04 - Keep.mp3")
make_mp3(p4)
id3(p4, title=["Keep"], artist=["Artist"], album=["Album"], track=["4/4"])
ape(p4, Title="Keep", Artist="Artist", Album="Album", Track="4/4")
keep_settings = dict(settings)
keep_settings["rule_modes"] = {"apev2": "disabled"}
tid4 = fresh_track(con, p4, adir, "04 - Keep.mp3")
rules.evaluate(con, keep_settings, album_dirs=[adir])
check("apev2 rule disabled -> no apev2 proposals",
      con.execute("SELECT 1 FROM proposals WHERE track_id=? AND"
                  " rule LIKE 'apev2%'", (tid4,)).fetchone() is None)
applier.set_manual_proposal(con, tid4, "title", ["Keep Edited"])
applier.apply_proposals(con, keep_settings, track_ids=[tid4])
check("APEv2 kept when apev2 rule disabled", has_ape(p4) is True)

# ---- 7b. per-field decisions: keep one field, use another ------------------
# Two differing fields (title, album); user keeps ID3v2 for title, uses APEv2
# for album. Only the selected field is affected; both independently reversible.
# own album folder so album-level rules don't claim the 'album' field
adir5 = os.path.join(tmp, "Artist", "2013 - Fields")
os.makedirs(adir5)
p5 = os.path.join(adir5, "05 - Fields.mp3")
make_mp3(p5)
id3(p5, title=["V2 Title"], artist=["Artist"], album=["V2 Album"], track=["5/5"])
ape(p5, Title="Ape Title", Artist="Artist", Album="Ape Album", Track="5/5")
tid5 = fresh_track(con, p5, adir5, "05 - Fields.mp3")
rules.evaluate(con, settings, album_dirs=[adir5])
offers = {r[0]: r[1] for r in con.execute(
    "SELECT field, id FROM proposals WHERE track_id=? AND rule='apev2_conflict'",
    (tid5,))}
check("both differing fields offered", set(offers) == {"title", "album"}, str(offers))
# keep ID3v2 for title only (simulate selecting just that row)
applier.set_keep_v2(con, settings, [{"rule": "apev2_conflict", "track_id": tid5,
                                     "field": "title", "album_dir": adir5}], keep=True)
check("title marked keep-v2", con.execute(
    "SELECT 1 FROM ape_keep_v2 WHERE track_id=? AND field='title'",
    (tid5,)).fetchone() is not None)
check("album NOT marked (selection scoped)", con.execute(
    "SELECT 1 FROM ape_keep_v2 WHERE track_id=? AND field='album'",
    (tid5,)).fetchone() is None)
# with title decided but album still undecided, removal stays blocked
check("removal blocked while album undecided", con.execute(
    "SELECT 1 FROM proposals WHERE track_id=? AND field='_apev2'",
    (tid5,)).fetchone() is None)
check("conflict issue still present (album)", con.execute(
    "SELECT 1 FROM issues WHERE track_id=? AND rule='apev2_conflict'",
    (tid5,)).fetchone() is not None)
# use APEv2 value for album, then apply
applier.use_old_value(con, settings, [{"rule": "apev2_conflict", "track_id": tid5,
                                       "field": "album", "album_dir": adir5}])
applier.apply_proposals(con, settings, track_ids=[tid5])
t5 = tagio.read_tags(p5)
check("kept field: ID3v2 title preserved", t5.get("title") == ["V2 Title"], str(t5.get("title")))
check("switched field: APEv2 album written", t5.get("album") == ["Ape Album"], str(t5.get("album")))
check("APEv2 tag removed once all fields decided", t5["_has_ape"] is False)

# ---- 7c. reversibility: undo a keep-v2 decision ---------------------------
p6 = os.path.join(adir, "06 - Undo.mp3")
make_mp3(p6)
id3(p6, title=["V2 Only"], artist=["Artist"], album=["Album"], track=["6/6"])
ape(p6, Title="Ape Only", Artist="Artist", Album="Album", Track="6/6")
tid6 = fresh_track(con, p6, adir, "06 - Undo.mp3")
rules.evaluate(con, settings, album_dirs=[adir])
ent = [{"rule": "apev2_conflict", "track_id": tid6, "field": "title", "album_dir": adir}]
applier.set_keep_v2(con, settings, ent, keep=True)
check("decided keep-v2 (no blocking issue)", con.execute(
    "SELECT 1 FROM issues WHERE track_id=? AND rule='apev2_conflict'",
    (tid6,)).fetchone() is None)
applier.set_keep_v2(con, settings, ent, keep=False)   # change my mind
check("undo keep-v2 -> conflict offered again", con.execute(
    "SELECT 1 FROM issues WHERE track_id=? AND rule='apev2_conflict'",
    (tid6,)).fetchone() is not None)
check("undo keep-v2 -> marker cleared", con.execute(
    "SELECT 1 FROM ape_keep_v2 WHERE track_id=? AND field='title'",
    (tid6,)).fetchone() is None)

# ---- 7d. delay-off: removal offered even with undecided differences --------
nodelay = dict(settings)
nodelay["apev2_delay_on_conflict"] = False
rules.evaluate(con, nodelay, album_dirs=[adir])
check("delay-off: removal offered despite undecided conflict", con.execute(
    "SELECT 1 FROM proposals WHERE track_id=? AND field='_apev2'",
    (tid6,)).fetchone() is not None)
check("delay-off: no blocking conflict issue", con.execute(
    "SELECT 1 FROM issues WHERE track_id=? AND rule='apev2_conflict'",
    (tid6,)).fetchone() is None)
rules.evaluate(con, settings, album_dirs=[adir])   # back to delayed default

# ---- 8. history revert restores a removed APEv2 tag ------------------------
# reuse tid2 (clean removal applied in step 5): revert the album to before it
hist = applier.album_history(con, adir)
# find the batch just before the clean removal was applied to p2 - simplest:
# snapshot batch of the scan that first saw p2 with its APEv2 tag
first_batch = con.execute(
    "SELECT MIN(scan_id) FROM snapshots WHERE track_id=?", (tid2,)).fetchone()[0]
res = applier.revert_album(con, settings, adir, first_batch)
check("revert ran", not res["errors"], str(res))
check("APEv2 restored by revert", has_ape(p2) is True)
restored = tagio.read_apev2(p2)
check("restored APEv2 has the original title",
      restored and restored.get("title") == ["Same"], str(restored))

print("\n%d passed, %d failed" % (ok, fail))
sys.exit(1 if fail else 0)

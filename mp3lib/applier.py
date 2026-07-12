"""Applying approved proposals to files, and reverting to older versions.

Every write: snapshot afterwards (so the latest snapshot always equals the
file's current state), changelog entry per changed field, rules re-evaluated.
"""

import json
from collections import defaultdict
from pathlib import Path

from . import db, rules, tagio


def _write_track(con, settings, batch_id, track_id, path, changes, origin_by_field,
                 reason="applied", keep_v1="auto", keep_ape=None):
    """Write changes to one file, snapshot + changelog. Returns #fields changed.
    keep_v1: 'auto' = preserve the old tag while a v1/v2 conflict is open;
    bytes = write exactly this ID3v1 block (history revert); None = normal.
    keep_ape: {field: [values]} = restore this APEv2 tag (history revert);
    None = strip per settings, but never while a v2 conflict is open."""
    if keep_v1 == "auto":
        keep = None
        # unresolved ID3v1/v2 conflict: never let a write destroy the old tag
        if con.execute("SELECT 1 FROM issues WHERE track_id=?"
                       " AND rule='id3v1_conflict' LIMIT 1",
                       (track_id,)).fetchone():
            keep = tagio.get_v1_bytes(path)
    else:
        keep = keep_v1
    # APEv2: same "never destroy an unresolved conflict" guard (unless a revert
    # is explicitly restoring the tag via keep_ape)
    strip_ape = None
    if keep_ape is None and con.execute(
            "SELECT 1 FROM issues WHERE track_id=? AND rule='apev2_conflict'"
            " LIMIT 1", (track_id,)).fetchone():
        strip_ape = False
    # a cover replacement: remember the old picture so revert can restore it
    if "cover" in changes:
        snap = db.latest_snapshot(con, track_id)
        old_cov = tagio.get_cover_data(path)
        if snap and old_cov:
            db.save_cover_blob(con, snap["id"], old_cov[0], old_cov[1])
    applied = tagio.write_changes(path, changes, settings, keep_v1_bytes=keep,
                                  strip_ape=strip_ape, keep_ape_data=keep_ape)
    tags = tagio.read_tags(path)
    db.add_snapshot(con, track_id, batch_id, reason, tags,
                    keep=settings["history_keep"])
    import os
    st = os.stat(path)
    con.execute("UPDATE tracks SET size=?, mtime=? WHERE id=?",
                (st.st_size, st.st_mtime, track_id))
    for field, old, new in applied:
        db.log_change(con, track_id, path, field, old, new,
                      origin_by_field.get(field, "rule"))
    pseudo = [f for f in changes if f in tagio.PSEUDO_FIELDS]
    for f in pseudo:
        if f in ("_id3v1", "_apev2"):
            old_v, new_v = ["present"], ["removed"]
        else:
            old_v, new_v = ["old"], ["2.4"]
        db.log_change(con, track_id, path, f, old_v, new_v,
                      origin_by_field.get(f, "rule"))
    return len(applied) + len(pseudo)


def apply_proposals(con, settings, artist_folders=None, album_dirs=None,
                    track_ids=None, rule=None, field=None, online_filter=None,
                    exclude_rules=None, prop_ids=None, progress=None):
    """Apply all pending/edited proposals in scope. Returns summary dict.
    Postponed / needs-input / excepted / hidden proposals are never applied.
    prop_ids: apply only these explicitly selected proposal rows."""
    props = db.open_proposals(con, artist_folders, album_dirs, track_ids,
                              rule=rule, field=field,
                              online_filter=online_filter,
                              exclude_rules=exclude_rules, prop_ids=prop_ids)
    if not props:
        return {"files": 0, "changes": 0, "errors": []}

    batch_id = db.new_batch(con, "apply")
    by_track = defaultdict(list)
    album_level = defaultdict(list)
    for p in props:
        if p["track_id"] is None:
            album_level[p["album_dir"]].append(p)
        else:
            by_track[p["track_id"]].append(p)

    # album-level 'cover' proposals turn into per-track cover writes
    cover_for_album = {}
    for adir, plist in album_level.items():
        for p in plist:
            if p["field"] == "cover":
                ref = p["proposed"][0]          # "pending_cover:<id>"
                cid = int(ref.split(":")[1])
                row = con.execute("SELECT mime, data FROM pending_covers WHERE id=?",
                                  (cid,)).fetchone()
                if row:
                    cover_for_album[adir] = (row[0], row[1], p)
                    for (tid,) in con.execute(
                            "SELECT id FROM tracks WHERE album_dir=? AND missing=0",
                            (adir,)):
                        by_track[tid]  # ensure key exists even with no other props

    n_files = n_changes = 0
    errors = []
    affected_albums = set()
    todo = list(by_track.items())
    total = len(todo)
    for i, (tid, plist) in enumerate(todo):
        row = con.execute("SELECT path, album_dir FROM tracks WHERE id=?",
                          (tid,)).fetchone()
        if row is None:
            continue
        path, adir = row
        affected_albums.add(adir)
        changes = {}
        origin = {}
        for p in plist:
            changes[p["field"]] = p["proposed"]
            origin[p["field"]] = p["source"]
        if adir in cover_for_album:
            mime, data, _cp = cover_for_album[adir]
            changes["cover"] = (mime, bytes(data))
            origin["cover"] = "online"
        # applying a 'use the old ID3v1 value' proposal: when the values being
        # written leave no remaining v1/v2 difference, the old tag is removed
        # in the same write (value first, removal second - one apply does both)
        if (any(p["rule"] == "id3v1_conflict" for p in plist)
                and settings.get("strip_id3v1", True)):
            snap = db.latest_snapshot(con, tid)
            tags = snap["tags"] if snap else {}
            updated = dict(tags)
            for f, v in changes.items():
                if f != "cover" and f not in tagio.PSEUDO_FIELDS:
                    updated[f] = v
            if tags.get("_has_v1") and not rules.v1_conflicts(
                    tags.get("_v1") or {}, updated):
                con.execute("DELETE FROM issues WHERE track_id=?"
                            " AND rule='id3v1_conflict'", (tid,))
                changes.setdefault("_id3v1", ["remove"])
                origin.setdefault("_id3v1", "rule")
        # same for an applied 'use the APEv2 value' proposal
        if (any(p["rule"] == "apev2_conflict" for p in plist)
                and settings.get("strip_apev2", True)):
            snap = db.latest_snapshot(con, tid)
            tags = snap["tags"] if snap else {}
            updated = dict(tags)
            for f, v in changes.items():
                if f != "cover" and f not in tagio.PSEUDO_FIELDS:
                    updated[f] = v
            if tags.get("_has_ape") and not rules.ape_conflicts(
                    tags.get("_ape") or {}, updated):
                con.execute("DELETE FROM issues WHERE track_id=?"
                            " AND rule='apev2_conflict'", (tid,))
                changes.setdefault("_apev2", ["remove"])
                origin.setdefault("_apev2", "rule")
        try:
            n_changes += _write_track(con, settings, batch_id, tid, path,
                                      changes, origin)
            n_files += 1
            for p in plist:
                con.execute("UPDATE proposals SET status='applied' WHERE id=?",
                            (p["id"],))
        except Exception as e:
            errors.append((path, "%s: %s" % (type(e).__name__, e)))
        if progress and (i % 20 == 0 or i == total - 1):
            progress(i + 1, total, path)

    # album-level: folder.jpg + finish cover proposals
    for adir, plist in album_level.items():
        for p in plist:
            if p["field"] == "folder_jpg":
                try:
                    if _export_folder_jpg(con, settings, adir):
                        db.log_change(con, None, adir, "folder_jpg",
                                      [], ["written from embedded cover"], p["source"])
                    con.execute("UPDATE proposals SET status='applied' WHERE id=?",
                                (p["id"],))
                    con.execute("UPDATE albums SET folder_jpg=1 WHERE album_dir=?",
                                (adir,))
                except Exception as e:
                    errors.append((adir, "folder.jpg: %s" % e))
            elif p["field"] == "cover":
                if adir in cover_for_album:
                    mime, data, _ = cover_for_album[adir]
                    con.execute("UPDATE proposals SET status='applied' WHERE id=?",
                                (p["id"],))
                    if settings["write_folder_jpg"]:
                        _write_folder_jpg_bytes(adir, data,
                                                settings["overwrite_folder_jpg"])
                        con.execute("UPDATE albums SET folder_jpg=1 WHERE album_dir=?",
                                    (adir,))
                affected_albums.add(adir)

    db.finish_batch(con, batch_id, n_files)
    con.commit()
    if affected_albums:
        rules.evaluate(con, settings, album_dirs=list(affected_albums))
    return {"files": n_files, "changes": n_changes, "errors": errors,
            "batch_id": batch_id}


def _write_folder_jpg_bytes(adir, data, overwrite):
    target = Path(adir) / "folder.jpg"
    if target.exists() and not overwrite:
        return False
    target.write_bytes(bytes(data))
    return True


def _export_folder_jpg(con, settings, adir):
    """folder.jpg from the largest embedded cover of the album's tracks."""
    best = None
    for (path,) in con.execute(
            "SELECT path FROM tracks WHERE album_dir=? AND missing=0", (adir,)):
        got = tagio.get_cover_data(path)
        if got and (best is None or len(got[1]) > len(best[1])):
            best = got
    if best is None:
        return False
    return _write_folder_jpg_bytes(adir, best[1], settings["overwrite_folder_jpg"])


def album_history(con, album_dir):
    """Version batches for an album, newest first.
    Returns [{'batch_id', 'when', 'kind', 'n_tracks'}]."""
    rows = con.execute(
        "SELECT s.scan_id, MIN(s.taken_at), COUNT(DISTINCT s.track_id), sc.kind"
        " FROM snapshots s JOIN tracks t ON t.id = s.track_id"
        " LEFT JOIN scans sc ON sc.id = s.scan_id"
        " WHERE t.album_dir=? GROUP BY s.scan_id ORDER BY s.scan_id DESC",
        (album_dir,)).fetchall()
    return [{"batch_id": r[0], "when": r[1], "n_tracks": r[2],
             "kind": r[3] or "?"} for r in rows]


def album_state_at(con, album_dir, batch_id):
    """{'track_id': {'file', 'tags', 'snap_id'}} as of a batch."""
    out = {}
    for tid, fname in con.execute(
            "SELECT id, filename FROM tracks WHERE album_dir=?", (album_dir,)):
        snap = db.snapshot_at_batch(con, tid, batch_id)
        if snap:
            out[tid] = {"file": fname, "tags": snap["tags"],
                        "snap_id": snap["id"]}
    return out


def revert_album(con, settings, album_dir, batch_id, progress=None):
    """Rewrite the album's files back to their state at `batch_id`.
    Restores text fields, a removed old ID3v1 tag, and a replaced cover
    (when its picture was remembered at replacement time)."""
    target = album_state_at(con, album_dir, batch_id)
    new_batch = db.new_batch(con, "revert", "album %s to batch %d"
                             % (Path(album_dir).name, batch_id))
    n_files = n_changes = 0
    errors = []
    items = list(target.items())
    for i, (tid, info) in enumerate(items):
        row = con.execute("SELECT path FROM tracks WHERE id=? AND missing=0",
                          (tid,)).fetchone()
        if row is None:
            continue
        path = row[0]
        tgt = info["tags"]
        current = db.latest_snapshot(con, tid)["tags"]
        changes = {}
        for field in tagio.EDITABLE_FIELDS:
            if field not in tgt:
                continue   # snapshot predates this field being tracked - leave it alone
            if current.get(field, []) != tgt[field]:
                changes[field] = tgt[field]
        # restore a removed old ID3v1 tag exactly as it was recorded
        keep_v1 = None
        if tgt.get("_has_v1") and tgt.get("_v1") and not current.get("_has_v1"):
            keep_v1 = tagio.build_id3v1(tgt["_v1"])
            changes.setdefault("_id3v1", ["restore"])
        # restore a removed APEv2 tag from its recorded contents
        keep_ape = None
        if tgt.get("_has_ape") and tgt.get("_ape") and not current.get("_has_ape"):
            keep_ape = tgt["_ape"]
            changes.setdefault("_apev2", ["restore"])
        # restore a replaced cover if the old picture was remembered
        def _cov_sig(tags):
            return [(c.get("bytes"), c.get("w"), c.get("h"))
                    for c in tags.get("_cover", [])]
        if _cov_sig(tgt) != _cov_sig(current):
            blob = db.cover_blob_for_snapshot(con, info["snap_id"])
            if blob:
                changes["cover"] = blob
        if not changes:
            continue
        try:
            n_changes += _write_track(con, settings, new_batch, tid, path, changes,
                                      {f: "revert" for f in changes},
                                      reason="reverted",
                                      keep_v1=keep_v1 if keep_v1 else "auto",
                                      keep_ape=keep_ape)
            n_files += 1
        except Exception as e:
            errors.append((path, str(e)))
        if progress:
            progress(i + 1, len(items), path)
    db.finish_batch(con, new_batch, n_files)
    con.commit()
    rules.evaluate(con, settings, album_dirs=[album_dir])
    return {"files": n_files, "changes": n_changes, "errors": errors}


def resolve_v1_keep_v2(con, settings, entries):
    """User chose 'keep ID3v2' for v1/v2 conflicts. Recorded as a lightweight
    decision (NOT an exception): the row stays visible as "ID3v2 stays", the
    removal becomes applicable, and the marker clears itself once the old tag
    is really gone."""
    albums = set()
    for e in entries:
        if e.get("rule") != "id3v1_conflict" or not e.get("track_id"):
            continue
        con.execute("INSERT OR IGNORE INTO v1_keep_v2(track_id, created_at)"
                    " VALUES (?,?)", (e["track_id"], db.now()))
        con.execute("DELETE FROM proposals WHERE track_id=? AND"
                    " rule='id3v1_conflict' AND status IN"
                    " ('pending','edited','postponed','needs_input')",
                    (e["track_id"],))
        if e.get("album_dir"):
            albums.add(e["album_dir"])
    con.commit()
    if albums:
        rules.evaluate(con, settings, album_dirs=list(albums))


def resolve_ape_keep_v2(con, settings, entries):
    """User chose 'keep ID3v2' for APEv2/v2 conflicts. Same lightweight marker
    as resolve_v1_keep_v2 - the APEv2 tag becomes removable and the marker
    clears itself once the tag is really gone."""
    albums = set()
    for e in entries:
        if e.get("rule") != "apev2_conflict" or not e.get("track_id"):
            continue
        con.execute("INSERT OR IGNORE INTO ape_keep_v2(track_id, created_at)"
                    " VALUES (?,?)", (e["track_id"], db.now()))
        con.execute("DELETE FROM proposals WHERE track_id=? AND"
                    " rule='apev2_conflict' AND status IN"
                    " ('pending','edited','postponed','needs_input')",
                    (e["track_id"],))
        if e.get("album_dir"):
            albums.add(e["album_dir"])
    con.commit()
    if albums:
        rules.evaluate(con, settings, album_dirs=list(albums))


def set_manual_proposal(con, track_id, field, new_values):
    """User edited a proposed value in the GUI (track level)."""
    row = con.execute("SELECT path, artist_folder, album_dir FROM tracks WHERE id=?",
                      (track_id,)).fetchone()
    if row is None:
        return
    path, artist, adir = row
    snap = db.latest_snapshot(con, track_id)
    current = snap["tags"].get(field, []) if snap else []
    qs = ",".join("'%s'" % s for s in db.ALL_OPEN_STATUSES)
    existing = con.execute(
        "SELECT id FROM proposals WHERE track_id=? AND field=?"
        " AND status IN (%s)" % qs, (track_id, field)).fetchone()
    cur_j = json.dumps(current, ensure_ascii=False)
    new_j = json.dumps(new_values, ensure_ascii=False)
    if new_values == current:
        if existing:
            con.execute("DELETE FROM proposals WHERE id=?", (existing[0],))
    elif existing:
        # a user edit always wins and becomes applicable (also un-postpones,
        # and turns a needs-input placeholder into a real proposal)
        con.execute("UPDATE proposals SET current=?, proposed=?, source='manual',"
                    " status='edited' WHERE id=?", (cur_j, new_j, existing[0]))
    else:
        con.execute(
            "INSERT INTO proposals(track_id, artist_folder, album_dir, field,"
            " current, proposed, source, status, created_at, rule)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (track_id, artist, adir, field, cur_j, new_j, "manual", "edited",
             db.now(), "manual"))
    con.commit()


def set_proposal_status(con, prop_ids, status):
    """Postpone / restore proposals ('postponed' <-> 'pending')."""
    if not prop_ids:
        return
    qs = ",".join("?" * len(prop_ids))
    con.execute("UPDATE proposals SET status=? WHERE id IN (%s)" % qs,
                [status] + list(prop_ids))
    con.commit()


def mark_exceptions(con, settings, entries):
    """Turn panel entries (proposal or issue rows) into permanent exceptions.
    Suppressed at every future rule evaluation until removed again."""
    albums = set()
    for e in entries:
        db.add_exception(con, e.get("artist_folder") or "", e.get("album_dir"),
                         e.get("track_id"), e["rule"], e.get("field"),
                         info="%s — %s" % (e.get("file", ""), e.get("label", e["rule"])))
        if e.get("prop_id"):
            con.execute("UPDATE proposals SET status='exception' WHERE id=?",
                        (e["prop_id"],))
        if e.get("album_dir"):
            albums.add(e["album_dir"])
    con.commit()
    if albums:
        rules.evaluate(con, settings, album_dirs=list(albums))


def remove_exception(con, settings, exception_id):
    row = con.execute("SELECT album_dir, artist_folder FROM exceptions WHERE id=?",
                      (exception_id,)).fetchone()
    con.execute("DELETE FROM exceptions WHERE id=?", (exception_id,))
    con.commit()
    if row:
        if row[0]:
            rules.evaluate(con, settings, album_dirs=[row[0]])
        elif row[1]:
            rules.evaluate(con, settings, artist_folders=[row[1]])

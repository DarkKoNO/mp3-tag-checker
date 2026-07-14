"""SQLite storage: tracks, tag snapshots (history), issues, proposals, changelog."""

import json
import sqlite3
import time

from .settings import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans(
    id INTEGER PRIMARY KEY,
    started_at TEXT, finished_at TEXT,
    kind TEXT,              -- 'scan' | 'apply' | 'revert'
    n_files INTEGER DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS artists(
    folder TEXT PRIMARY KEY,
    artist_jpg INTEGER DEFAULT 0,
    last_scan_id INTEGER
);
CREATE TABLE IF NOT EXISTS albums(
    album_dir TEXT PRIMARY KEY,
    artist_folder TEXT,
    folder_jpg INTEGER DEFAULT 0,   -- cover file in the dir or its parent
    cover_path TEXT,                -- resolved path of that cover file
    cover_w INTEGER, cover_h INTEGER,  -- its pixel size (for resolution compare)
    last_scan_id INTEGER
);
CREATE TABLE IF NOT EXISTS tracks(
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE,
    artist_folder TEXT,
    album_dir TEXT,
    filename TEXT,
    size INTEGER, mtime REAL,
    last_scan_id INTEGER,
    missing INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album_dir);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist_folder);
CREATE TABLE IF NOT EXISTS snapshots(
    id INTEGER PRIMARY KEY,
    track_id INTEGER,
    scan_id INTEGER,        -- batch this snapshot belongs to
    taken_at TEXT,
    reason TEXT,            -- 'scan' | 'applied' | 'reverted'
    tags TEXT               -- JSON dict of the full tag state
);
CREATE INDEX IF NOT EXISTS idx_snap_track ON snapshots(track_id, scan_id);
CREATE TABLE IF NOT EXISTS cover_blobs(
    snapshot_id INTEGER,
    mime TEXT, data BLOB
);
CREATE TABLE IF NOT EXISTS issues(
    id INTEGER PRIMARY KEY,
    track_id INTEGER,       -- NULL for album/artist level
    artist_folder TEXT,
    album_dir TEXT,         -- NULL for artist level
    rule TEXT,
    severity TEXT,          -- 'red' | 'yellow'
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_artist ON issues(artist_folder);
CREATE TABLE IF NOT EXISTS proposals(
    id INTEGER PRIMARY KEY,
    track_id INTEGER,       -- NULL for album-level (cover, folder_jpg)
    artist_folder TEXT,
    album_dir TEXT,
    field TEXT,
    current TEXT,           -- JSON list (display form for pseudo fields)
    proposed TEXT,          -- JSON list
    source TEXT,            -- 'rule' | 'manual' | 'online'
    status TEXT DEFAULT 'pending',  -- 'pending' | 'edited' | 'applied' | 'rejected'
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_prop_track ON proposals(track_id);
CREATE INDEX IF NOT EXISTS idx_prop_album ON proposals(album_dir);
CREATE TABLE IF NOT EXISTS pending_covers(
    id INTEGER PRIMARY KEY,
    album_dir TEXT,
    mime TEXT, data BLOB,
    note TEXT
);
CREATE TABLE IF NOT EXISTS exceptions(
    id INTEGER PRIMARY KEY,
    artist_folder TEXT,
    album_dir TEXT,         -- NULL for artist-level rules
    track_id INTEGER,       -- NULL for album/artist-level rules
    rule TEXT,
    field TEXT,             -- NULL when the whole rule is excepted
    info TEXT,              -- human-readable description of what was excepted
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS changelog(
    id INTEGER PRIMARY KEY,
    ts TEXT,
    track_id INTEGER,
    path TEXT,
    field TEXT,
    old TEXT, new TEXT,
    origin TEXT             -- 'rule' | 'manual' | 'online' | 'revert' | 'file'
);
"""


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path=None):
    con = sqlite3.connect(db_path if db_path is not None else DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con):
    cols = [r[1] for r in con.execute("PRAGMA table_info(proposals)")]
    if "rule" not in cols:
        con.execute("ALTER TABLE proposals ADD COLUMN rule TEXT")
    if "note" not in cols:
        con.execute("ALTER TABLE proposals ADD COLUMN note TEXT")
    con.execute("DELETE FROM exceptions WHERE rule='id3v1_conflict'")
    acols = [r[1] for r in con.execute("PRAGMA table_info(albums)")]
    for col, decl in (("cover_path", "TEXT"), ("cover_w", "INTEGER"),
                      ("cover_h", "INTEGER")):
        if col not in acols:
            con.execute("ALTER TABLE albums ADD COLUMN %s %s" % (col, decl))
    # Per-field "keep ID3v2" decisions for the v1/v2 and APEv2/v2 conflict flows:
    # a lightweight, reversible marker (NOT an exception) recording that the user
    # decided to keep the current ID3v2 value of THIS field over the old/foreign
    # tag's value. One row per (track, field) so decisions are independent and
    # can be switched back. Older track-level tables (no 'field' column) are
    # replaced - the coarse markers are dropped and the user re-decides.
    for tbl in ("v1_keep_v2", "ape_keep_v2"):
        cols = [r[1] for r in con.execute("PRAGMA table_info(%s)" % tbl)]
        if cols and "field" not in cols:
            con.execute("DROP TABLE %s" % tbl)
        con.execute("CREATE TABLE IF NOT EXISTS %s("
                    "track_id INTEGER, field TEXT, created_at TEXT,"
                    " PRIMARY KEY(track_id, field))" % tbl)
    con.commit()


def new_batch(con, kind, note=""):
    cur = con.execute(
        "INSERT INTO scans(started_at, kind, note) VALUES (?,?,?)",
        (now(), kind, note))
    return cur.lastrowid


def finish_batch(con, batch_id, n_files):
    con.execute("UPDATE scans SET finished_at=?, n_files=? WHERE id=?",
                (now(), n_files, batch_id))


def add_snapshot(con, track_id, scan_id, reason, tags_dict, keep=10):
    cur = con.execute(
        "INSERT INTO snapshots(track_id, scan_id, taken_at, reason, tags)"
        " VALUES (?,?,?,?,?)",
        (track_id, scan_id, now(), reason,
         json.dumps(tags_dict, ensure_ascii=False)))
    snap_id = cur.lastrowid
    # prune: keep the newest `keep` snapshots per track
    old = [r[0] for r in con.execute(
        "SELECT id FROM snapshots WHERE track_id=? ORDER BY id DESC LIMIT -1 OFFSET ?",
        (track_id, keep))]
    if old:
        qs = ",".join("?" * len(old))
        con.execute("DELETE FROM snapshots WHERE id IN (%s)" % qs, old)
        con.execute("DELETE FROM cover_blobs WHERE snapshot_id IN (%s)" % qs, old)
    return snap_id


def latest_snapshot(con, track_id):
    row = con.execute(
        "SELECT id, scan_id, taken_at, reason, tags FROM snapshots"
        " WHERE track_id=? ORDER BY id DESC LIMIT 1", (track_id,)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "scan_id": row[1], "taken_at": row[2],
            "reason": row[3], "tags": json.loads(row[4])}


def snapshot_at_batch(con, track_id, batch_id):
    """State of a track as of batch `batch_id` (latest snapshot with scan_id <= it)."""
    row = con.execute(
        "SELECT id, scan_id, taken_at, reason, tags FROM snapshots"
        " WHERE track_id=? AND scan_id<=? ORDER BY scan_id DESC, id DESC LIMIT 1",
        (track_id, batch_id)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "scan_id": row[1], "taken_at": row[2],
            "reason": row[3], "tags": json.loads(row[4])}


def extra_fields(con):
    """Every dynamic tag key (named comments, TXXX, lyrics — see tagio) present
    in the library's current state, sorted. Used to offer them for search."""
    from .tagio import EXTRA_PREFIX
    keys = set()
    # only snapshots that actually mention one are parsed
    for (tags_json,) in con.execute(
            "SELECT tags FROM snapshots WHERE id IN"
            " (SELECT MAX(id) FROM snapshots GROUP BY track_id)"
            " AND tags LIKE ?", ('%"' + EXTRA_PREFIX + '%',)):
        keys.update(k for k in json.loads(tags_json)
                    if k.startswith(EXTRA_PREFIX))
    return sorted(keys)


def log_change(con, track_id, path, field, old, new, origin):
    con.execute(
        "INSERT INTO changelog(ts, track_id, path, field, old, new, origin)"
        " VALUES (?,?,?,?,?,?,?)",
        (now(), track_id, path, field,
         json.dumps(old, ensure_ascii=False),
         json.dumps(new, ensure_ascii=False), origin))


OPEN_STATUSES = ("pending", "edited")
ALL_OPEN_STATUSES = ("pending", "edited", "postponed", "needs_input")


def online_condition(online_filter):
    """SQL fragment filtering internet proposals (rule 'online_meta') by kind.
    online_filter: None = show all; else a set with 'add' (fills empty fields,
    current == []) and/or 'diff' (changes an existing value)."""
    if online_filter is None:
        return ""
    add = "add" in online_filter
    diff = "diff" in online_filter
    if add and diff:
        return ""
    keep = " OR current='[]'" if add else (" OR current<>'[]'" if diff else "")
    return " AND (rule IS NULL OR rule<>'online_meta'%s)" % keep


def rules_condition(exclude_rules):
    """SQL fragment hiding proposals of the given rules (e.g. image rules)."""
    if not exclude_rules:
        return ""
    quoted = ",".join("'%s'" % r for r in exclude_rules)
    return " AND (rule IS NULL OR rule NOT IN (%s))" % quoted


def open_proposals(con, artist_folders=None, album_dirs=None, track_ids=None,
                   rule=None, field=None, statuses=OPEN_STATUSES,
                   online_filter=None, exclude_rules=None, prop_ids=None):
    """Proposals in the given statuses, optionally restricted in scope.
    Default statuses = the ones Apply processes (pending/edited).
    online_filter: see online_condition(); exclude_rules hides whole rules;
    prop_ids restricts to explicitly chosen proposal rows."""
    where = ["status IN (%s)" % ",".join("?" * len(statuses))]
    params = list(statuses)
    if prop_ids is not None:
        where.append("id IN (%s)" % ",".join("?" * len(prop_ids)))
        params += list(prop_ids)
    if artist_folders is not None:
        where.append("artist_folder IN (%s)" % ",".join("?" * len(artist_folders)))
        params += list(artist_folders)
    if album_dirs is not None:
        where.append("album_dir IN (%s)" % ",".join("?" * len(album_dirs)))
        params += list(album_dirs)
    if track_ids is not None:
        where.append("track_id IN (%s)" % ",".join("?" * len(track_ids)))
        params += list(track_ids)
    if rule is not None:
        where.append("rule=?")
        params.append(rule)
    if field is not None:
        where.append("field=? AND rule IS NULL")
        params.append(field)
    rows = con.execute(
        "SELECT id, track_id, artist_folder, album_dir, field, current, proposed,"
        " source, status, rule, note FROM proposals WHERE " + " AND ".join(where)
        + online_condition(online_filter) + rules_condition(exclude_rules), params)
    return [{"id": r[0], "track_id": r[1], "artist_folder": r[2], "album_dir": r[3],
             "field": r[4], "current": json.loads(r[5]), "proposed": json.loads(r[6]),
             "source": r[7], "status": r[8], "rule": r[9], "note": r[10]}
            for r in rows]


def upsert_proposal(con, track_id, artist_folder, album_dir, field,
                    current, proposed, source, status="pending", rule=None,
                    note=None):
    """Create or update a proposal for track+field (album-level: track_id None).
    User decisions survive regeneration: 'edited' keeps its value, 'postponed'
    keeps its status - but only while the SAME rule is proposing (a different
    rule taking over the field starts fresh); 'needs_input' upgrades once a
    value becomes available."""
    qs = ",".join("'%s'" % s for s in ALL_OPEN_STATUSES)
    if track_id is None:
        row = con.execute(
            "SELECT id, status, rule FROM proposals WHERE track_id IS NULL"
            " AND album_dir=? AND field=? AND status IN (%s)" % qs,
            (album_dir, field)).fetchone()
    else:
        row = con.execute(
            "SELECT id, status, rule FROM proposals WHERE track_id=? AND field=?"
            " AND status IN (%s)" % qs, (track_id, field)).fetchone()
    cur_j = json.dumps(current, ensure_ascii=False)
    prop_j = json.dumps(proposed, ensure_ascii=False)
    if row:
        rid, old_status, old_rule = row
        if old_status == "edited":
            con.execute("UPDATE proposals SET current=? WHERE id=?", (cur_j, rid))
            return rid
        if old_status == "postponed" and (old_rule or None) == (rule or None):
            new_status = "postponed"
        elif old_status == "needs_input" and not proposed:
            new_status = "needs_input"
        else:
            new_status = status
        con.execute("UPDATE proposals SET current=?, proposed=?, source=?, status=?,"
                    " rule=COALESCE(?, rule), note=COALESCE(?, note) WHERE id=?",
                    (cur_j, prop_j, source, new_status, rule, note, rid))
        return rid
    cur = con.execute(
        "INSERT INTO proposals(track_id, artist_folder, album_dir, field, current,"
        " proposed, source, status, created_at, rule, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (track_id, artist_folder, album_dir, field, cur_j, prop_j, source, status,
         now(), rule, note))
    return cur.lastrowid


def save_cover_blob(con, snapshot_id, mime, data):
    """Remember the cover being replaced so a revert can bring it back."""
    if con.execute("SELECT 1 FROM cover_blobs WHERE snapshot_id=? LIMIT 1",
                   (snapshot_id,)).fetchone() is None:
        con.execute("INSERT INTO cover_blobs(snapshot_id, mime, data)"
                    " VALUES (?,?,?)", (snapshot_id, mime, data))


def cover_blob_for_snapshot(con, snapshot_id):
    row = con.execute("SELECT mime, data FROM cover_blobs WHERE snapshot_id=?",
                      (snapshot_id,)).fetchone()
    return (row[0], bytes(row[1])) if row else None


def remove_scope(con, artist_folders=None, album_dirs=None, track_ids=None):
    """Remove artists / albums / tracks from the library DATABASE only.
    Files on disk are never touched; scanning again re-adds everything.
    Exceptions and the change log are kept. Returns (artists, albums, tracks)
    counts of what was removed."""
    arts = set(artist_folders or [])
    adirs = set(album_dirs or [])
    tids = set(track_ids or [])
    if arts:
        qs = ",".join("?" * len(arts))
        adirs |= {r[0] for r in con.execute(
            "SELECT DISTINCT album_dir FROM tracks WHERE artist_folder IN (%s)"
            % qs, list(arts))}
    if adirs:
        qs = ",".join("?" * len(adirs))
        tids |= {r[0] for r in con.execute(
            "SELECT id FROM tracks WHERE album_dir IN (%s)" % qs, list(adirs))}
    if tids:
        qs = ",".join("?" * len(tids))
        snap_ids = [r[0] for r in con.execute(
            "SELECT id FROM snapshots WHERE track_id IN (%s)" % qs, list(tids))]
        if snap_ids:
            q2 = ",".join("?" * len(snap_ids))
            con.execute("DELETE FROM cover_blobs WHERE snapshot_id IN (%s)" % q2,
                        snap_ids)
        con.execute("DELETE FROM snapshots WHERE track_id IN (%s)" % qs, list(tids))
        con.execute("DELETE FROM proposals WHERE track_id IN (%s)" % qs, list(tids))
        con.execute("DELETE FROM issues WHERE track_id IN (%s)" % qs, list(tids))
        con.execute("DELETE FROM v1_keep_v2 WHERE track_id IN (%s)" % qs, list(tids))
        con.execute("DELETE FROM ape_keep_v2 WHERE track_id IN (%s)" % qs, list(tids))
        con.execute("DELETE FROM tracks WHERE id IN (%s)" % qs, list(tids))
    if adirs:
        qs = ",".join("?" * len(adirs))
        con.execute("DELETE FROM albums WHERE album_dir IN (%s)" % qs, list(adirs))
        con.execute("DELETE FROM proposals WHERE album_dir IN (%s)" % qs, list(adirs))
        con.execute("DELETE FROM issues WHERE album_dir IN (%s)" % qs, list(adirs))
        con.execute("DELETE FROM pending_covers WHERE album_dir IN (%s)" % qs,
                    list(adirs))
    if arts:
        qs = ",".join("?" * len(arts))
        con.execute("DELETE FROM artists WHERE folder IN (%s)" % qs, list(arts))
        con.execute("DELETE FROM issues WHERE artist_folder IN (%s)" % qs,
                    list(arts))
    con.commit()
    return len(arts), len(adirs), len(tids)


def purge_gone(con, track_ids):
    """Remove the given no-longer-on-disk tracks from the database, then drop
    orphaned album-level leftovers (proposals/issues/album facts whose album
    folder has no tracks at all any more — e.g. entries recorded under an old
    path before files were renamed). Change log and exceptions stay."""
    if track_ids:
        remove_scope(con, track_ids=list(track_ids))
    for table, col in (("proposals", "album_dir"), ("issues", "album_dir"),
                       ("albums", "album_dir"), ("pending_covers", "album_dir")):
        con.execute(
            "DELETE FROM %s WHERE %s IS NOT NULL AND %s NOT IN"
            " (SELECT DISTINCT album_dir FROM tracks)" % (table, col, col))
    con.commit()


def add_exception(con, artist_folder, album_dir, track_id, rule, field, info=""):
    con.execute(
        "INSERT INTO exceptions(artist_folder, album_dir, track_id, rule, field,"
        " info, created_at) VALUES (?,?,?,?,?,?,?)",
        (artist_folder, album_dir, track_id, rule, field, info, now()))


def load_exceptions(con, artist_folders=None):
    """{(artist, album_dir_or_None, track_id_or_None, rule)} for suppression checks."""
    where, params = "1=1", []
    if artist_folders:
        where = "artist_folder IN (%s)" % ",".join("?" * len(artist_folders))
        params = list(artist_folders)
    return {(r[0], r[1], r[2], r[3]) for r in con.execute(
        "SELECT artist_folder, album_dir, track_id, rule FROM exceptions"
        " WHERE " + where, params)}

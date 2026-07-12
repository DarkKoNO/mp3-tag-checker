"""Scanning: walk the listed artist folders, read tags of new/changed files,
store snapshots, then run the rule engine."""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import db, rules, tagio

COVER_NAMES = {"folder.jpg", "folder.jpeg", "folder.png", "cover.jpg", "cover.jpeg",
               "cover.png", "front.jpg", "front.jpeg", "front.png", "album.jpg"}
ARTIST_IMG_NAMES = {"artist.jpg", "artist.jpeg", "artist.png"}


def collect(root: Path, entries):
    """Walk artist folders. Returns (files, missing_folders, artist_facts,
    album_facts, errors). files: list of (path, artist_folder, album_dir,
    size, mtime); errors: list of (path, message) for files/folders that
    exist but cannot be read or safely addressed."""
    files = []
    missing = []
    errors = []
    artist_facts = {}   # entry -> {'artist_jpg': bool}
    album_facts = {}    # album_dir -> {'artist_folder', 'folder_jpg': bool}
    dir_has_cover = {}

    for entry in entries:
        folder = root / entry
        if not folder.is_dir():
            missing.append(entry)
            continue
        artist_facts[entry] = {"artist_jpg": False}

        def on_walk_error(err, _entry=entry):
            errors.append((getattr(err, "filename", None) or str(root / _entry),
                           "folder could not be listed: %s" % err))

        for dirpath, _dirs, filenames in os.walk(folder, onerror=on_walk_error):
            names_lower = {n.lower() for n in filenames}
            dir_has_cover[dirpath] = bool(names_lower & COVER_NAMES)
            if dirpath == str(folder) and names_lower & ARTIST_IMG_NAMES:
                artist_facts[entry]["artist_jpg"] = True
            mp3_here = False
            for name in filenames:
                if name.lower().endswith(".mp3"):
                    p = os.path.join(dirpath, name)
                    # a name the filesystem serves but path functions would
                    # split differently (separator or other reserved
                    # characters smuggled in, e.g. by a NAS) must not enter
                    # the database as a broken entry
                    if os.path.basename(p) != name or os.path.dirname(p) != dirpath:
                        errors.append((p, "file name contains characters that"
                                       " cannot be addressed safely — please"
                                       " rename the file"))
                        continue
                    try:
                        st = os.stat(p)
                    except OSError as e:
                        errors.append((p, "file cannot be read: %s" % e))
                        continue
                    files.append((p, entry, dirpath, st.st_size, st.st_mtime))
                    mp3_here = True
            if mp3_here:
                album_facts[dirpath] = {"artist_folder": entry, "folder_jpg": False}

    for adir, facts in album_facts.items():
        parent = str(Path(adir).parent)
        facts["folder_jpg"] = dir_has_cover.get(adir) or dir_has_cover.get(parent, False)
    return files, missing, artist_facts, album_facts, errors


def scan(con, settings, root, entries, progress=None, full=False, workers=8,
         auto_remove_gone=False):
    """Incremental scan of `entries` folders under `root`.
    progress(done, total, text). Returns summary dict.
    auto_remove_gone: when an album's whole folder no longer exists on disk,
    remove its database entry (files already gone). Albums that merely lost some
    tracks are never removed — those tracks stay in the library marked missing."""
    root = Path(root)
    scan_id = db.new_batch(con, "scan", "%d folders" % len(entries))

    if progress:
        progress(0, 0, "Collecting file list...")
    files, missing, artist_facts, album_facts, errors = collect(root, entries)

    # update artist/album fact tables
    for entry, facts in artist_facts.items():
        con.execute("INSERT INTO artists(folder, artist_jpg, last_scan_id) VALUES (?,?,?)"
                    " ON CONFLICT(folder) DO UPDATE SET artist_jpg=?, last_scan_id=?",
                    (entry, int(facts["artist_jpg"]), scan_id,
                     int(facts["artist_jpg"]), scan_id))
    for adir, facts in album_facts.items():
        con.execute("INSERT INTO albums(album_dir, artist_folder, folder_jpg, last_scan_id)"
                    " VALUES (?,?,?,?) ON CONFLICT(album_dir) DO UPDATE SET"
                    " artist_folder=?, folder_jpg=?, last_scan_id=?",
                    (adir, facts["artist_folder"], int(facts["folder_jpg"]), scan_id,
                     facts["artist_folder"], int(facts["folder_jpg"]), scan_id))

    # which files need (re)reading?
    known = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
        "SELECT path, id, size, mtime FROM tracks")}
    to_read = []
    for p, artist, adir, size, mtime in files:
        old = known.get(p)
        if full or old is None or old[1] != size or abs(old[2] - mtime) > 1:
            to_read.append((p, artist, adir, size, mtime))

    n_new = 0
    n_read = len(to_read)
    if progress:
        progress(0, n_read, "Reading tags of %d new/changed files..." % n_read)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(tagio.read_tags, p): (p, artist, adir, size, mtime)
                   for p, artist, adir, size, mtime in to_read}
        for fut in as_completed(futures):
            p, artist, adir, size, mtime = futures[fut]
            done += 1
            try:
                tags = fut.result()
            except Exception as e:
                errors.append((p, "%s: %s" % (type(e).__name__, e)))
                if progress and (done % 50 == 0 or done == n_read):
                    progress(done, n_read, p)
                continue
            row = con.execute("SELECT id FROM tracks WHERE path=?", (p,)).fetchone()
            if row:
                track_id = row[0]
                con.execute("UPDATE tracks SET size=?, mtime=?, last_scan_id=?,"
                            " missing=0, artist_folder=?, album_dir=? WHERE id=?",
                            (size, mtime, scan_id, artist, adir, track_id))
            else:
                n_new += 1
                track_id = con.execute(
                    "INSERT INTO tracks(path, artist_folder, album_dir, filename,"
                    " size, mtime, last_scan_id) VALUES (?,?,?,?,?,?,?)",
                    (p, artist, adir, os.path.basename(p), size, mtime, scan_id)
                ).lastrowid
            db.add_snapshot(con, track_id, scan_id, "scan", tags,
                            keep=settings["history_keep"])
            if progress and (done % 50 == 0 or done == n_read):
                progress(done, n_read, p)

    # refresh scan stamp of unchanged files; mark vanished files of scanned artists
    found_paths = {p for p, *_ in files}
    scanned_artists = list(artist_facts.keys())
    for p, artist, adir, size, mtime in files:
        con.execute("UPDATE tracks SET last_scan_id=?, missing=0 WHERE path=?",
                    (scan_id, p))
    gone = []       # (track_id, path) of every not-found file in scan scope
    if scanned_artists:
        qs = ",".join("?" * len(scanned_artists))
        for (p, tid, was_missing) in con.execute(
                "SELECT path, id, missing FROM tracks WHERE artist_folder IN (%s)"
                % qs, scanned_artists).fetchall():
            if p not in found_paths:
                if not was_missing:
                    con.execute("UPDATE tracks SET missing=1 WHERE id=?", (tid,))
                gone.append((tid, p))

    # whole gone albums: the folder itself no longer exists -> optional removal.
    # Partial-missing albums (folder still there, some tracks gone) are kept and
    # left marked missing, so they show up gray instead of silently disappearing.
    removed_albums = []
    if auto_remove_gone and gone:
        gone_albums = {r[0] for r in con.execute(
            "SELECT DISTINCT album_dir FROM tracks WHERE missing=1 AND"
            " artist_folder IN (%s)" % ",".join("?" * len(scanned_artists)),
            scanned_artists)} if scanned_artists else set()
        for adir in gone_albums:
            if not Path(adir).is_dir():          # the whole album folder is gone
                removed_albums.append(adir)
        if removed_albums:
            db.remove_scope(con, album_dirs=removed_albums)
            # those tracks are gone from the DB now: drop them from the manual
            # "remove deleted files" offer so it does not double-count them
            gone = [(tid, p) for tid, p in gone if con.execute(
                "SELECT 1 FROM tracks WHERE id=?", (tid,)).fetchone()]

    db.finish_batch(con, scan_id, len(files))
    con.commit()

    if progress:
        progress(0, 0, "Evaluating rules...")
    rules.evaluate(con, settings, artist_folders=scanned_artists)
    con.commit()

    return {"scan_id": scan_id, "files": len(files), "read": n_read, "new": n_new,
            "missing_folders": missing, "errors": errors, "gone": gone,
            "removed_albums": removed_albums}

"""Online metadata: MusicBrainz release search, Cover Art Archive covers,
Deezer artist images. All on-demand, never automatic."""

import time

import requests

UA = {"User-Agent": "MP3TaggerJK/0.1 (personal library tool)"}
MB_URL = "https://musicbrainz.org/ws/2"
_last_mb = [0.0]


def _mb_get(path, params):
    # MusicBrainz allows 1 request/second
    wait = 1.1 - (time.time() - _last_mb[0])
    if wait > 0:
        time.sleep(wait)
    r = requests.get(MB_URL + path, params={**params, "fmt": "json"},
                     headers=UA, timeout=20)
    _last_mb[0] = time.time()
    r.raise_for_status()
    return r.json()


def search_releases(artist, album, limit=6):
    """[{'id','title','artist','date','country','format'}] best matches first."""
    q = 'release:"%s" AND artist:"%s"' % (album.replace('"', ""), artist.replace('"', ""))
    data = _mb_get("/release", {"query": q, "limit": limit})
    out = []
    for rel in data.get("releases", []):
        credit = "".join(c.get("name", "") + c.get("joinphrase", "")
                         for c in rel.get("artist-credit", []))
        media = rel.get("media") or []
        out.append({
            "id": rel["id"],
            "title": rel.get("title", ""),
            "artist": credit,
            "date": rel.get("date", ""),
            "country": rel.get("country", ""),
            "format": media[0].get("format", "") if media else "",
            "score": rel.get("score", 0),
        })
    return out


def fetch_cover(release_id, size=500):
    """(mime, bytes) from Cover Art Archive, or None."""
    url = "https://coverartarchive.org/release/%s/front-%d" % (release_id, size)
    r = requests.get(url, headers=UA, timeout=30, allow_redirects=True)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    return mime, r.content


def deezer_artist_candidates(name, limit=6):
    """[{'source','label','url'}] artist picture candidates from Deezer."""
    r = requests.get("https://api.deezer.com/search/artist",
                     params={"q": name, "limit": limit}, headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for item in r.json().get("data", []):
        url = item.get("picture_xl") or item.get("picture_big")
        if url:
            out.append({"source": "Deezer", "label": item.get("name", "?"),
                        "url": url})
    return out


def theaudiodb_artist_candidates(name):
    """[{'source','label','url'}] artist pictures from TheAudioDB (free key)."""
    r = requests.get("https://www.theaudiodb.com/api/v1/json/2/search.php",
                     params={"s": name}, headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for a in (r.json() or {}).get("artists") or []:
        for key, kind in (("strArtistThumb", "portrait"),
                          ("strArtistFanart", "fanart"),
                          ("strArtistFanart2", "fanart 2"),
                          ("strArtistWideThumb", "wide")):
            url = a.get(key)
            if url:
                out.append({"source": "TheAudioDB",
                            "label": "%s (%s)" % (a.get("strArtist", "?"), kind),
                            "url": url})
    return out


def search_artist_images(name):
    """Candidates from all online sources; a failing source is just skipped."""
    out = []
    for fn in (deezer_artist_candidates, theaudiodb_artist_candidates):
        try:
            out.extend(fn(name))
        except Exception:
            pass
    return out


def fetch_image(url):
    """Download an image; None if it is too small to be real."""
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.content if len(r.content) > 2000 else None


def fetch_release_details(release_id):
    return _mb_get("/release/%s" % release_id,
                   {"inc": "labels+recordings+release-groups"})


def artist_exists(name):
    """Does MusicBrainz know `name` as a single artist (incl. aliases)?"""
    data = _mb_get("/artist", {"query": 'artist:"%s"' % name.replace('"', ""),
                               "limit": 5})
    nl = name.strip().casefold()
    for a in data.get("artists", []):
        if a.get("name", "").casefold() == nl:
            return True
        for al in a.get("aliases") or []:
            if al.get("name", "").casefold() == nl:
                return True
    return False


def scan_album_online(con, settings, adir):
    """Internet metadata check for one album. Creates proposals with
    rule='online_meta' (own layer: hideable, clearable, never automatic).
    Also verifies pending split proposals against MusicBrainz artists."""
    from collections import Counter

    from . import db

    tracks = con.execute(
        "SELECT id, artist_folder FROM tracks WHERE album_dir=? AND missing=0",
        (adir,)).fetchall()
    if not tracks:
        return {"album": adir, "matched": False, "proposals": 0}
    artist_folder = tracks[0][1]
    snaps = {}
    names, albums_c = Counter(), Counter()
    for tid, _af in tracks:
        snap = db.latest_snapshot(con, tid)
        if not snap:
            continue
        tags = snap["tags"]
        snaps[tid] = tags
        for v in (tags.get("albumartist") or tags.get("artist") or [])[:1]:
            names[v] += 1
        for v in tags.get("album", [])[:1]:
            albums_c[v] += 1
    if not names or not albums_c:
        return {"album": adir, "matched": False, "proposals": 0}
    artist_name = names.most_common(1)[0][0]
    album_name = albums_c.most_common(1)[0][0]

    rels = search_releases(artist_name, album_name, limit=3)
    if not rels or rels[0].get("score", 0) < 85:
        return {"album": adir, "matched": False, "proposals": 0}
    rel = rels[0]
    det = fetch_release_details(rel["id"])
    year = (det.get("date") or "")[:4]
    labels = [li["label"]["name"] for li in det.get("label-info", [])
              if li.get("label") and li["label"].get("name")]
    label = labels[0] if labels else ""
    first = ((det.get("release-group") or {}).get("first-release-date") or "")[:4]
    titles = {}
    for medium in det.get("media", []):
        for tr in medium.get("tracks", []):
            if tr.get("position") and tr.get("title"):
                titles[int(tr["position"])] = tr["title"]

    note = "MusicBrainz: %s — %s (%s, score %s)" % (
        rel["artist"], rel["title"], rel.get("date", "?"), rel.get("score"))
    n = 0

    # both kinds are always gathered; the GUI toggles which are shown/applied
    def prop(tid, tags, field, value):
        nonlocal n
        if not value:
            return
        cur = tags.get(field, [])
        if cur == [value]:
            return
        db.upsert_proposal(con, tid, artist_folder, adir, field, cur, [value],
                           "online", rule="online_meta", note=note)
        n += 1

    from .rules import parse_track
    for tid, tags in snaps.items():
        prop(tid, tags, "year", year if year.isdigit() else "")
        prop(tid, tags, "publisher", label)
        if not tags.get("origdate") and first.isdigit():
            prop(tid, tags, "origdate", first)
        if not tags.get("title"):
            num, _raw = parse_track(tags.get("track", []))
            if num in titles:
                prop(tid, tags, "title", titles[num])

    # verify split proposals: is the combined name actually ONE known artist?
    for p in db.open_proposals(con, album_dirs=[adir], rule="multi_split",
                               statuses=db.ALL_OPEN_STATUSES):
        if p["field"] in ("artist", "albumartist") and len(p["current"]) == 1:
            try:
                known = artist_exists(p["current"][0])
            except Exception:
                continue
            vnote = ("MusicBrainz lists '%s' as ONE artist — splitting may be wrong"
                     if known else
                     "MusicBrainz has no single artist '%s' — split looks correct"
                     ) % p["current"][0]
            con.execute("UPDATE proposals SET note=? WHERE id=?", (vnote, p["id"]))
    con.commit()
    return {"album": adir, "matched": True, "proposals": n,
            "release": "%s (%s)" % (rel["title"], rel.get("date", "?"))}

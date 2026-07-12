"""Reading and writing MP3 tags. The only module that touches the files.

Tag state format (used in snapshots and everywhere else): a dict of
  title/artist/albumartist/album/track/disc/year/genre/comment -> list[str]
plus underscore metadata: _version, _has_v1, _length, _bitrate, _cover, _enc.
"""

import os
from collections import Counter
from io import BytesIO

import mutagen.apev2
import mutagen.id3
from mutagen.apev2 import APEv2, APENoHeaderError
from mutagen.id3 import APIC, COMM
from mutagen.mp3 import MP3
from PIL import Image

# ID3v2.4 stores the year in TDRC, formally a timestamp frame. mutagen
# silently turns any non-timestamp text ('undated', 'unknown', ...) into ''
# both when writing and when reading back, which made such values impossible
# to store even though they are configured as allowed and real-world taggers
# write free text there. Patch: keep the raw text whenever it does not parse
# as a timestamp, so it round-trips through read/write unchanged.
from mutagen.id3._specs import ID3TimeStamp as _ID3TimeStamp

if not getattr(_ID3TimeStamp, "_keeps_raw_text", False):
    _orig_set_text = _ID3TimeStamp.set_text
    _orig_get_text = _ID3TimeStamp.get_text

    def _set_text_keep_raw(self, text, *args, **kwargs):
        self._raw_text = text if isinstance(text, str) else ""
        _orig_set_text(self, text, *args, **kwargs)

    def _get_text_keep_raw(self):
        return _orig_get_text(self) or getattr(self, "_raw_text", "")

    _ID3TimeStamp.set_text = _set_text_keep_raw
    _ID3TimeStamp.get_text = _get_text_keep_raw
    _ID3TimeStamp.text = property(_get_text_keep_raw, _set_text_keep_raw)
    _ID3TimeStamp._keeps_raw_text = True

FIELD_FRAMES = {
    # primary fields, always shown in the GUI
    "title": "TIT2",
    "artist": "TPE1",
    "albumartist": "TPE2",
    "album": "TALB",
    "track": "TRCK",
    "disc": "TPOS",
    "year": "TDRC",
    "genre": "TCON",
    "comment": "COMM",
    # extended fields, shown when filled (or via 'show all fields')
    "composer": "TCOM",
    "conductor": "TPE3",
    "remixer": "TPE4",
    "lyricist": "TEXT",
    "origartist": "TOPE",
    "origdate": "TDOR",
    "grouping": "TIT1",
    "subtitle": "TIT3",
    "discsubtitle": "TSST",
    "publisher": "TPUB",
    "copyright": "TCOP",
    "language": "TLAN",
    "bpm": "TBPM",
    "isrc": "TSRC",
    "compilation": "TCMP",
    "mood": "TMOO",
    "artistsort": "TSOP",
    "albumsort": "TSOA",
    "titlesort": "TSOT",
}
EDITABLE_FIELDS = list(FIELD_FRAMES)
PRIMARY_FIELDS = EDITABLE_FIELDS[:9]
# pseudo fields carried in proposals; applying them = "just rewrite the file"
PSEUDO_FIELDS = {"_id3v1", "_apev2", "_version", "_encoding"}

# APEv2 tags (a foreign block many old rippers / MP3Gain append to MP3s) use
# free-text, case-insensitive keys. Map the standard ones onto our field names
# so an APEv2 leftover is compared against ID3v2 the same way ID3v1 is. Keys
# are matched case-folded; the first key that maps to a field wins.
APEV2_TO_FIELD = {
    "title": "title", "artist": "artist",
    "album artist": "albumartist", "albumartist": "albumartist",
    "album": "album", "track": "track", "tracknumber": "track",
    "disc": "disc", "discnumber": "disc",
    "year": "year", "date": "year",
    "genre": "genre", "comment": "comment", "composer": "composer",
}
# canonical APEv2 key for each of our fields (used to rebuild a tag on revert)
FIELD_TO_APEV2 = {
    "title": "Title", "artist": "Artist", "albumartist": "Album Artist",
    "album": "Album", "track": "Track", "disc": "Disc", "year": "Year",
    "genre": "Genre", "comment": "Comment", "composer": "Composer",
}

ENC_NAMES = {0: "latin-1", 1: "utf-16", 2: "utf-16be", 3: "utf-8"}


def has_id3v1(path):
    with open(path, "rb") as f:
        f.seek(0, 2)
        if f.tell() < 128:
            return False
        f.seek(-128, 2)
        head = f.read(4)
        return head[:3] == b"TAG" and head != b"TAG+"


def get_v1_bytes(path):
    """The raw 128-byte ID3v1 block, or None."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        if f.tell() < 128:
            return None
        f.seek(-128, 2)
        data = f.read(128)
    return data if data[:3] == b"TAG" and data[:4] != b"TAG+" else None


def build_id3v1(v1):
    """Rebuild a 128-byte ID3v1.1 block from the dict read_id3v1() produced.
    Used by history revert to restore a previously removed old tag."""
    def enc(field, n):
        s = (v1.get(field) or [""])[0]
        return s.encode("cp1250", "replace")[:n].ljust(n, b"\x00")

    year = ((v1.get("year") or [""])[0][:4]).encode("ascii", "replace")
    block = b"TAG" + enc("title", 30) + enc("artist", 30) + enc("album", 30) \
        + year.ljust(4, b"\x00")
    track_s = (v1.get("track") or ["0"])[0]
    track = int(track_s) if track_s.isdigit() and 0 < int(track_s) < 256 else 0
    if track:
        block += enc("comment", 28) + b"\x00" + bytes([track])
    else:
        block += enc("comment", 30)
    genres = getattr(mutagen.id3.TCON, "GENRES", [])
    genre = (v1.get("genre") or [""])[0]
    idx = genres.index(genre) if genre in genres else 255
    return block + bytes([idx])


def read_id3v1(path):
    """Parse the old ID3v1 tag at the end of the file, if present.
    Returns {field: [value]} with only the non-empty fields, or None.
    Text is decoded as UTF-8 when it is valid UTF-8 (many taggers wrote UTF-8
    bytes into ID3v1), otherwise as cp1250 (right for old Czech rips)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        if f.tell() < 128:
            return None
        f.seek(-128, 2)
        data = f.read(128)
    if data[:3] != b"TAG" or data[:4] == b"TAG+":
        return None

    def txt(b):
        b = b.split(b"\x00")[0]
        try:
            return b.decode("utf-8").strip()
        except UnicodeDecodeError:
            return b.decode("cp1250", "replace").strip()

    out = {}
    for field, sl in (("title", slice(3, 33)), ("artist", slice(33, 63)),
                      ("album", slice(63, 93))):
        v = txt(data[sl])
        if v:
            out[field] = [v]
    year = txt(data[93:97])
    if year.isdigit():
        out["year"] = [year]
    comment_bytes = data[97:127]
    if data[125] == 0 and data[126] != 0:          # ID3v1.1 track number
        out["track"] = [str(data[126])]
        comment_bytes = data[97:125]
    comment = txt(comment_bytes)
    if comment:
        out["comment"] = [comment]
    genre_idx = data[127]
    genres = getattr(mutagen.id3.TCON, "GENRES", [])
    if genre_idx < len(genres):
        out["genre"] = [genres[genre_idx]]
    return out or None


def read_apev2(path):
    """Parse an APEv2 tag appended to the file, if present.
    Returns {field: [values]} with only the mapped, non-empty text fields
    (our field names, not the raw APEv2 keys), or None. Non-text values
    (binary, replay-gain, custom keys we don't map) are ignored - they carry
    no metadata we track, so they never block removal."""
    try:
        tag = APEv2(path)
    except APENoHeaderError:
        return None
    except Exception:
        return None
    out = {}
    for key in tag.keys():
        field = APEV2_TO_FIELD.get(key.strip().casefold())
        if not field or field in out:
            continue
        val = tag[key]
        if getattr(val, "kind", 0) != 0:        # 0 = text; skip binary/other
            continue
        vals = [str(s).strip() for s in list(val) if str(s).strip()]
        if vals:
            out[field] = vals
    return out or None


def build_apev2(path, ape):
    """(Re)write an APEv2 tag onto the file from the dict read_apev2() produced.
    Used by history revert to restore a previously removed APEv2 tag."""
    tag = APEv2()
    for field, key in FIELD_TO_APEV2.items():
        vals = [str(v) for v in (ape.get(field) or []) if str(v).strip()]
        if vals:
            tag[key] = vals
    if len(tag):
        tag.save(path)


def _main_comm(tags):
    """The primary comment frame (empty description), if any."""
    frames = tags.getall("COMM")
    for fr in frames:
        if fr.desc == "":
            return fr
    return frames[0] if frames else None


def read_tags(path):
    audio = MP3(path, load_v1=False)
    tags = audio.tags
    v1 = read_id3v1(path)
    t = {
        "_version": ("2.%d" % tags.version[1]) if tags else None,
        "_has_v1": v1 is not None,
        "_length": round(audio.info.length, 1),
        "_bitrate": audio.info.bitrate,
    }
    if v1:
        # full ID3v1 content is kept in every snapshot, so even after the v1
        # tag is stripped its data stays in the database history forever
        t["_v1"] = v1
    ape = read_apev2(path)
    t["_has_ape"] = ape is not None
    if ape:
        # like _v1: kept in every snapshot so a removed APEv2 tag stays in history
        t["_ape"] = ape
    for field, fid in FIELD_FRAMES.items():
        if tags is None:
            t[field] = []
        elif fid == "COMM":
            fr = _main_comm(tags)
            t[field] = [str(x) for x in fr.text if str(x).strip()] if fr else []
        else:
            fr = tags.get(fid)
            if fr is None:
                t[field] = []
            elif fid == "TCON":
                t[field] = [g for g in fr.genres if g.strip()]
            else:
                t[field] = [str(x) for x in fr.text if str(x).strip()]
    txxx = {}
    if tags:
        for fr in tags.getall("TXXX"):
            vals = [str(x) for x in fr.text if str(x).strip()]
            if vals:
                txxx[fr.desc] = vals
    if txxx:
        t["_txxx"] = txxx
    covers = []
    encs = Counter()
    if tags:
        for pic in tags.getall("APIC"):
            e = {"mime": pic.mime, "bytes": len(pic.data), "w": None, "h": None}
            try:
                with Image.open(BytesIO(pic.data)) as im:
                    e["w"], e["h"] = im.size
            except Exception:
                pass
            covers.append(e)
        for fr in tags.values():
            enc = getattr(fr, "encoding", None)
            if enc is not None:
                encs[ENC_NAMES.get(int(enc), "?")] += 1
    t["_cover"] = covers
    t["_enc"] = dict(encs)
    return t


def get_cover_data(path):
    """(mime, bytes) of the largest embedded picture, or None."""
    audio = MP3(path, load_v1=False)
    if not audio.tags:
        return None
    pics = audio.tags.getall("APIC")
    if not pics:
        return None
    best = max(pics, key=lambda p: len(p.data))
    return best.mime, best.data


def write_changes(path, changes, settings, keep_v1_bytes=None,
                  strip_ape=None, keep_ape_data=None, strip_v1=None):
    """Apply field changes to one file and save as ID3v2.4/UTF-8.

    changes: field -> list[str] for text fields, or ('cover', (mime, bytes)).
    Pseudo fields (_id3v1, _apev2, _version) force a rewrite without a field
    change.
    keep_v1_bytes: raw 128-byte ID3v1 block to preserve verbatim (used while
    an unresolved v1/v2 conflict exists, so the old tag is never lost).
    strip_ape: None = follow settings['strip_apev2']; True/False = force.
    keep_ape_data: {field: [values]} to (re)write an APEv2 tag (history revert);
    when given, the tag is restored instead of stripped.
    Returns list of (field, old_list, new_list) actually written.
    """
    st = os.stat(path) if settings.get("preserve_file_times", True) else None
    audio = MP3(path, load_v1=False)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    applied = []

    for field, value in changes.items():
        if field in PSEUDO_FIELDS:
            continue
        if field == "cover":
            mime, data = value
            old = ["%d picture(s)" % len(tags.getall("APIC"))]
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="", data=data))
            applied.append((field, old, ["new cover, %d KB" % (len(data) // 1024)]))
            continue
        fid = FIELD_FRAMES[field]
        value = [v for v in (value or []) if str(v).strip()]
        if fid == "COMM":
            fr = _main_comm(tags)
            old = [str(x) for x in fr.text] if fr else []
            others = [c for c in tags.getall("COMM") if c is not fr]
            new_frames = others + ([COMM(encoding=3, lang="XXX", desc="", text=value)]
                                   if value else [])
            tags.setall("COMM", new_frames)
        else:
            fr = tags.get(fid)
            if fid == "TCON" and fr is not None:
                old = [g for g in fr.genres if g.strip()]
            else:
                old = [str(x) for x in fr.text if str(x).strip()] if fr else []
            if value:
                cls = getattr(mutagen.id3, fid)
                tags.setall(fid, [cls(encoding=3, text=value)])
            else:
                tags.delall(fid)
        if old != value:
            applied.append((field, old, value))

    if settings.get("utf8_all_frames", True):
        for fr in tags.values():
            if hasattr(fr, "encoding"):
                fr.encoding = 3
    strip1 = settings.get("strip_id3v1", True) if strip_v1 is None else strip_v1
    v1 = 0 if strip1 else 1
    if keep_v1_bytes:
        v1 = 0                      # remove, then re-append the original block
    audio.save(v1=v1, v2_version=4)
    # APEv2 is a separate block at the end of the file that saving ID3 leaves
    # untouched, so it has to be handled explicitly. Do it before re-appending
    # any ID3v1 block so the on-disk order stays [audio][APEv2][ID3v1].
    strip = settings.get("strip_apev2", True) if strip_ape is None else strip_ape
    if keep_ape_data is not None:
        build_apev2(path, keep_ape_data)
    elif strip:
        try:
            mutagen.apev2.delete(path)
        except Exception:
            pass
    if keep_v1_bytes:
        with open(path, "ab") as f:
            f.write(keep_v1_bytes)
    if st is not None:
        # tag edits rewrite the file in place, so the creation date is untouched;
        # restoring mtime/atime keeps "date modified" as it was too
        os.utime(path, (st.st_atime, st.st_mtime))
    return applied

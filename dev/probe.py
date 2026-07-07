#!/usr/bin/env python3
"""
probe.py - Read-only probe of the MP3 library tag state.

Scans the folders listed in folders.txt under the MP3 root, reads all tags,
and produces:
  probe_report.md - human-readable summary of the state and problems found
  probe.db        - SQLite database with the raw per-file tag data

It NEVER writes to the MP3 files or the library folders.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

from mutagen.mp3 import MP3
from PIL import Image

# lives in dev\; the app's config.json is one level up, outputs stay in dev\
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR.parent / "config.json"
REPORT_PATH = BASE_DIR / "probe_report.md"
DB_PATH = BASE_DIR / "probe.db"

DEFAULT_CONFIG = {"folders_txt": "folders.txt", "mp3_root": ""}

OTHER_AUDIO_EXTS = {".flac", ".m4a", ".wma", ".ogg", ".wav", ".ape", ".wv", ".opus", ".aac"}
COVER_NAMES = {"folder.jpg", "folder.jpeg", "folder.png", "cover.jpg", "cover.jpeg",
               "cover.png", "front.jpg", "front.jpeg", "front.png", "album.jpg"}
ARTIST_IMG_NAMES = {"artist.jpg", "artist.jpeg", "artist.png"}

# Text frames we record (after mutagen's load, v2.3 year frames appear as TDRC)
TEXT_FRAMES = [
    "TIT2", "TPE1", "TPE2", "TALB", "TRCK", "TPOS", "TCON", "TDRC", "TDOR",
    "TCOM", "TEXT", "TOPE", "TPE3", "TPE4", "TIT1", "TIT3", "TPUB", "TCOP",
    "TLAN", "TBPM", "TSRC", "TSOP", "TSOA", "TSOT", "TSST", "TCMP",
]
CORE_FIELDS = ["TIT2", "TPE1", "TALB", "TPE2", "TRCK", "TDRC", "TCON"]
FIELD_LABELS = {
    "TIT2": "Title", "TPE1": "Artist", "TALB": "Album", "TPE2": "Album artist",
    "TRCK": "Track number", "TDRC": "Year/date", "TCON": "Genre",
}

ENC_NAMES = {0: "latin-1", 1: "utf-16", 2: "utf-16be", 3: "utf-8"}

CZECH_CHARS = set("áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ")
CP1250_AS_CP1252_MARKERS = set("ìèøùòÌÈØÙÒ")

FEAT_RE = re.compile(r"(?i)(?:\b|\()(feat\.?|ft\.?|featuring)\s")
SEPARATOR_PATTERNS = [
    ("; ", "semicolon  'A; B'"),
    ("/", "slash  'A/B'"),
    (" & ", "ampersand  'A & B'"),
    (", ", "comma  'A, B'"),
    (" x ", "'A x B'"),
    (" vs", "'A vs B'"),
]


# ----------------------------------------------------------------- config ---

def _config_values_literal(raw):
    """Pull the quoted values out of config.json text without JSON unescaping."""
    cfg = dict(DEFAULT_CONFIG)
    found = False
    for key in cfg:
        m = re.search(r'"%s"\s*:\s*"([^"]*)"' % key, raw)
        if m:
            cfg[key] = m.group(1)
            found = True
    return cfg if found else None


def load_config():
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    raw = CONFIG_PATH.read_text(encoding="utf-8-sig")
    try:
        parsed = json.loads(raw)
        cfg = {**DEFAULT_CONFIG,
               **{k: v for k, v in parsed.items() if k in DEFAULT_CONFIG}}
        # multi-library config (the GUI app): use the active library's root
        if not cfg.get("mp3_root") and parsed.get("libraries"):
            libs = parsed["libraries"]
            active = next((l for l in libs
                           if l.get("name") == parsed.get("active_library")),
                          libs[0])
            cfg["mp3_root"] = active.get("root", "")
            cfg["folders_txt"] = active.get("folders_txt", "folders.txt")
        # A pasted Windows path can also be *valid* JSON with the wrong meaning:
        # in "\\nas\tracks" the \t silently turns into a TAB. If any value came
        # out with control characters, re-read the values literally instead.
        if not any(c in str(v) for v in cfg.values() for c in "\t\n\r\b\f"):
            return cfg
    except json.JSONDecodeError:
        # Plain backslash paths ("C:\Users\...") are simply invalid JSON.
        pass
    cfg = _config_values_literal(raw)
    if cfg is None:
        print("config.json could not be read - fix it or delete it and run again.")
        sys.exit(1)
    save_config(cfg)  # rewrite as valid JSON so the next load is clean
    print("Note: config.json contained unescaped backslashes; rewritten as valid JSON.")
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def read_folders_txt(path: Path):
    """One folder name per line, trailing slash/backslash tolerated (Total Commander style)."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1250", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    entries = []
    seen = set()
    for line in text.splitlines():
        name = line.strip().rstrip("\\/").strip()
        if name and name not in seen:
            seen.add(name)
            entries.append(name)
    return entries


# ------------------------------------------------------- mojibake heuristics ---

def detect_mojibake(s: str):
    """Return a proposed repaired string if s looks like wrongly decoded Czech text."""
    # Case 1: UTF-8 bytes decoded as cp1252/latin-1 ("Å¡" instead of "š")
    for enc in ("cp1252", "latin-1"):
        try:
            b = s.encode(enc)
        except UnicodeEncodeError:
            continue
        try:
            fixed = b.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if fixed != s and any(ch in CZECH_CHARS for ch in fixed):
            return fixed
    # Case 2: cp1250 bytes decoded as cp1252 ("ø" instead of "ř")
    if any(ch in CP1250_AS_CP1252_MARKERS for ch in s):
        try:
            fixed = s.encode("cp1252").decode("cp1250")
            if fixed != s and any(ch in CZECH_CHARS for ch in fixed):
                return fixed
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return None


# -------------------------------------------------------------- file probe ---

def probe_file(path: str):
    """Read everything interesting from one MP3. Returns a result dict."""
    r = {"path": path, "error": None}
    try:
        st = os.stat(path)
        r["size"] = st.st_size
        r["mtime"] = st.st_mtime

        with open(path, "rb") as f:
            f.seek(0, 2)
            if f.tell() >= 128:
                f.seek(-128, 2)
                r["has_v1"] = f.read(3) == b"TAG"
            else:
                r["has_v1"] = False

        # load_v1=False: report what is really in the ID3v2 tag; the old v1
        # leftover is detected separately above
        audio = MP3(path, load_v1=False)
        r["length"] = round(audio.info.length, 1)
        r["bitrate"] = audio.info.bitrate

        tags = audio.tags
        if tags is None:
            r["id3_version"] = None
            r["frames"] = {}
            r["apic"] = []
            r["txxx"] = []
            r["extra"] = {}
            return r

        r["id3_version"] = "2.%d" % tags.version[1]
        frames = {}
        for fid in TEXT_FRAMES:
            fr = tags.get(fid)
            if fr is None:
                continue
            if fid == "TCON":
                values = list(fr.genres)
            else:
                values = [str(t) for t in fr.text]
            values = [v for v in values if v.strip() != ""]
            if not values:
                continue
            frames[fid] = {
                "values": values,
                "enc": ENC_NAMES.get(getattr(fr, "encoding", -1), "?"),
            }
        r["frames"] = frames

        apics = []
        for pic in tags.getall("APIC"):
            entry = {"mime": pic.mime, "type": int(pic.type), "bytes": len(pic.data),
                     "w": None, "h": None}
            try:
                with Image.open(BytesIO(pic.data)) as im:
                    entry["w"], entry["h"] = im.size
            except Exception:
                pass
            apics.append(entry)
        r["apic"] = apics

        r["txxx"] = sorted({fr.desc for fr in tags.getall("TXXX")})
        r["extra"] = {
            "comments": len(tags.getall("COMM")),
            "lyrics": len(tags.getall("USLT")),
            "popm": len(tags.getall("POPM")),
        }
    except Exception as e:
        r["error"] = "%s: %s" % (type(e).__name__, e)
    return r


# ------------------------------------------------------------- collection ---

def collect_files(root: Path, entries):
    """Walk the listed artist folders. Returns file list + folder-level facts."""
    mp3s = []          # (path, artist_folder, album_dir)
    missing = []
    other_audio = Counter()
    dir_images = {}    # album_dir -> has cover image file
    artist_info = {}   # entry -> {"artist_img": bool, "depths": Counter, "n_mp3": int}

    for entry in entries:
        folder = root / entry
        if not folder.is_dir():
            missing.append(entry)
            continue
        info = {"artist_img": False, "depths": Counter(), "n_mp3": 0}
        artist_info[entry] = info
        for dirpath, _dirnames, filenames in os.walk(folder):
            names_lower = {n.lower() for n in filenames}
            if dirpath == str(folder) and names_lower & ARTIST_IMG_NAMES:
                info["artist_img"] = True
            has_mp3_here = False
            for name in filenames:
                ext = os.path.splitext(name)[1].lower()
                if ext == ".mp3":
                    p = os.path.join(dirpath, name)
                    mp3s.append((p, entry, dirpath))
                    has_mp3_here = True
                elif ext in OTHER_AUDIO_EXTS:
                    other_audio[ext] += 1
            if has_mp3_here:
                dir_images[dirpath] = bool(names_lower & COVER_NAMES)
                depth = len(Path(dirpath).relative_to(folder).parts)
                info["depths"][depth] += 1
                info["n_mp3"] += sum(
                    1 for n in filenames if n.lower().endswith(".mp3"))
    return mp3s, missing, other_audio, dir_images, artist_info


# ------------------------------------------------------------------ sqlite ---

def init_db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE files (
            path TEXT PRIMARY KEY,
            artist_folder TEXT,
            album_dir TEXT,
            size INTEGER, mtime REAL,
            length REAL, bitrate INTEGER,
            id3_version TEXT, has_v1 INTEGER,
            frames TEXT, apic TEXT, txxx TEXT, extra TEXT,
            error TEXT
        )""")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    return con


# ------------------------------------------------------------------ report ---

def fmt_counter(counter, total=None, limit=None):
    lines = []
    for key, n in counter.most_common(limit):
        pct = " (%.1f%%)" % (100.0 * n / total) if total else ""
        lines.append("| %s | %d%s |" % (key, n, pct))
    return lines


def pct(n, total):
    return "%.1f%%" % (100.0 * n / total) if total else "n/a"


def build_report(ctx):
    L = []
    a = L.append
    total = ctx["total"]

    a("# MP3 library probe report")
    a("")
    a("- Generated: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    a("- MP3 root: `%s`" % ctx["root"])
    a("- Artist folders requested: %d, found: %d, **missing: %d**"
      % (ctx["n_entries"], ctx["n_entries"] - len(ctx["missing"]), len(ctx["missing"])))
    a("- MP3 files scanned: **%d** (%.1f GB), read errors: %d"
      % (total, ctx["total_bytes"] / 1e9, len(ctx["errors"])))
    a("- Other audio files present (not scanned): %s"
      % (", ".join("%s: %d" % kv for kv in ctx["other_audio"].most_common()) or "none"))
    a("- Scan time: %.1f s (%.1f files/s) — this measures Samba speed for planning"
      % (ctx["elapsed"], total / ctx["elapsed"] if ctx["elapsed"] else 0))
    a("")

    if ctx["missing"]:
        a("## Folders from folders.txt not found under the root")
        a("")
        for m in ctx["missing"][:50]:
            a("- `%s`" % m)
        if len(ctx["missing"]) > 50:
            a("- ... and %d more" % (len(ctx["missing"]) - 50))
        a("")

    a("## Folder structure")
    a("")
    a("| MP3 location relative to artist folder | album dirs |")
    a("|---|---|")
    depth_total = Counter()
    for info in ctx["artist_info"].values():
        depth_total.update(info["depths"])
    for depth, n in sorted(depth_total.items()):
        label = {0: "directly in artist folder", 1: "one level deep (Artist\\Album)"}.get(
            depth, "%d levels deep" % depth)
        a("| %s | %d |" % (label, n))
    n_artist_img = sum(1 for i in ctx["artist_info"].values() if i["artist_img"])
    a("")
    a("- Artist folders with artist.jpg: %d / %d (%s)"
      % (n_artist_img, len(ctx["artist_info"]), pct(n_artist_img, len(ctx["artist_info"]))))
    n_album_dirs = len(ctx["dir_images"])
    n_cover = sum(1 for v in ctx["dir_images"].values() if v)
    a("- Album folders with folder.jpg/cover.jpg: %d / %d (%s)"
      % (n_cover, n_album_dirs, pct(n_cover, n_album_dirs)))
    a("")

    a("## ID3 versions")
    a("")
    a("| Version | files |")
    a("|---|---|")
    L.extend(fmt_counter(ctx["ver_counter"], total))
    a("")
    a("- Files with an extra old ID3v1 tag at the end: %d (%s)"
      % (ctx["n_v1"], pct(ctx["n_v1"], total)))
    a("- Files with ID3v1 ONLY (no modern tag at all): %d" % ctx["n_v1_only"])
    a("- Files with no tags whatsoever: %d" % ctx["n_untagged"])
    a("")

    a("## Text encodings used inside frames")
    a("")
    a("| Encoding | frames |")
    a("|---|---|")
    L.extend(fmt_counter(ctx["enc_counter"]))
    a("")

    a("## Missing core fields")
    a("")
    a("| Field | files missing it | share |")
    a("|---|---|---|")
    for fid in CORE_FIELDS:
        n_missing = total - ctx["field_present"][fid]
        a("| %s (%s) | %d | %s |" % (FIELD_LABELS[fid], fid, n_missing, pct(n_missing, total)))
    n_noart = total - ctx["n_has_apic"]
    a("| Embedded cover art (APIC) | %d | %s |" % (n_noart, pct(n_noart, total)))
    a("")

    a("## Artist field formats (TPE1)")
    a("")
    a("- Files whose artist frame holds several proper multi-values: %d" % ctx["n_multivalue"])
    a("")
    a("| Separator style found in artist text | files | example |")
    a("|---|---|---|")
    for key, label in SEPARATOR_PATTERNS:
        n = ctx["sep_counter"][key]
        if n:
            ex = ctx["sep_examples"].get(key, "")
            a("| %s | %d | %s |" % (label, n, ex.replace("|", "¦")))
    a("")
    a("- 'feat./ft.' found in artist field: %d (example: %s)"
      % (ctx["n_feat_artist"], (ctx["feat_artist_example"] or "-").replace("|", "¦")))
    a("- 'feat./ft.' found in title field: %d (example: %s)"
      % (ctx["n_feat_title"], (ctx["feat_title_example"] or "-").replace("|", "¦")))
    a("")

    a("## Album artist (TPE2)")
    a("")
    a("| Value pattern | files |")
    a("|---|---|")
    L.extend(fmt_counter(ctx["tpe2_kinds"], total))
    a("")

    a("## Track numbers")
    a("")
    a("| Format | files |")
    a("|---|---|")
    L.extend(fmt_counter(ctx["trck_formats"], total))
    a("")

    a("## Year / date formats (TDRC after normalization)")
    a("")
    a("| Format | files |")
    a("|---|---|")
    L.extend(fmt_counter(ctx["year_formats"], total))
    if ctx["weird_years"]:
        a("")
        a("Suspicious year values: %s" % ", ".join(
            "`%s`" % v for v in ctx["weird_years"][:15]))
    a("")

    a("## Genres (top 30)")
    a("")
    a("| Genre | files |")
    a("|---|---|")
    L.extend(fmt_counter(ctx["genre_counter"], total, limit=30))
    a("")

    a("## Embedded cover art quality")
    a("")
    a("| Resolution | pictures |")
    a("|---|---|")
    L.extend(fmt_counter(ctx["apic_res"]))
    a("")
    a("- Files with more than one embedded picture: %d" % ctx["n_multi_apic"])
    a("")

    a("## Suspected broken Czech encoding (mojibake)")
    a("")
    if ctx["mojibake"]:
        a("%d frame values look damaged. Examples (current → proposed repair):" % len(ctx["mojibake"]))
        a("")
        for frame, orig, fixed, path in ctx["mojibake"][:20]:
            a("- `%s`: \"%s\" → \"%s\"  \n  `%s`" % (frame, orig, fixed, path))
        if len(ctx["mojibake"]) > 20:
            a("- ... and %d more" % (len(ctx["mojibake"]) - 20))
    else:
        a("None detected.")
    a("")

    a("## Consistency inside albums")
    a("")
    a("- Album folders where the ALBUM name differs between tracks: %d" % ctx["incons"]["TALB"])
    a("- Album folders where the ALBUM ARTIST differs between tracks: %d" % ctx["incons"]["TPE2"])
    a("- Album folders where the YEAR differs between tracks: %d" % ctx["incons"]["TDRC"])
    a("- Album folders where the GENRE differs between tracks: %d" % ctx["incons"]["TCON"])
    a("- Album folders with gaps in track numbering: %d" % ctx["n_track_gaps"])
    a("- Album folders that look like compilations (3+ different track artists): %d"
      % ctx["n_compilation_like"])
    a("")

    a("## Extra frames present")
    a("")
    a("| TXXX custom tag | files |")
    a("|---|---|")
    L.extend(fmt_counter(ctx["txxx_counter"], limit=25))
    a("")
    a("- Files with comment frames (COMM): %d" % ctx["n_comm"])
    a("- Files with embedded lyrics (USLT): %d" % ctx["n_uslt"])
    a("- Files with ratings (POPM): %d" % ctx["n_popm"])
    a("")

    a("## Bitrates")
    a("")
    a("| Bitrate | files |")
    a("|---|---|")
    L.extend(fmt_counter(ctx["bitrate_counter"], total))
    a("")

    if ctx["errors"]:
        a("## Files that could not be read")
        a("")
        for path, err in ctx["errors"][:30]:
            a("- `%s` — %s" % (path, err))
        if len(ctx["errors"]) > 30:
            a("- ... and %d more" % (len(ctx["errors"]) - 30))
        a("")

    return "\n".join(L)


# -------------------------------------------------------------- aggregation ---

def aggregate(results, dir_images, artist_info):
    ctx = defaultdict(Counter)
    ctx = {}
    ver_counter = Counter()
    enc_counter = Counter()
    field_present = Counter()
    sep_counter = Counter()
    sep_examples = {}
    genre_counter = Counter()
    trck_formats = Counter()
    year_formats = Counter()
    tpe2_kinds = Counter()
    apic_res = Counter()
    txxx_counter = Counter()
    bitrate_counter = Counter()
    weird_years = []
    mojibake = []
    errors = []
    albums = defaultdict(lambda: defaultdict(set))
    album_tracks = defaultdict(list)

    n_v1 = n_v1_only = n_untagged = n_has_apic = n_multi_apic = 0
    n_multivalue = n_feat_artist = n_feat_title = n_comm = n_uslt = n_popm = 0
    feat_artist_example = feat_title_example = None
    total_bytes = 0

    for r in results:
        if r["error"]:
            errors.append((r["path"], r["error"]))
            continue
        total_bytes += r["size"]
        frames = r["frames"]

        if r["has_v1"]:
            n_v1 += 1
            if r["id3_version"] is None:
                n_v1_only += 1
        if r["id3_version"] is None and not r["has_v1"]:
            n_untagged += 1
        ver_counter[r["id3_version"] or "no ID3v2"] += 1

        for fid, fr in frames.items():
            enc_counter[fr["enc"]] += 1
            for v in fr["values"]:
                fixed = detect_mojibake(v)
                if fixed:
                    mojibake.append((fid, v, fixed, r["path"]))

        for fid in CORE_FIELDS:
            if fid in frames:
                field_present[fid] += 1

        if r["apic"]:
            n_has_apic += 1
            if len(r["apic"]) > 1:
                n_multi_apic += 1
            for pic in r["apic"]:
                if pic["w"] is None:
                    apic_res["unreadable image"] += 1
                else:
                    px = min(pic["w"], pic["h"])
                    if px < 300:
                        apic_res["tiny (< 300 px)"] += 1
                    elif px < 500:
                        apic_res["small (300-499 px)"] += 1
                    elif px < 1000:
                        apic_res["good (500-999 px)"] += 1
                    else:
                        apic_res["large (1000+ px)"] += 1

        tpe1 = frames.get("TPE1")
        if tpe1:
            if len(tpe1["values"]) > 1:
                n_multivalue += 1
            joined = " | ".join(tpe1["values"])
            for key, _label in SEPARATOR_PATTERNS:
                if key in joined:
                    sep_counter[key] += 1
                    sep_examples.setdefault(key, joined)
            if FEAT_RE.search(joined):
                n_feat_artist += 1
                feat_artist_example = feat_artist_example or joined
        tit2 = frames.get("TIT2")
        if tit2 and FEAT_RE.search(" ".join(tit2["values"])):
            n_feat_title += 1
            feat_title_example = feat_title_example or " ".join(tit2["values"])

        tpe2 = frames.get("TPE2")
        if not tpe2:
            tpe2_kinds["missing"] += 1
        else:
            v = " | ".join(tpe2["values"])
            if v.strip().lower() in ("various artists", "various", "va", "různí interpreti"):
                tpe2_kinds["Various Artists variant"] += 1
            elif tpe1 and v == " | ".join(tpe1["values"]):
                tpe2_kinds["same as track artist"] += 1
            else:
                tpe2_kinds["different from track artist"] += 1

        trck = frames.get("TRCK")
        if trck:
            v = trck["values"][0]
            has_total = "/" in v
            num = v.split("/")[0]
            padded = len(num) > 1 and num.startswith("0")
            key = ("'%s'" % ("03/12" if has_total and padded
                             else "3/12" if has_total
                             else "03" if padded else "3"))
            if not num.strip().isdigit():
                key = "non-numeric"
            trck_formats[key] += 1

        tdrc = frames.get("TDRC")
        if tdrc:
            v = tdrc["values"][0]
            if re.fullmatch(r"\d{4}", v):
                year_formats["year only (1999)"] += 1
                if not (1900 <= int(v) <= 2030):
                    weird_years.append(v)
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                year_formats["full date (1999-05-21)"] += 1
            elif re.fullmatch(r"\d{4}-\d{2}", v):
                year_formats["year-month"] += 1
            else:
                year_formats["other"] += 1
                weird_years.append(v)

        tcon = frames.get("TCON")
        if tcon:
            for g in tcon["values"]:
                genre_counter[g] += 1

        for desc in r["txxx"]:
            txxx_counter[desc] += 1
        n_comm += 1 if r["extra"].get("comments") else 0
        n_uslt += 1 if r["extra"].get("lyrics") else 0
        n_popm += 1 if r["extra"].get("popm") else 0

        kbps = round((r["bitrate"] or 0) / 1000)
        if kbps >= 315:
            bitrate_counter["320 kbps"] += 1
        elif kbps in (128, 160, 192, 224, 256):
            bitrate_counter["%d kbps" % kbps] += 1
        else:
            bitrate_counter["VBR/other (~%d)" % (round(kbps, -1))] += 1

        # album-level collection
        adir = r["album_dir"]
        for fid in ("TALB", "TPE2", "TDRC", "TCON"):
            if fid in frames:
                albums[adir][fid].add(" | ".join(frames[fid]["values"]))
        if tpe1:
            albums[adir]["TPE1"].add(" | ".join(tpe1["values"]))
        if trck:
            num = trck["values"][0].split("/")[0]
            if num.strip().isdigit():
                album_tracks[adir].append(int(num))

    incons = {fid: sum(1 for a in albums.values() if len(a[fid]) > 1)
              for fid in ("TALB", "TPE2", "TDRC", "TCON")}
    n_compilation_like = sum(1 for a in albums.values() if len(a["TPE1"]) >= 3)
    n_track_gaps = 0
    for adir, nums in album_tracks.items():
        s = sorted(set(nums))
        if s and s != list(range(s[0], s[0] + len(s))):
            n_track_gaps += 1

    ctx.update(
        ver_counter=ver_counter, enc_counter=enc_counter, field_present=field_present,
        sep_counter=sep_counter, sep_examples=sep_examples, genre_counter=genre_counter,
        trck_formats=trck_formats, year_formats=year_formats, tpe2_kinds=tpe2_kinds,
        apic_res=apic_res, txxx_counter=txxx_counter, bitrate_counter=bitrate_counter,
        weird_years=weird_years, mojibake=mojibake, errors=errors,
        n_v1=n_v1, n_v1_only=n_v1_only, n_untagged=n_untagged,
        n_has_apic=n_has_apic, n_multi_apic=n_multi_apic, n_multivalue=n_multivalue,
        n_feat_artist=n_feat_artist, n_feat_title=n_feat_title,
        feat_artist_example=feat_artist_example, feat_title_example=feat_title_example,
        n_comm=n_comm, n_uslt=n_uslt, n_popm=n_popm,
        incons=incons, n_compilation_like=n_compilation_like, n_track_gaps=n_track_gaps,
        total_bytes=total_bytes, dir_images=dir_images, artist_info=artist_info,
    )
    return ctx


# -------------------------------------------------------------------- main ---

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Read-only MP3 library probe")
    parser.add_argument("--root", help="MP3 root folder (UNC path or mapped drive); saved to config.json")
    parser.add_argument("--folders", help="path to folders.txt (default from config.json)")
    parser.add_argument("--workers", type=int, default=8, help="parallel readers (default 8)")
    parser.add_argument("--limit", type=int, help="scan at most N files (for testing)")
    args = parser.parse_args()

    cfg = load_config()
    # --root/--folders are session-only; the GUI app owns config.json now
    if args.root:
        cfg["mp3_root"] = args.root
    if args.folders:
        cfg["folders_txt"] = args.folders

    if not cfg["mp3_root"]:
        print("No MP3 root configured. Run:  run.bat --root \"\\\\NAS\\share\\path\"")
        print("(or edit mp3_root in config.json)")
        return 1
    root = Path(cfg["mp3_root"])
    if not root.is_dir():
        print("MP3 root not reachable: %s" % root)
        return 1

    folders_path = Path(cfg["folders_txt"])
    if not folders_path.is_absolute():
        folders_path = BASE_DIR / folders_path
    if not folders_path.exists():
        print("folders.txt not found: %s" % folders_path)
        return 1
    entries = read_folders_txt(folders_path)
    if not entries:
        print("%s is empty - paste folder names into it first (one per line)." % folders_path.name)
        return 1

    print("Root: %s" % root)
    print("Folders listed: %d" % len(entries))
    print("Collecting file list...")
    t0 = time.time()
    mp3s, missing, other_audio, dir_images, artist_info = collect_files(root, entries)
    if args.limit:
        mp3s = mp3s[: args.limit]
    print("Found %d MP3 files in %.1f s (missing folders: %d)"
          % (len(mp3s), time.time() - t0, len(missing)))
    if not mp3s:
        print("Nothing to scan.")
        return 1

    album_dir_of = {p: adir for p, _art, adir in mp3s}
    artist_of = {p: art for p, art, _adir in mp3s}

    results = []
    t1 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(probe_file, p): p for p, _a, _d in mp3s}
        for fut in as_completed(futures):
            r = fut.result()
            r["album_dir"] = album_dir_of[r["path"]]
            r["artist_folder"] = artist_of[r["path"]]
            results.append(r)
            done += 1
            if done % 500 == 0 or done == len(mp3s):
                rate = done / (time.time() - t1)
                print("  %d / %d files (%.0f files/s)" % (done, len(mp3s), rate))
    elapsed = time.time() - t1

    print("Writing probe.db ...")
    con = init_db()
    with con:
        con.executemany(
            "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["path"], r["artist_folder"], r["album_dir"],
              r.get("size"), r.get("mtime"), r.get("length"), r.get("bitrate"),
              r.get("id3_version"), int(r.get("has_v1") or 0),
              json.dumps(r.get("frames", {}), ensure_ascii=False),
              json.dumps(r.get("apic", []), ensure_ascii=False),
              json.dumps(r.get("txxx", []), ensure_ascii=False),
              json.dumps(r.get("extra", {}), ensure_ascii=False),
              r["error"]) for r in results])
        con.execute("INSERT INTO meta VALUES ('root', ?)", (str(root),))
        con.execute("INSERT INTO meta VALUES ('scanned_at', ?)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
    con.close()

    print("Building report ...")
    ctx = aggregate(results, dir_images, artist_info)
    ctx.update(root=str(root), n_entries=len(entries), missing=missing,
               other_audio=other_audio, total=len(results) - len(ctx["errors"]),
               elapsed=elapsed)
    REPORT_PATH.write_text(build_report(ctx), encoding="utf-8")

    total = ctx["total"]
    print()
    print("=== PROBE SUMMARY ===")
    print("Files scanned: %d, errors: %d" % (total, len(ctx["errors"])))
    print("ID3 versions: %s" % dict(ctx["ver_counter"]))
    print("Old ID3v1 leftovers: %d, v1-only: %d, untagged: %d"
          % (ctx["n_v1"], ctx["n_v1_only"], ctx["n_untagged"]))
    for fid in CORE_FIELDS:
        n_miss = total - ctx["field_present"][fid]
        if n_miss:
            print("Missing %s: %d" % (FIELD_LABELS[fid], n_miss))
    print("Missing embedded cover: %d" % (total - ctx["n_has_apic"]))
    print("Suspected mojibake values: %d" % len(ctx["mojibake"]))
    print()
    print("Full report: %s" % REPORT_PATH)
    print("Raw data:    %s" % DB_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())

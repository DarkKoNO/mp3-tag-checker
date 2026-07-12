"""Rule engine: reads the latest tag snapshots and produces issues + fix proposals.

Runs entirely from the database - no file access - so it can be re-run
instantly after a settings change ("Re-evaluate").
"""

import json
import re
from collections import Counter, defaultdict

from . import db

CZECH_CHARS = set("áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ")
# Invisible C1 control characters are the ONLY trigger for the cp1250-as-
# cp1252 repair: they never occur in real text, so they prove damage.
# Printable letters are NEVER evidence: è à ì ò ù are normal French/Italian
# and ø å æ ð þ are normal Nordic text - repairing them broke valid tags.
C1_RE = re.compile("[\u0080-\u009f]")
IMAGE_RULES = {"cover_missing", "cover_tiny", "folder_jpg", "artist_jpg", "cover"}

TEXTUAL_FIELDS = ["title", "artist", "albumartist", "album", "genre", "comment",
                  "composer", "conductor", "remixer", "lyricist", "origartist",
                  "grouping", "subtitle", "discsubtitle", "publisher", "copyright"]
RED, YEL = "red", "yellow"
CORE_RED = {"title", "artist", "album", "track"}

RULE_LABELS = {
    "id3v1": "Remove old ID3v1 tag",
    "id3v1_conflict": "Old ID3v1 tag disagrees with ID3v2",
    "v1_rescue": "Rescue data from old ID3v1 tag",
    "apev2": "Remove foreign APEv2 tag",
    "apev2_conflict": "APEv2 tag disagrees with ID3v2",
    "apev2_rescue": "Rescue data from APEv2 tag",
    "id3_version": "Upgrade to ID3v2.4",
    "encoding": "Re-encode text as UTF-8",
    "artist_superset": "Artist should include the album artist",
    "plus_collab": "'+' in folder name — artist should hold multiple values",
    "track_format": "Track number format",
    "albumartist": "Unify album artist",
    "album_inconsistent": "Album name differs inside album",
    "mojibake": "Repair broken text (mojibake)",
    "multi_split": "Split combined values into multiple values",
    "value_format": "Value doesn't match the required format",
    "single_value": "Several values in a single-value field",
    "publisher_from_comment": "Fill Publisher from the Label comment",
    "artist_sync": "Copy between artist and album artist",
    "online_meta": "Internet metadata (MusicBrainz)",
    "folder_jpg": "Write folder.jpg",
    "cover": "Replace cover art (online)",
    "manual": "Manual edits",
    "cover_missing": "No embedded cover art",
    "cover_tiny": "Embedded cover too small",
    "track_gaps": "Gaps in track numbering",
    "year_inconsistent": "Year differs inside album",
    "artist_jpg": "Missing artist.jpg",
}
RULE_SEVERITY = {
    "mojibake": RED, "album_inconsistent": RED, "cover_missing": RED,
    "track_gaps": RED, "id3v1_conflict": RED, "apev2_conflict": RED,
    "value_format": RED,
    "missing_title": RED, "missing_artist": RED, "missing_album": RED,
    "missing_track": RED,
}
# issue rules with no automatic fix -> shown under "needs attention"
NON_FIXABLE = {"cover_missing", "cover_tiny", "track_gaps", "year_inconsistent",
               "artist_jpg", "id3v1_conflict", "apev2_conflict", "plus_collab"}

# Fixed workflow order of the problem types in the change-type tree: things
# that should be resolved first (later rules depend on clean text/structure)
# come first. Used as a STABLE sort key so the tree order never shuffles
# when counts change after applying.
RULE_PRIORITY = [
    "v1_rescue", "id3v1_conflict", "id3v1",
    "apev2_rescue", "apev2_conflict", "apev2",
    "id3_version", "encoding", "mojibake",
    "missing",                      # missing_<field> rules share this slot
    "value_format", "track_format",
    "multi_split", "single_value",
    "plus_collab", "artist_superset", "albumartist", "artist_sync",
    "publisher_from_comment",
    "album_inconsistent", "year_inconsistent", "track_gaps",
    "online_meta", "cover", "manual",
    "cover_missing", "cover_tiny", "folder_jpg", "artist_jpg",
]


def rule_priority(rule):
    """Stable sort rank of a change/problem type (lower = shown earlier).
    Unknown types sort last, alphabetically via the caller's tie-break."""
    r = rule or ""
    if r.startswith("missing_"):
        r = "missing"
    try:
        return RULE_PRIORITY.index(r)
    except ValueError:
        return len(RULE_PRIORITY)

# Detailed, user-facing explanation of every change/problem type. Shown as a
# tooltip wherever the type's name appears (tree, detail panel, settings).
RULE_DESCRIPTIONS = {
    "id3v1":
        "MP3 files can carry two independent tags: the modern ID3v2 tag at the"
        " start of the file (what every current player reads) and the legacy"
        " ID3v1 tag - a fixed 128-byte block at the very end, limited to 30"
        " characters per field and one genre from a fixed list.\n\n"
        "This app treats ID3v2 as the authoritative tag and proposes deleting"
        " the ID3v1 leftover. The removal is only offered after checking that"
        " every piece of information in the old tag is also present in ID3v2,"
        " so nothing is lost. Applying rewrites the file without the trailing"
        " 128-byte block; the visible tags do not change.",
    "id3v1_conflict":
        "The file has both tags, and the legacy ID3v1 tag (at the end of the"
        " file) holds a DIFFERENT value than the modern ID3v2 tag. ID3v2 is"
        " the CURRENT value - it is what players show and what the 'Current'"
        " column displays; the ID3v1 value is the old/other one.\n\n"
        "The old tag is never removed while such a difference is unresolved."
        " To resolve it, either right-click the row and choose 'Keep ID3v2"
        " value' (the current value stays, the old tag becomes removable), or"
        " apply the offered proposal to copy the old ID3v1 value into ID3v2."
        " When you apply the ID3v1 value, the old tag is removed in the same"
        " write (if 'Remove old ID3v1 tags' is enabled), so one apply does"
        " both steps.",
    "v1_rescue":
        "The legacy ID3v1 tag at the end of the file contains a value (e.g."
        " a year or a comment) that is completely MISSING in the modern ID3v2"
        " tag. The proposal copies that value into ID3v2 so it is not lost."
        " The old ID3v1 tag is then removed together with the write (if"
        " 'Remove old ID3v1 tags' is enabled).",
    "apev2":
        "An APEv2 tag is a separate block of metadata that some old rippers,"
        " taggers and tools like MP3Gain append to MP3 files. Modern players"
        " read ID3v2, not this block, so it is a hidden second copy of the"
        " metadata that can quietly disagree with what you see.\n\n"
        "This app treats ID3v2 as the authoritative tag and proposes deleting"
        " the APEv2 leftover - handled exactly like an old ID3v1 tag. Removal"
        " is only offered after checking that every metadata value in the APEv2"
        " block is also present in ID3v2, so nothing you track is lost. (Note:"
        " non-metadata APEv2 entries such as MP3Gain replay-gain values are not"
        " tracked and are removed with the block.)",
    "apev2_conflict":
        "The file has an APEv2 tag whose value for a field DIFFERS from the"
        " modern ID3v2 tag. ID3v2 is the CURRENT value - what players show and"
        " what the 'Current' column displays; the APEv2 value is the other"
        " one.\n\n"
        "The APEv2 tag is never removed while such a difference is unresolved."
        " To resolve it, either right-click the row and choose 'Keep ID3v2"
        " value' (the current value stays, the APEv2 tag becomes removable),"
        " or apply the offered proposal to copy the APEv2 value into ID3v2."
        " When you apply the APEv2 value, the tag is removed in the same write"
        " (if 'Remove foreign APEv2 tags' is enabled), so one apply does both.",
    "apev2_rescue":
        "The APEv2 tag contains a value (e.g. a year or a comment) that is"
        " completely MISSING in the modern ID3v2 tag. The proposal copies that"
        " value into ID3v2 so it is not lost. The APEv2 tag is then removed"
        " together with the write (if 'Remove foreign APEv2 tags' is enabled).",
    "id3_version":
        "The file uses an older ID3v2 sub-version (2.2 or 2.3). This app"
        " writes tags in the current standard, ID3v2.4, which supports UTF-8"
        " text and proper multi-value fields. Applying rewrites the tag in"
        " the 2.4 format - no textual values are changed by this.",
    "encoding":
        "Some text frames in the file are stored in a legacy encoding"
        " (latin-1, utf-16, ...). The ID3v2.4 standard is UTF-8, which every"
        " modern player reads and which can represent all characters of all"
        " languages. Applying re-saves the same values encoded as UTF-8 - the"
        " visible text does not change.",
    "artist_superset":
        "With the 'subset' album-artist rule (see Settings - Writing), every"
        " track's ARTIST must contain the album artist: the artist field may"
        " hold more names (e.g. guests) but never fewer. This track's artist"
        " is missing the album artist; the proposal adds it to the artist"
        " list, keeping the existing names.",
    "plus_collab":
        "The album folder name contains '+', which in this library convention"
        " marks a collaboration of several artists (e.g. '+ Mothers Of"
        " Invention - Uncle Meat' inside the Frank Zappa folder). The track's"
        " ARTIST field holds only one value, so several values are probably"
        " expected.\n\nThis is only a warning - names are never guessed or"
        " looked up. When the single value itself contains a separator, a"
        " split into several values is offered. Only the artist field is"
        " checked (the album artist may legitimately name just the main"
        " artist).",
    "track_format":
        "The track number is not written in the format chosen in Settings"
        " (zero-padding like '03' and/or the album total like '3/12'). The"
        " proposal rewrites the number in the configured format; the total is"
        " taken from the highest track number found in the album. Only the"
        " formatting changes - the number itself stays.",
    "albumartist":
        "The album artist should be identical on every track of an album -"
        " players use it to group the album together; differing values split"
        " the album apart. The proposal unifies it: with the 'subset' rule to"
        " the value used by most tracks of the album, with the 'common' rule"
        " to the artists shared by all tracks (or the compilation name from"
        " Settings when there is none).",
    "album_inconsistent":
        "Tracks inside one album folder carry different ALBUM names (often a"
        " typo or a different spelling on a few tracks), which makes players"
        " show two half-albums. The proposal renames the minority spellings"
        " to the name used by the majority of the tracks.",
    "mojibake":
        "The text contains character sequences typical of encoding damage"
        " ('mojibake') - sequences that appear when UTF-8 bytes are displayed"
        " in a wrong codepage, or invisible control characters. Where a safe"
        " automatic repair exists it is proposed; text with control"
        " characters but no safe repair is flagged for manual fixing."
        " Normal accented letters (French, Nordic, Czech, ...) are never"
        " treated as damage.",
    "multi_split":
        "A single stored value contains a separator ('; ', '\\', ' / ', ...)"
        " that indicates several values glued into one string (e.g. 'A; B' in"
        " artist). For fields configured as multi-value (Settings -"
        " Multi-value), the proposal splits the string into real separate"
        " values, as ID3v2.4 properly supports.",
    "value_format":
        "The value does not match the validation rule defined in Settings -"
        " Validation (a regular expression and/or a list of allowed literal"
        " values; e.g. year must be four digits or one of the allowed words)."
        " When the bad value is just the same value duplicated (a common"
        " tagger artifact like '2005\\2005'), collapsing the duplicate is"
        " proposed automatically; anything else needs a manual correction.",
    "single_value":
        "A field that should hold exactly one value (it is not ticked as"
        " multi-value in Settings) contains several values. Identical"
        " duplicates are collapsed automatically; differing values need a"
        " manual decision about which one is right.",
    "publisher_from_comment":
        "The publisher field is empty, but the comment contains a 'Label: ...'"
        " note. The proposal copies the label name from the comment into the"
        " proper Publisher (TPUB) field.",
    "artist_sync":
        "One of artist / album artist is empty while the other has a value."
        " The proposal copies the existing value into the empty field.",
    "online_meta":
        "Values found on the internet (MusicBrainz) by the 'Internet check'"
        " button: either additions that fill an empty field, or differences"
        " from the current value. They are kept strictly separate from rule"
        " proposals and are never applied without your review.",
    "folder_jpg":
        "The album folder has no folder.jpg image (used by players and by"
        " Windows Explorer as the folder thumbnail). The proposal exports the"
        " largest cover embedded in the album's tracks into folder.jpg.",
    "cover":
        "Replaces the cover art embedded in all tracks of the album with the"
        " picture chosen in 'Find cover online...' (and writes folder.jpg"
        " when configured). The previous cover is remembered and can be"
        " restored via History.",
    "manual":
        "A value you typed yourself into a Proposed column. Applied exactly"
        " as written when you apply.",
    "cover_missing":
        "No track of the album has embedded cover art, so players show a"
        " blank square. There is no automatic fix - open the album and use"
        " 'Find cover online...' to pick one.",
    "cover_tiny":
        "The embedded cover is smaller than the minimum size set in Settings"
        " - Checks, so players upscale it and it looks blurry. Use 'Find"
        " cover online...' in the album view to replace it with a larger"
        " picture.",
    "track_gaps":
        "The track numbers in the album are not continuous (e.g. 1, 2, 4 -"
        " number 3 is missing). This usually means a missing file or a"
        " mis-numbered track. There is no automatic fix - check the album by"
        " hand.",
    "year_inconsistent":
        "Tracks inside one album have different YEAR values. This is often"
        " legitimate (compilations, reissues with bonus tracks), so nothing"
        " is changed automatically - check the album and unify the year by"
        " hand if it is wrong.",
    "artist_jpg":
        "The artist folder has no artist.jpg image. Open the artist and use"
        " 'Find artist image...' to pick one online or from disk.",
    "missing":
        "A field marked as required (Settings - Checks) has no value in this"
        " file. Where the app can derive a value it proposes the fill (genre"
        " from the artist's other tracks, publisher from a 'Label:' comment,"
        " artist from the album artist, ...); otherwise the value needs"
        " manual input or the internet check.",
}

# --- per-rule handling mode, configurable in Settings - Problem types -------
#  'enabled'   = detected, shown, applied normally (default)
#  'postponed' = detected and shown, but proposals start postponed: Apply
#                skips them until you 'Restore' a row yourself
#  'disabled'  = not detected, not shown anywhere
RULE_MODES = ("enabled", "postponed", "disabled")
DEFAULT_RULE_MODES = {"id3v1_conflict": "postponed",
                      "apev2_conflict": "postponed"}
# rules the user can switch in Settings ('missing' covers every missing_<field>
# rule; online / manual / cover have their own controls elsewhere)
CONFIGURABLE_RULES = [
    "mojibake", "value_format", "album_inconsistent", "track_gaps", "missing",
    "cover_missing", "id3v1", "id3v1_conflict", "v1_rescue",
    "apev2", "apev2_conflict", "apev2_rescue", "id3_version",
    "encoding", "track_format", "single_value", "multi_split", "albumartist",
    "artist_superset", "artist_sync", "plus_collab", "publisher_from_comment",
    "year_inconsistent", "cover_tiny", "folder_jpg", "artist_jpg",
]


def rule_mode(settings, rule):
    """'enabled' | 'postponed' | 'disabled' for the given rule."""
    if not rule:
        return "enabled"
    key = "missing" if rule.startswith("missing_") else rule
    mode = (settings.get("rule_modes") or {}).get(key)
    if mode not in RULE_MODES:
        mode = DEFAULT_RULE_MODES.get(key, "enabled")
    return mode


def rule_description(rule):
    """Detailed explanation of a rule, for tooltips. '' when unknown."""
    if rule in RULE_DESCRIPTIONS:
        return RULE_DESCRIPTIONS[rule]
    if rule and rule.startswith("missing_"):
        return RULE_DESCRIPTIONS["missing"]
    return ""


def rule_label(rule):
    if rule in RULE_LABELS:
        return RULE_LABELS[rule]
    if rule == "missing":
        return "Missing required field"
    if rule and rule.startswith("missing_"):
        return "Missing %s" % rule[8:]
    return rule or "?"


def missing_field_of(rule):
    return rule[8:] if rule and rule.startswith("missing_") else None


def is_non_fixable(rule):
    return rule in NON_FIXABLE or (rule or "").startswith("missing_")


def detect_mojibake(s):
    """Propose a repair ONLY on strong evidence. Valid French/Italian/German
    accented text must never be flagged (è, à, ì... are normal letters)."""
    # case 1: UTF-8 bytes that were decoded as cp1252/latin-1 ("Ã©" for "é").
    # Reliable: real accented text almost never re-decodes as valid UTF-8.
    for enc in ("cp1252", "latin-1"):
        try:
            b = s.encode(enc)
        except UnicodeEncodeError:
            continue
        try:
            fixed = b.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if (fixed != s and not C1_RE.search(fixed)
                and any(ord(ch) > 127 and ch.isalpha() for ch in fixed)):
            return fixed
    # case 2: cp1250 bytes decoded as cp1252/latin-1 - only when invisible C1
    # control characters prove damage; printable accents are never evidence
    if C1_RE.search(s):
        for enc in ("latin-1", "cp1252"):    # latin-1 first: keeps C1 bytes
            try:
                fixed = s.encode(enc).decode("cp1250")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if (fixed != s and not C1_RE.search(fixed)
                    and any(ch in CZECH_CHARS for ch in fixed)):
                return fixed
    return None


def split_separators(settings):
    """Separator strings that indicate several values stored as one."""
    seps = []
    if settings.get("split_semicolon", True):
        seps += ["; ", ";"]
    if settings.get("split_backslash", True):
        seps += ["\\\\", "\\"]
    if settings.get("split_slash_spaced", True):
        seps += [" / "]
    if settings.get("split_comma", False):
        seps += [", "]
    seps += [s for s in settings.get("split_custom", "").split() if s]
    return seps


def split_value(v, seps):
    """Split v on the first matching separator; None if nothing to split."""
    for sep in seps:
        if sep in v:
            parts = [p.strip() for p in v.split(sep) if p.strip()]
            if len(parts) > 1:
                return parts
    return None


def parse_track(values):
    """('3' or '03' or '3/12') -> (int or None, raw)."""
    if not values:
        return None, ""
    raw = values[0]
    num = raw.split("/")[0].strip()
    return (int(num) if num.isdigit() else None), raw


def format_track(n, total, settings):
    s = "%02d" % n if settings["track_pad"] else str(n)
    if settings["track_totals"] and total:
        pad_total = settings["track_pad"] and settings.get("track_pad_total", True)
        s += "/" + ("%02d" % total if pad_total else str(total))
    return s


def common_artists(track_artist_lists):
    """Artists present on every track, in first-track order. [] if none."""
    if not track_artist_lists:
        return []
    sets = [set(a) for a in track_artist_lists]
    inter = set.intersection(*sets)
    return [a for a in track_artist_lists[0] if a in inter]


def _v1_variants(v1v):
    """The v1 value plus its encoding repairs. ID3v1 is decoded as cp1250 at
    read time, but many taggers wrote UTF-8 bytes into it - then 'Dan Bárta'
    reads back as 'Dan BÃ¡rta'. Such a value is NOT a real conflict with the
    identical ID3v2 value, so the comparison also tries the repaired form."""
    variants = [v1v]
    for enc in ("cp1250", "cp1252", "latin-1"):
        try:
            fixed = v1v.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != v1v:
            variants.append(fixed)
    return variants


def v1_conflicts(v1, tags):
    """[(field, v1_value, v2_value)] where the old tag disagrees with ID3v2.
    Truncation-aware: a v1 value that is a prefix of the v2 value is fine.
    Encoding-damage-aware: a v1 value whose repair matches ID3v2 is fine."""
    out = []
    for field in ("title", "artist", "album", "year", "comment", "track", "genre"):
        v1v = (v1.get(field) or [""])[0].strip()
        if not v1v:
            continue
        v2list = tags.get(field) or []
        if not v2list:
            continue        # only in v1 -> handled by the rescue rule
        if field == "genre":
            if v1v.casefold() not in {g.casefold() for g in v2list}:
                out.append((field, v1v, " | ".join(v2list)))
            continue
        if field == "track":
            n2, _raw = parse_track(v2list)
            if n2 is None or str(n2) != v1v.lstrip("0").strip() and v1v != str(n2):
                out.append((field, v1v, v2list[0]))
            continue
        cands = [v2list[0], " / ".join(v2list), "/".join(v2list),
                 "; ".join(v2list), " ".join(v2list)]
        if not any(c.casefold().startswith(v.casefold())
                   for v in _v1_variants(v1v) for c in cands):
            out.append((field, v1v, " | ".join(v2list)))
    return out


APE_FIELDS = ("title", "artist", "albumartist", "album", "year",
              "comment", "composer", "track", "disc", "genre")


def ape_conflicts(ape, tags):
    """[(field, ape_values, v2_display)] where the APEv2 tag disagrees with
    ID3v2. Unlike ID3v1, APEv2 stores full-length UTF-8 text, so the match is
    exact (not prefix-based); it stays multi-value aware."""
    out = []
    for field in APE_FIELDS:
        apv = [str(v).strip() for v in (ape.get(field) or []) if str(v).strip()]
        if not apv:
            continue
        v2list = [str(v) for v in (tags.get(field) or []) if str(v).strip()]
        if not v2list:
            continue        # only in APEv2 -> handled by the rescue rule
        if field == "genre":
            if {a.casefold() for a in apv} - {g.casefold() for g in v2list}:
                out.append((field, apv, " | ".join(v2list)))
            continue
        if field in ("track", "disc"):
            # compare the leading numbers, ignoring any '/total' either side
            n2, _r2 = parse_track(v2list)
            na, _ra = parse_track(apv)
            if n2 != na:
                out.append((field, apv, v2list[0]))
            continue
        cands = {v2list[0], " / ".join(v2list), "/".join(v2list),
                 "; ".join(v2list), " ".join(v2list)}
        cand_cf = {c.casefold() for c in cands}
        if (" / ".join(apv).casefold() not in cand_cf
                and apv[0].casefold() not in cand_cf):
            out.append((field, apv, " | ".join(v2list)))
    return out


def value_pattern_check(vals, pattern, seps):
    """Return (bad_values, fixed_or_None). Fixes duplicates like '2005\\2005'."""
    regex = (pattern.get("regex") or "").strip()
    allowed = {a.strip().casefold()
               for a in (pattern.get("allowed") or "").split(";") if a.strip()}

    def valid(v):
        if regex:
            try:
                if re.fullmatch(regex, v):
                    return True
            except re.error:
                return True     # broken user regex: don't flag anything
        return v.casefold() in allowed

    bad = [v for v in vals if not valid(v)]
    if not bad:
        return [], None
    fixed = []
    for v in vals:
        if valid(v):
            fixed.append(v)
            continue
        parts = split_value(v, seps) or split_value(v, ["\\\\", "\\", ";", " / "])
        if parts and len(set(parts)) == 1 and valid(parts[0]):
            fixed.append(parts[0])
        else:
            return bad, None    # not automatically fixable
    return bad, fixed


def evaluate(con, settings, artist_folders=None, album_dirs=None):
    """Re-generate issues + rule proposals for the given scope (or everything)."""
    where, params = "missing=0", []
    if album_dirs is not None:
        where += " AND album_dir IN (%s)" % ",".join("?" * len(album_dirs))
        params += list(album_dirs)
    elif artist_folders is not None:
        where += " AND artist_folder IN (%s)" % ",".join("?" * len(artist_folders))
        params += list(artist_folders)

    rows = con.execute(
        "SELECT id, path, artist_folder, album_dir, filename FROM tracks WHERE " + where,
        params).fetchall()
    albums = defaultdict(list)
    artists_in_scope = set()
    for tid, path, artist, adir, fname in rows:
        snap = db.latest_snapshot(con, tid)
        if snap is None:
            continue
        albums[adir].append({"id": tid, "path": path, "artist": artist,
                             "file": fname, "tags": snap["tags"]})
        artists_in_scope.add(artist)

    # wipe previous issues and untouched rule proposals in scope
    scope_albums = list(albums.keys())
    if scope_albums:
        qs = ",".join("?" * len(scope_albums))
        con.execute("DELETE FROM issues WHERE album_dir IN (%s)" % qs, scope_albums)
        con.execute("DELETE FROM proposals WHERE album_dir IN (%s)"
                    " AND source='rule' AND status IN ('pending','needs_input')" % qs,
                    scope_albums)
        # disabled problem types: drop even postponed/edited leftovers in scope
        open_qs = ",".join("'%s'" % s for s in
                           ("pending", "edited", "postponed", "needs_input"))
        disabled = [r for r in RULE_LABELS
                    if rule_mode(settings, r) == "disabled"]
        if disabled:
            rq = ",".join("?" * len(disabled))
            con.execute("DELETE FROM proposals WHERE album_dir IN (%s)"
                        " AND rule IN (%s) AND status IN (%s)"
                        % (qs, rq, open_qs), scope_albums + disabled)
        if rule_mode(settings, "missing") == "disabled":
            con.execute("DELETE FROM proposals WHERE album_dir IN (%s)"
                        " AND rule LIKE 'missing~_%%' ESCAPE '~'"
                        " AND status IN (%s)" % (qs, open_qs), scope_albums)

    # postponed / needs-input rule proposals survive regeneration, but only
    # while their rule still produces them; anything not re-proposed in this
    # run is stale (e.g. the situation changed after an apply) and is dropped
    stale_candidates = set()
    if scope_albums:
        qs = ",".join("?" * len(scope_albums))
        stale_candidates = {r[0] for r in con.execute(
            "SELECT id FROM proposals WHERE album_dir IN (%s) AND source='rule'"
            " AND status IN ('postponed','needs_input')" % qs, scope_albums)}
    touched_ids = set()
    if artists_in_scope:
        qs = ",".join("?" * len(artists_in_scope))
        con.execute("DELETE FROM issues WHERE album_dir IS NULL"
                    " AND artist_folder IN (%s)" % qs, list(artists_in_scope))

    exc = db.load_exceptions(con, list(artists_in_scope) or None)
    artist_of_album = {adir: ts[0]["artist"] for adir, ts in albums.items()}

    def excepted(adir, track_id, rule):
        artist = artist_of_album.get(adir)
        return ((artist, adir, track_id, rule) in exc
                or (artist, adir, None, rule) in exc
                or (artist, None, None, rule) in exc)

    def issue(track_id, artist, adir, rule, sev, msg):
        if rule_mode(settings, rule) == "disabled" or excepted(adir, track_id, rule):
            return False
        con.execute("INSERT INTO issues(track_id, artist_folder, album_dir, rule,"
                    " severity, message) VALUES (?,?,?,?,?,?)",
                    (track_id, artist, adir, rule, sev, msg))
        return True

    def propose(track_id, artist, adir, field, current, proposed, rule,
                status="pending", note=None):
        mode = rule_mode(settings, rule)
        if mode == "disabled" or excepted(adir, track_id, rule):
            return
        if mode == "postponed" and status == "pending":
            status = "postponed"
        rid = db.upsert_proposal(con, track_id, artist, adir, field, current,
                                 proposed, "rule", status=status, rule=rule,
                                 note=note)
        touched_ids.add(rid)

    required = settings.get("required_fields", [])
    mv_fields = settings.get("multi_value_fields", [])
    seps = split_separators(settings)
    patterns = settings.get("field_patterns", {})
    sync = settings.get("sync_artist_albumartist", True)

    # genre majority per artist folder (for filling missing genres)
    artist_genres = defaultdict(Counter)
    for adir, tracks in albums.items():
        for t in tracks:
            for g in t["tags"].get("genre", []):
                artist_genres[t["artist"]][g] += 1

    for adir, tracks in albums.items():
        artist = tracks[0]["artist"]
        nums = []
        artist_lists = []

        for t in tracks:
            tags = t["tags"]
            tid = t["id"]
            v1 = tags.get("_v1") or {}

            # -- required fields, with a fill chain where possible
            for field in required:
                if tags.get(field):
                    continue
                sev = RED if field in CORE_RED else YEL
                issue(tid, artist, adir, "missing_" + field, sev,
                      "%s: missing %s" % (t["file"], field))
                if field == "genre" and settings["genre_policy"] == "fill_missing":
                    best = artist_genres[artist].most_common(1)
                    if best:
                        propose(tid, artist, adir, "genre", [], [best[0][0]],
                                "missing_genre")
                elif field == "publisher":
                    m = re.match(r"(?i)\s*label:\s*(.+?)\s*(?:\[[^\]]*\])?\s*$",
                                 " ".join(tags.get("comment", [])))
                    if m:
                        propose(tid, artist, adir, "publisher", [], [m.group(1)],
                                "publisher_from_comment")
                elif field == "artist" and sync and tags.get("albumartist"):
                    propose(tid, artist, adir, "artist", [],
                            tags["albumartist"], "artist_sync")
                elif (field == "albumartist" and sync and tags.get("artist")
                      and settings["albumartist_mode"] == "keep"):
                    propose(tid, artist, adir, "albumartist", [],
                            tags["artist"], "artist_sync")

            # -- old ID3v1 leftover: verified before removal is proposed
            if not tags.get("_has_v1"):
                # a resolved "keep v2" decision is consumed once the tag is gone
                con.execute("DELETE FROM v1_keep_v2 WHERE track_id=?", (tid,))
            if tags.get("_has_v1") and settings["strip_id3v1"]:
                # a disabled 'id3v1_conflict' type means: ignore differences
                conflicts = (v1_conflicts(v1, tags)
                             if rule_mode(settings, "id3v1_conflict") != "disabled"
                             else [])
                # user chose "keep ID3v2" -> conflict is resolved by decision
                chose_v2 = con.execute(
                    "SELECT 1 FROM v1_keep_v2 WHERE track_id=?",
                    (tid,)).fetchone() is not None
                rescues = [f for f in ("title", "artist", "album", "track",
                                       "year", "genre", "comment")
                           if v1.get(f) and not tags.get(f)]
                if conflicts and not chose_v2:
                    issue(tid, artist, adir, "id3v1_conflict", RED,
                          "%s: old ID3v1 tag disagrees with ID3v2 (the current"
                          " tag) — resolve before removal: %s"
                          % (t["file"], "; ".join(
                              "%s: old v1='%s' vs current v2='%s'" % c
                              for c in conflicts)))
                    # per-field "use v1" offers are created at the end of the
                    # track (after mojibake etc.), so they never displace a fix
                    t["_v1_offers"] = conflicts
                elif conflicts and chose_v2:
                    issue(tid, artist, adir, "id3v1", YEL,
                          "%s: old ID3v1 tag differs — decision made: ID3v2"
                          " stays, the old tag will be removed" % t["file"])
                    propose(tid, artist, adir, "_id3v1",
                            ["differs — ID3v2 stays"], ["remove"], "id3v1")
                elif rescues:
                    issue(tid, artist, adir, "id3v1", YEL,
                          "%s: old ID3v1 tag holds data missing in ID3v2 (%s) —"
                          " it is removed together with writing the rescued data"
                          % (t["file"], ", ".join(rescues)))
                else:
                    issue(tid, artist, adir, "id3v1", YEL,
                          "%s: old ID3v1 tag — checked, all its information is"
                          " preserved in ID3v2" % t["file"])
                    propose(tid, artist, adir, "_id3v1",
                            ["present — all info preserved in ID3v2"], ["remove"],
                            "id3v1")

            # -- foreign APEv2 tag: verified before removal is proposed
            # (same flow as the ID3v1 leftover above)
            ape = tags.get("_ape") or {}
            if not tags.get("_has_ape"):
                con.execute("DELETE FROM ape_keep_v2 WHERE track_id=?", (tid,))
            if tags.get("_has_ape") and settings.get("strip_apev2", True):
                ape_conf = (ape_conflicts(ape, tags)
                            if rule_mode(settings, "apev2_conflict") != "disabled"
                            else [])
                chose_v2_ape = con.execute(
                    "SELECT 1 FROM ape_keep_v2 WHERE track_id=?",
                    (tid,)).fetchone() is not None
                ape_rescues = [f for f in APE_FIELDS
                               if ape.get(f) and not tags.get(f)]
                if ape_conf and not chose_v2_ape:
                    issue(tid, artist, adir, "apev2_conflict", RED,
                          "%s: APEv2 tag disagrees with ID3v2 (the current tag)"
                          " — resolve before removal: %s"
                          % (t["file"], "; ".join(
                              "%s: APEv2='%s' vs current v2='%s'"
                              % (f, " | ".join(av), v2v)
                              for f, av, v2v in ape_conf)))
                    # per-field "use APEv2" offers are created at the end of the
                    # track (after mojibake etc.), so they never displace a fix
                    t["_ape_offers"] = ape_conf
                elif ape_conf and chose_v2_ape:
                    issue(tid, artist, adir, "apev2", YEL,
                          "%s: APEv2 tag differs — decision made: ID3v2 stays,"
                          " the APEv2 tag will be removed" % t["file"])
                    propose(tid, artist, adir, "_apev2",
                            ["differs — ID3v2 stays"], ["remove"], "apev2")
                elif ape_rescues:
                    issue(tid, artist, adir, "apev2", YEL,
                          "%s: APEv2 tag holds data missing in ID3v2 (%s) — it is"
                          " removed together with writing the rescued data"
                          % (t["file"], ", ".join(ape_rescues)))
                else:
                    issue(tid, artist, adir, "apev2", YEL,
                          "%s: APEv2 tag — checked, all its information is"
                          " preserved in ID3v2" % t["file"])
                    propose(tid, artist, adir, "_apev2",
                            ["present — all info preserved in ID3v2"], ["remove"],
                            "apev2")
            if tags.get("_version") and tags["_version"] != "2.4":
                issue(tid, artist, adir, "id3_version", YEL,
                      "%s: ID3v%s (will be upgraded to 2.4)" % (t["file"], tags["_version"]))
                propose(tid, artist, adir, "_version",
                        [tags["_version"]], ["2.4"], "id3_version")
            # -- non-UTF-8 text frames (UTF-8 is the ID3v2.4 standard)
            if settings.get("utf8_all_frames", True):
                encs = sorted(k for k in (tags.get("_enc") or {}) if k != "utf-8")
                if encs:
                    issue(tid, artist, adir, "encoding", YEL,
                          "%s: text stored as %s (will be re-encoded to UTF-8)"
                          % (t["file"], ", ".join(encs)))
                    propose(tid, artist, adir, "_encoding",
                            encs, ["utf-8"], "encoding")

            # -- data that exists ONLY in the old ID3v1 tag: rescue it into v2
            for field in ("title", "artist", "album", "track", "year",
                          "genre", "comment"):
                if v1.get(field) and not tags.get(field):
                    issue(tid, artist, adir, "v1_rescue", YEL,
                          "%s: %s '%s' exists only in the old ID3v1 tag"
                          % (t["file"], field, v1[field][0]))
                    propose(tid, artist, adir, field, [], v1[field], "v1_rescue")

            # -- data that exists ONLY in the APEv2 tag: rescue it into v2
            for field in APE_FIELDS:
                if ape.get(field) and not tags.get(field):
                    issue(tid, artist, adir, "apev2_rescue", YEL,
                          "%s: %s '%s' exists only in the APEv2 tag"
                          % (t["file"], field, ape[field][0]))
                    propose(tid, artist, adir, field, [], ape[field],
                            "apev2_rescue")

            # -- mojibake repair + splitting combined multi-values
            for field in TEXTUAL_FIELDS:
                vals = tags.get(field, [])
                if not vals:
                    continue
                fixed_vals = [detect_mojibake(v) or v for v in vals]
                had_mojibake = fixed_vals != vals
                final = []
                had_split = False
                for v in fixed_vals:
                    parts = split_value(v, seps) if field in mv_fields else None
                    if parts:
                        final.extend(parts)
                        had_split = True
                    else:
                        final.append(v)
                if had_mojibake or had_split:
                    rule = "mojibake" if had_mojibake else "multi_split"
                    sev = RED if had_mojibake else YEL
                    if issue(tid, artist, adir, rule, sev,
                             "%s: %s '%s' -> '%s'" % (t["file"], field,
                                                      " | ".join(vals),
                                                      " | ".join(final))):
                        propose(tid, artist, adir, field, vals, final, rule)
                elif any(C1_RE.search(v) for v in vals):
                    # broken control characters with no safe automatic repair:
                    # high priority, fix by hand
                    if issue(tid, artist, adir, "mojibake", RED,
                             "%s: %s '%s' contains broken (invisible) characters"
                             " — no safe automatic repair, fix manually"
                             % (t["file"], field, " | ".join(vals))):
                        propose(tid, artist, adir, field, vals, [], "mojibake",
                                status="needs_input",
                                note="broken characters — needs manual input")

            # -- several values in a field that should hold one (MediaMonkey's
            # duplicated year etc.); identical duplicates collapse automatically
            for field, vals in list(tags.items()):
                if (field.startswith("_") or field in mv_fields
                        or not isinstance(vals, list) or len(vals) < 2):
                    continue
                if issue(tid, artist, adir, "single_value", YEL,
                         "%s: %s has %d values ('%s') but is a single-value"
                         " field" % (t["file"], field, len(vals),
                                     " | ".join(vals))):
                    if len(set(vals)) == 1:
                        propose(tid, artist, adir, field, vals, [vals[0]],
                                "single_value")
                    else:
                        propose(tid, artist, adir, field, vals, [],
                                "single_value", status="needs_input",
                                note="values differ — needs manual input")

            # -- value format validation (regex + allowed literals)
            for field, pattern in patterns.items():
                vals = tags.get(field, [])
                if not vals:
                    continue
                bad, fixed = value_pattern_check(vals, pattern, seps)
                if not bad:
                    continue
                if issue(tid, artist, adir, "value_format", RED,
                         "%s: %s '%s' doesn't match the required format"
                         % (t["file"], field, " | ".join(bad))):
                    if fixed is not None:
                        propose(tid, artist, adir, field, vals, fixed,
                                "value_format")
                    else:
                        propose(tid, artist, adir, field, vals, [],
                                "value_format", status="needs_input",
                                note="needs manual input")

            # -- cover
            covers = tags.get("_cover", [])
            if not covers:
                issue(tid, artist, adir, "cover_missing", RED,
                      "%s: no embedded cover art" % t["file"])
            else:
                px = min((min(c["w"], c["h"]) for c in covers if c["w"]), default=None)
                if px is not None and px < settings["cover_min_px"]:
                    issue(tid, artist, adir, "cover_tiny", YEL,
                          "%s: embedded cover only %d px" % (t["file"], px))

            # -- per-field "use the ID3v1 value" offers (only where no OTHER
            # rule already proposed something for the field; an existing offer
            # of this same rule is refreshed so it never goes stale)
            for field, v1v, v2v in t.pop("_v1_offers", []):
                row = con.execute(
                    "SELECT rule FROM proposals WHERE track_id=? AND field=?"
                    " AND status IN ('pending','edited','postponed','needs_input')"
                    " LIMIT 1", (tid, field)).fetchone()
                if row is None or row[0] == "id3v1_conflict":
                    # default status comes from the rule's mode in Settings
                    # (postponed unless the user enabled it)
                    propose(tid, artist, adir, field,
                            tags.get(field, []), [v1v], "id3v1_conflict",
                            note="old ID3v1 tag says '%s'; ID3v2 (the current"
                                 " tag) says '%s' — apply this proposal to use"
                                 " the old v1 value (the old tag is removed in"
                                 " the same step), or right-click → keep ID3v2"
                                 % (v1v, v2v))

            # -- per-field "use the APEv2 value" offers (same rules as v1)
            for field, apv, v2v in t.pop("_ape_offers", []):
                row = con.execute(
                    "SELECT rule FROM proposals WHERE track_id=? AND field=?"
                    " AND status IN ('pending','edited','postponed','needs_input')"
                    " LIMIT 1", (tid, field)).fetchone()
                if row is None or row[0] == "apev2_conflict":
                    propose(tid, artist, adir, field,
                            tags.get(field, []), apv, "apev2_conflict",
                            note="APEv2 tag says '%s'; ID3v2 (the current tag)"
                                 " says '%s' — apply this proposal to use the"
                                 " APEv2 value (the tag is removed in the same"
                                 " step), or right-click → keep ID3v2"
                                 % (" | ".join(apv), v2v))

            # -- required fields still without any proposal -> ask for input
            for field in required:
                if tags.get(field):
                    continue
                row = con.execute(
                    "SELECT rule FROM proposals WHERE track_id=? AND field=?"
                    " AND status IN ('pending','edited','postponed','needs_input')"
                    " LIMIT 1", (tid, field)).fetchone()
                if row is None or row[0] == "missing_" + field:
                    propose(tid, artist, adir, field, [], [],
                            "missing_" + field, status="needs_input",
                            note="needs manual input (or run the internet check)")

            n, _raw = parse_track(tags.get("track", []))
            if n is not None:
                nums.append(n)
            if tags.get("artist"):
                artist_lists.append(tags["artist"])

        # -- track number formatting (needs album total)
        total = max(nums) if nums else None
        for t in tracks:
            n, raw = parse_track(t["tags"].get("track", []))
            if n is None:
                continue
            want = format_track(n, total, settings)
            if raw != want:
                if issue(t["id"], artist, adir, "track_format", YEL,
                         "%s: track '%s' -> '%s'" % (t["file"], raw, want)):
                    propose(t["id"], artist, adir, "track", [raw], [want],
                            "track_format")

        # -- track number gaps
        s = sorted(set(nums))
        if s and s != list(range(s[0], s[0] + len(s))):
            missing_nums = sorted(set(range(s[0], s[-1] + 1)) - set(s))
            issue(None, artist, adir, "track_gaps", RED,
                  "Track numbering has gaps (missing: %s)"
                  % ", ".join(map(str, missing_nums[:10])))

        # -- album name consistency
        names = Counter()
        for t in tracks:
            for v in t["tags"].get("album", [])[:1]:
                names[v] += 1
        if len(names) > 1:
            majority = names.most_common(1)[0][0]
            if issue(None, artist, adir, "album_inconsistent", RED,
                     "Album name differs between tracks: %s" % " | ".join(names)):
                for t in tracks:
                    cur = t["tags"].get("album", [])
                    if cur and cur[0] != majority:
                        propose(t["id"], artist, adir, "album", cur, [majority],
                                "album_inconsistent")
        # -- year consistency
        years = {t["tags"]["year"][0] for t in tracks if t["tags"].get("year")}
        if len(years) > 1:
            issue(None, artist, adir, "year_inconsistent", YEL,
                  "Year differs between tracks: %s" % ", ".join(sorted(years)))

        def effective_vals(tid, field, fallback):
            """Current tag values, or what an open proposal will turn them into.
            id3v1_conflict rows don't count: they only OFFER the old v1 value
            as an alternative — treating that offer as the future value made
            album-level rules fire on it and overwrite the offer itself
            (e.g. v1 'Kiioto' vs v2 'Kiiōtō' spawned a bogus artist_superset
            proposal and the conflict lost its current/proposed display)."""
            row = con.execute(
                "SELECT proposed FROM proposals WHERE track_id=? AND field=?"
                " AND status IN ('pending','edited','postponed')"
                " AND (rule IS NULL OR rule != 'id3v1_conflict')",
                (tid, field)).fetchone()
            if row:
                try:
                    vals = json.loads(row[0])
                except ValueError:
                    vals = None
                if vals:
                    return vals
            return fallback

        # -- album artist rule. Works on EFFECTIVE values (what each field
        # will hold once already-proposed changes such as splits or mojibake
        # repairs are applied) so dependent rules never fight each other or
        # overwrite each other's proposals with stale data.
        aa_mode = settings["albumartist_mode"]
        if aa_mode in ("common", "subset") and artist_lists:
            eff_artist_lists = [
                effective_vals(t["id"], "artist", t["tags"]["artist"])
                for t in tracks if t["tags"].get("artist")]
            common = common_artists(eff_artist_lists)
            eff_aa = {t["id"]: effective_vals(t["id"], "albumartist",
                                              t["tags"].get("albumartist", []))
                      for t in tracks}
            non_empty = [tuple(v) for v in eff_aa.values() if v]
            if aa_mode == "common" or not non_empty:
                # derive the album artist from the track artists
                want = common if common else [settings["va_name"]]
                why = ("compilation" if not common else
                       "collaboration" if len(want) > 1 else "main artist")
            else:
                # 'subset': the album artist is the user's own choice - just
                # unify it to the majority value inside the album
                want = list(Counter(non_empty).most_common(1)[0][0])
                why = "majority inside the album"
            wrong = [t for t in tracks if eff_aa[t["id"]] != want]
            if wrong:
                if issue(None, artist, adir, "albumartist", YEL,
                         "Album artist should be '%s' (%s) on all tracks"
                         % (", ".join(want), why)):
                    for t in wrong:
                        propose(t["id"], artist, adir, "albumartist",
                                t["tags"].get("albumartist", []), want,
                                "albumartist")

        # -- '+' in the album folder name (e.g. "1969 - + Mothers Of Invention
        # - Uncle Meat" inside the Frank Zappa folder) suggests a collaboration:
        # artist and album artist are then expected to hold TWO OR MORE values.
        # Only a warning - names are never guessed or verified. (Runs before
        # the superset check so a proposed split counts as the artist there.)
        base_name = adir.replace("\\", "/").rsplit("/", 1)[-1]
        if "+" in base_name and settings.get("check_plus_collab", True):
            plus_seps = split_separators(settings) + [" + ", "+"]
            # only ARTIST is checked: the album artist may legitimately hold
            # just the main artist even on a collaboration album
            for t in tracks:
                vals = t["tags"].get("artist", [])
                if not vals:
                    continue
                effective = effective_vals(t["id"], "artist", vals)
                if len(effective) > 1:
                    continue
                parts = split_value(effective[0], plus_seps)
                if issue(t["id"], artist, adir, "plus_collab", YEL,
                         "%s: more artists may be expected based on the folder"
                         " name ('+'), but there is only '%s'"
                         % (t["file"], effective[0])):
                    if parts:
                        propose(t["id"], artist, adir, "artist", vals, parts,
                                "plus_collab")

        if aa_mode == "subset" and artist_lists:
            # artist must CONTAIN the album artist (plus optional extras
            # such as guests); propose adding what is missing. An already
            # proposed artist value (e.g. a split) counts as the artist.
            for t in tracks:
                art = t["tags"].get("artist", [])
                if not art:
                    continue
                effective = effective_vals(t["id"], "artist", art)
                missing = [a for a in want
                           if a not in effective and a != settings["va_name"]]
                if missing:
                    proposed = missing + [x for x in effective
                                          if x not in missing]
                    if issue(t["id"], artist, adir, "artist_superset", YEL,
                             "%s: artist '%s' is missing the album artist"
                             " '%s'" % (t["file"], " | ".join(effective),
                                        " | ".join(missing))):
                        propose(t["id"], artist, adir, "artist", art,
                                proposed, "artist_superset")

        # -- folder.jpg
        row = con.execute("SELECT folder_jpg FROM albums WHERE album_dir=?",
                          (adir,)).fetchone()
        if row and not row[0] and settings["write_folder_jpg"]:
            has_embedded = any(t["tags"].get("_cover") for t in tracks)
            if issue(None, artist, adir, "folder_jpg", YEL,
                     "No folder.jpg in album folder") and has_embedded:
                propose(None, artist, adir, "folder_jpg",
                        ["missing"], ["export from embedded cover"], "folder_jpg")

    # drop stale postponed / needs-input rule proposals (not re-proposed above)
    stale = stale_candidates - touched_ids
    if stale:
        qs = ",".join("?" * len(stale))
        con.execute("DELETE FROM proposals WHERE id IN (%s)" % qs, list(stale))

    # -- artist.jpg
    for artist in artists_in_scope:
        if rule_mode(settings, "artist_jpg") == "disabled":
            break
        row = con.execute("SELECT artist_jpg FROM artists WHERE folder=?",
                          (artist,)).fetchone()
        if row and not row[0] and (artist, None, None, "artist_jpg") not in exc:
            con.execute("INSERT INTO issues(track_id, artist_folder, album_dir,"
                        " rule, severity, message) VALUES (?,?,?,?,?,?)",
                        (None, artist, None, "artist_jpg", YEL,
                         "No artist.jpg in artist folder (use 'Find artist image')"))
    con.commit()

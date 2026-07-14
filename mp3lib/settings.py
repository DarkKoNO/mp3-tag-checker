"""Configuration: libraries (root folders, each with its own database),
the active library, and user-tunable settings.

Everything lives in config.json next to the app. Legacy single-root configs
are migrated automatically; the loader never destroys the file.
"""

import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent   # code + shipped resources
# All USER DATA (config, themes, library databases) lives in DATA_DIR. It is
# BASE_DIR by default, but can be redirected with the MP3TAGGER_DATA_DIR
# environment variable so tests (and portable installs) never read or write the
# real files. Only user data moves — version.json and other shipped resources
# stay under BASE_DIR.
import os as _os
DATA_DIR = Path(_os.environ["MP3TAGGER_DATA_DIR"]).expanduser() \
    if _os.environ.get("MP3TAGGER_DATA_DIR") else BASE_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "library.db"   # default/fallback (legacy + tests)
THEMES_PATH = DATA_DIR / "themes.json"   # user-defined themes

# ---------------------------------------------------------------- themes ----
# A theme = every color and font the app uses. 'Light' and 'Dark' are built
# in and read-only (they can only be duplicated via 'Save as new theme');
# user themes live in themes.json. settings['theme'] holds 'auto' (follow
# Windows) or a theme name.

# (category, [(key, label), ...]) - also drives the Settings editor layout
THEME_COLOR_SPEC = [
    ("General", [
        ("window", "Window background"),
        ("window_text", "Window text"),
        ("base", "Lists / inputs background"),
        ("alternate_base", "Alternate row background"),
        ("text", "Lists / inputs text"),
        ("button", "Button background"),
        ("button_text", "Button text"),
        ("highlight", "Selection background"),
        ("highlighted_text", "Selection text"),
        ("tooltip_base", "Tooltip background"),
        ("tooltip_text", "Tooltip text"),
        ("link", "Links"),
        ("placeholder", "Placeholder text"),
        ("disabled_text", "Disabled text"),
    ]),
    ("Problems & statuses", [
        ("severity_red", "Red — real problem"),
        ("severity_yellow", "Yellow — should be improved"),
        ("severity_green", "Green — OK"),
        ("postponed", "Postponed"),
        ("exception", "Exception (permanently ignored)"),
        ("attention", "Attention (unconfirmed edit)"),
    ]),
    ("Menu & toolbar", [
        ("page_bar_bg", "Page menu background"),
        ("page_text", "Page button text"),
        ("page_active_bg", "Active page background"),
        ("page_active_text", "Active page text"),
        ("page_hover_bg", "Page hover background"),
        ("group_bg", "Button-block background"),
        ("caption", "Button-block captions"),
    ]),
]
# font slots: (key, label) - empty family / size 0 = system default
THEME_FONT_SPEC = [
    ("base", "Application (everything)"),
    ("lists", "Lists & tables"),
    ("menu", "Page menu"),
]

# --------------------------------------------------------- field labels ----
# How metadata fields are DISPLAYED everywhere in the GUI (tables, grids,
# search, settings). Sets work like themes: built-ins are read-only (only
# duplicable via 'Save as new set'), user sets live in the settings.
# The technical tag names (dict keys) never change - only their display.
FIELD_LABEL_SETS = {
    "Technical (tag names)": {},    # empty = show the raw technical names
    "English": {
        "title": "Title", "artist": "Artist", "albumartist": "Album Artist",
        "album": "Album", "track": "Track", "disc": "Disc", "year": "Year",
        "genre": "Genre", "comment": "Comment", "composer": "Composer",
        "conductor": "Conductor", "remixer": "Remixer", "lyricist": "Lyricist",
        "origartist": "Original Artist", "origdate": "Original Date",
        "grouping": "Grouping", "subtitle": "Subtitle",
        "discsubtitle": "Disc Subtitle", "publisher": "Publisher",
        "copyright": "Copyright", "language": "Language", "bpm": "BPM",
        "isrc": "ISRC", "compilation": "Compilation", "mood": "Mood",
        "artistsort": "Artist Sort", "albumsort": "Album Sort",
        "titlesort": "Title Sort",
    },
    "Czech": {
        "title": "Skladba", "artist": "Interpret",
        "albumartist": "Interpret alba", "album": "Album", "track": "Stopa",
        "disc": "Část sady", "year": "Rok", "genre": "Žánr",
        "comment": "Komentář", "composer": "Autor hudby",
        "conductor": "Dirigent", "remixer": "Remixér", "lyricist": "Textař",
        "origartist": "Původní interpret", "origdate": "Rok pův.",
        "grouping": "Seskupení", "subtitle": "Podtitul",
        "discsubtitle": "Podtitul disku", "publisher": "Label",
        "copyright": "Copyright", "language": "Jazyk", "bpm": "BPM",
        "isrc": "ISRC", "compilation": "Kompilace", "mood": "Nálada",
        "artistsort": "Řazení — interpret", "albumsort": "Řazení — album",
        "titlesort": "Řazení — skladba",
    },
}


def resolve_field_labels(settings):
    """The active {technical field -> display label} mapping. User sets fall
    back to English for fields they leave empty; unknown set names fall back
    to English entirely."""
    name = settings.get("field_label_set", "English")
    if name in FIELD_LABEL_SETS:
        return dict(FIELD_LABEL_SETS[name])
    user = settings.get("field_label_sets") or {}
    out = dict(FIELD_LABEL_SETS["English"])
    if name in user and isinstance(user[name], dict):
        out.update({k: v for k, v in user[name].items()
                    if isinstance(v, str) and v.strip()})
    return out

BUILTIN_THEMES = {
    "Light": {
        "colors": {
            "window": "#f2f2f2", "window_text": "#1a1a1a",
            "base": "#ffffff", "alternate_base": "#f0f0f0",
            "text": "#1a1a1a",
            "button": "#e9e9e9", "button_text": "#1a1a1a",
            "highlight": "#3874c8", "highlighted_text": "#ffffff",
            "tooltip_base": "#ffffdc", "tooltip_text": "#1a1a1a",
            "link": "#2a6bc6", "placeholder": "#8a8a8a",
            "disabled_text": "#a0a0a0",
            "severity_red": "#d9534f", "severity_yellow": "#e0a800",
            "severity_green": "#4a9e4a",
            "postponed": "#8f8f8f", "exception": "#9575cd",
            "attention": "#3874c8",
            "page_bar_bg": "#e2e2e2", "page_text": "#1a1a1a",
            "page_active_bg": "#3874c8",
            "page_active_text": "#ffffff", "page_hover_bg": "#d0d0d0",
            "group_bg": "#e9e9e9", "caption": "#777777",
        },
        "fonts": {"base": {"family": "", "size": 0},
                  "lists": {"family": "", "size": 0},
                  "menu": {"family": "", "size": 0}},
    },
    "Dark": {
        "colors": {
            "window": "#353535", "window_text": "#dcdcdc",
            "base": "#2d2d2d", "alternate_base": "#3c3c3c",
            "text": "#dcdcdc",
            "button": "#3a3a3a", "button_text": "#dcdcdc",
            "highlight": "#2a64a0", "highlighted_text": "#ffffff",
            "tooltip_base": "#353535", "tooltip_text": "#dcdcdc",
            "link": "#5aa0ff", "placeholder": "#8c8c8c",
            "disabled_text": "#787878",
            "severity_red": "#e0645f", "severity_yellow": "#e0a800",
            "severity_green": "#5cb85c",
            "postponed": "#9a9a9a", "exception": "#b39ddb",
            "attention": "#2a64a0",
            "page_bar_bg": "#404040", "page_text": "#dcdcdc",
            "page_active_bg": "#2a64a0",
            "page_active_text": "#ffffff", "page_hover_bg": "#4a4a4a",
            "group_bg": "#3d3d3d", "caption": "#9a9a9a",
        },
        "fonts": {"base": {"family": "", "size": 0},
                  "lists": {"family": "", "size": 0},
                  "menu": {"family": "", "size": 0}},
    },
}


def _complete_theme(theme, base="Light"):
    """Fill missing colors/fonts from a built-in, so old/partial user themes
    (and future new keys) always resolve to a complete theme."""
    ref = BUILTIN_THEMES[base]
    out = {"colors": dict(ref["colors"]), "fonts":
           {k: dict(v) for k, v in ref["fonts"].items()}}
    for k, v in (theme.get("colors") or {}).items():
        if isinstance(v, str):
            out["colors"][k] = v
    for k, v in (theme.get("fonts") or {}).items():
        if isinstance(v, dict) and k in out["fonts"]:
            out["fonts"][k] = {"family": str(v.get("family", "")),
                               "size": int(v.get("size", 0) or 0)}
    return out


def load_themes():
    """User themes from themes.json: {name: theme}. Never raises."""
    if not THEMES_PATH.exists():
        return {}
    try:
        raw = json.loads(THEMES_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}
    out = {}
    if isinstance(raw, dict):
        for name, theme in raw.items():
            if isinstance(name, str) and isinstance(theme, dict) \
                    and name not in BUILTIN_THEMES:
                out[name] = _complete_theme(theme)
    return out


def save_themes(themes):
    THEMES_PATH.write_text(
        json.dumps(themes, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")


def resolve_theme(name):
    """Theme name (or 'auto') -> complete theme dict. 'auto' follows Windows;
    unknown names fall back the same way."""
    if name in BUILTIN_THEMES:
        return _complete_theme(BUILTIN_THEMES[name], base=name)
    user = load_themes()
    if name in user:
        return user[name]
    # 'auto' or unknown: follow the Windows color scheme
    dark = False
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
        dark = (QGuiApplication.styleHints().colorScheme()
                == Qt.ColorScheme.Dark)
    except Exception:
        pass
    key = "Dark" if dark else "Light"
    return _complete_theme(BUILTIN_THEMES[key], base=key)

DEFAULT_SETTINGS = {
    # appearance
    "theme": "auto",              # 'auto' = follow Windows, 'dark', 'light'
    # writing standard. strip_* = the app manages/removes these foreign tags;
    # "keep the tag on the file" is expressed by DISABLING the id3v1 / apev2
    # problem type (Settings - Problem types), not by a separate toggle.
    "strip_id3v1": True,          # remove old ID3v1 tag when the rule is enabled
    "strip_apev2": True,          # remove foreign APEv2 tag when the rule is enabled
    # delay removing a foreign tag until every field where it DISAGREES with
    # ID3v2 has been decided (use the old value, or keep ID3v2). Off = remove
    # even while differences are undecided (ID3v2 wins for anything not chosen).
    "id3v1_delay_on_conflict": True,
    "apev2_delay_on_conflict": True,
    "utf8_all_frames": True,      # re-encode all text frames as UTF-8 on write
    "preserve_file_times": True,  # keep 'date modified' when only tags change
    # track numbers
    "track_pad": False,           # write '03' instead of '3'
    "track_pad_total": True,      # when padding, also pad the total ('03/09')
    "track_totals": True,         # write '3/12' instead of '3'
    # album artist rule:
    #  'subset' = album artist is your choice; it must be uniform inside the
    #             album, and every track's ARTIST must contain it (artist =
    #             album artist + optionally more, e.g. guests)
    #  'common' = album artist is forced to the artists shared by all tracks
    #  'keep'   = never touch album artist
    "albumartist_mode": "subset",
    "va_name": "Various Artists", # album artist for compilations (no common artist)
    "sync_artist_albumartist": True,  # copy artist <-> albumartist when one is empty
    "check_plus_collab": True,    # '+' in album folder -> expect several artists
    # required fields (missing -> problem; fill proposed where derivable)
    "required_fields": ["title", "artist", "albumartist", "album", "track",
                        "year", "genre", "publisher"],
    # multi-value handling
    "multi_value_fields": ["artist", "albumartist", "composer", "genre",
                           "lyricist", "origartist", "conductor", "remixer"],
    "split_semicolon": True,      # 'A; B' in one value -> propose real multi-value
    "split_backslash": True,      # 'A\\B'
    "split_slash_spaced": True,   # 'A / B' (bare '/' never splits: AC/DC)
    "split_comma": False,         # ', ' (off: 'Waits, Tom' style names)
    "split_custom": "",           # extra separators, space-separated
    # value validation: field -> {'regex': ..., 'allowed': 'v1; v2'}
    "field_patterns": {
        "year": {"regex": "^\\d{4}$", "allowed": "unknown; neznámé"},
    },
    # per problem type: 'enabled' | 'postponed' | 'disabled'
    # (missing keys fall back to rules.DEFAULT_RULE_MODES, then 'enabled')
    "rule_modes": {},
    # display names of metadata fields: active set + user-defined sets
    "field_label_set": "English",
    "field_label_sets": {},
    # internet metadata (the check always gathers both kinds; these only
    # control visibility + whether Apply processes them)
    "show_online_add": True,      # proposals that FILL an empty field
    "show_online_diff": True,     # proposals that DIFFER from the current value
    # image-related problems (covers, folder.jpg, artist.jpg) visibility
    "show_image_problems": True,
    # saved regular expressions for the search page: [{'name','pattern'}]
    "saved_expressions": [],
    # remembered column widths / splitter ratios, keyed by view name
    "ui_layout": {},
    # covers
    "cover_min_px": 300,          # below this = problem (yellow)
    "cover_warn_px": 500,         # below this = worth improving (info)
    "write_folder_jpg": True,     # propose exporting folder.jpg from embedded art
    "overwrite_folder_jpg": False,
    "embed_folder_jpg": True,     # embed folder image when a track has none, and
                                  # sync embedded<->folder to the larger resolution
    # personal access token for the Discogs cover search (empty = Discogs off);
    # discogs.com > Settings > Developers > 'Generate new token'
    "discogs_token": "",
    # genre
    "genre_policy": "fill_missing",  # 'preserve' = never touch; 'fill_missing' = propose for empty
    # history
    "history_keep": 10,           # tag versions kept per track
    # display
    "multi_sep": "; ",            # how multi-value fields are shown/edited in the GUI
    # auto-update (Settings - Updates)
    "auto_update_check": True,    # check GitHub for a new version at startup
    "skipped_version": "",        # version the user chose to skip in the popup
}

_LEGACY_KEYS = {"folders_txt": "folders.txt", "mp3_root": ""}


def _legacy_values_literal(raw):
    """Read legacy flat keys without JSON unescaping (raw backslash paths)."""
    cfg = dict(_LEGACY_KEYS)
    found = False
    for key in cfg:
        m = re.search(r'"%s"\s*:\s*"([^"]*)"' % key, raw)
        if m:
            cfg[key] = m.group(1)
            found = True
    return cfg if found else None


def make_db_filename(name, libraries):
    """A unique db filename for a new library."""
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "library"
    taken = {lib.get("db") for lib in libraries}
    db = "library_%s.db" % base
    i = 2
    while db in taken or (DATA_DIR / db).exists():
        db = "library_%s_%d.db" % (base, i)
        i += 1
    return db


def _migrate_legacy(parsed):
    """Old flat config {folders_txt, mp3_root} -> one library entry."""
    root = parsed.get("mp3_root", "")
    if not root:
        return []
    name = Path(str(root).rstrip("\\/")).name or "My Library"
    return [{"name": name, "root": root,
             "folders_txt": parsed.get("folders_txt", "folders.txt"),
             "db": "library.db"}]


def load_config():
    """Return {'libraries': [...], 'active_library': name, 'settings': {...}}."""
    cfg = {"libraries": [], "active_library": "",
           "settings": dict(DEFAULT_SETTINGS)}
    if CONFIG_PATH.exists():
        raw = CONFIG_PATH.read_text(encoding="utf-8-sig")
        parsed = None
        try:
            parsed = json.loads(raw)
            legacy_vals = [parsed.get(k, "") for k in _LEGACY_KEYS]
            if any(c in str(v) for v in legacy_vals for c in "\t\n\r\b\f"):
                parsed = None   # "\\nas\tracks" parsed 'successfully' into garbage
        except json.JSONDecodeError:
            parsed = None
        if parsed is None:
            parsed = _legacy_values_literal(raw)
            if parsed is None:
                print("config.json could not be read - fix it or delete it.")
                sys.exit(1)
        if isinstance(parsed.get("libraries"), list) and parsed["libraries"]:
            cfg["libraries"] = [
                {"name": lib.get("name", "Library"),
                 "root": lib.get("root", ""),
                 "folders_txt": lib.get("folders_txt", "folders.txt"),
                 "db": lib.get("db", "library.db")}
                for lib in parsed["libraries"]]
            cfg["active_library"] = parsed.get("active_library", "")
        else:
            cfg["libraries"] = _migrate_legacy(parsed)
        saved = parsed.get("settings", {})
        for k in DEFAULT_SETTINGS:
            if k in saved:
                cfg["settings"][k] = saved[k]
    # migrations of older stored values
    if cfg["settings"].get("albumartist_mode") == "common":
        cfg["settings"]["albumartist_mode"] = "subset"   # old default, new rule
    theme_migrate = {"light": "Light", "dark": "Dark"}
    cfg["settings"]["theme"] = theme_migrate.get(
        cfg["settings"].get("theme", "auto"), cfg["settings"].get("theme", "auto"))
    cfg["settings"]["saved_expressions"] = [
        e if isinstance(e, dict) else {"name": e, "pattern": e}
        for e in cfg["settings"].get("saved_expressions", [])]
    # zero libraries is a valid state (fresh install / all deleted)
    names = [lib["name"] for lib in cfg["libraries"]]
    if cfg["active_library"] not in names:
        cfg["active_library"] = names[0] if names else ""
    return cfg


def save_config(cfg):
    out = {
        "libraries": cfg.get("libraries", []),
        "active_library": cfg.get("active_library", ""),
        "settings": {k: cfg.get("settings", {}).get(k, DEFAULT_SETTINGS[k])
                     for k in DEFAULT_SETTINGS},
    }
    CONFIG_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")


def active_library(cfg):
    for lib in cfg["libraries"]:
        if lib["name"] == cfg["active_library"]:
            return lib
    return cfg["libraries"][0] if cfg["libraries"] else None


def lib_db_path(lib):
    p = Path(lib.get("db", "library.db"))
    return p if p.is_absolute() else DATA_DIR / p


def read_folders_txt(path: Path):
    """One folder name per line, trailing slash/backslash tolerated."""
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

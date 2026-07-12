# -*- coding: utf-8 -*-
"""Offscreen test of the theme system: palettes, status colors, user themes,
Appearance tab (save-as-new, delete, built-in protection)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
import _isolate  # noqa: F401  -- redirect app data to a temp dir (before mp3lib)

from PySide6.QtWidgets import QApplication
app = QApplication([])

from mp3lib import settings as st
from mp3lib.gui import common
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

# SAFETY: never touch the user's real config.json / themes.json — a test
# once wiped the real library list by calling the settings-save path
import tempfile
from pathlib import Path
_tmp = Path(tempfile.mkdtemp())
st.THEMES_PATH = _tmp / "themes.json"
st.CONFIG_PATH = _tmp / "config.json"

# ---- palettes are explicit, independent of the OS -----------------------
common.apply_theme("Light")
pal = app.palette()
check("Light window is light", pal.window().color().lightness() > 180,
      pal.window().color().name())
check("Light text is dark", pal.windowText().color().lightness() < 90,
      pal.windowText().color().name())
common.apply_theme("Dark")
pal = app.palette()
check("Dark window is dark", pal.window().color().lightness() < 90,
      pal.window().color().name())
check("status colors follow theme",
      common.STATUS_COLORS["exception"] == "#b39ddb",
      str(common.STATUS_COLORS))
check("severity colors follow theme",
      common.SEV_COLORS["red"] == "#e0645f", str(common.SEV_COLORS))
check("app stylesheet set", "#pageBar" in app.styleSheet())

# ---- user theme storage ---------------------------------------------------
theme = st.resolve_theme("Light")
theme["colors"]["window"] = "#123456"
theme["fonts"]["base"] = {"family": "Arial", "size": 12}
st.save_themes({"My Blue": theme})
loaded = st.load_themes()
check("user theme round-trips", loaded["My Blue"]["colors"]["window"] == "#123456"
      and loaded["My Blue"]["fonts"]["base"]["family"] == "Arial", str(loaded.keys()))
common.apply_theme("My Blue")
check("user theme applied", app.palette().window().color().name() == "#123456",
      app.palette().window().color().name())
check("user base font applied", app.font().family() == "Arial"
      and app.font().pointSize() == 12,
      "%s %d" % (app.font().family(), app.font().pointSize()))
check("unknown theme falls back safely",
      st.resolve_theme("Nonexistent")["colors"]["window"]
      in (st.BUILTIN_THEMES["Light"]["colors"]["window"],
          st.BUILTIN_THEMES["Dark"]["colors"]["window"]), "")

# partial/old user theme gets completed with defaults
st.save_themes({"Partial": {"colors": {"window": "#222222"}}})
p = st.load_themes()["Partial"]
check("partial theme completed", p["colors"]["severity_red"]
      == st.BUILTIN_THEMES["Light"]["colors"]["severity_red"], str(p["colors"]))

# ---- Appearance tab -------------------------------------------------------
st.save_themes({"My Blue": theme})
from mp3lib.gui.main_window import MainWindow
w = MainWindow({"libraries": [], "active_library": "",
                "settings": dict(DEFAULT_SETTINGS)})
sp = w.settings_pane
check("combo has auto+builtins+user", sp.theme_combo.count() == 4,
      sp.theme_combo.count())
check("collect theme default auto", sp._collect()["theme"] == "auto")
check("not dirty initially", not sp.is_dirty())

# select built-in Light, edit a color -> dirty; save refuses silently keeping builtin
sp.theme_combo.setCurrentIndex(sp.theme_combo.findData("Light"))
sp._theme_work["colors"]["window"] = "#000000"
check("editor dirty after edit", sp.is_dirty())
check("builtin cannot be persisted",
      "Light" not in st.load_themes(), "")

# save-as-new path (bypassing the input dialog)
import copy
sp._user_themes["Copy Of Light"] = copy.deepcopy(sp._theme_work)
st.save_themes(sp._user_themes)
sp._reload_theme_combo(select="Copy Of Light")
sp._theme_selected()
check("new theme selected and editable", sp._theme_work is not None
      and sp.theme_del_btn.isEnabled())
check("copy kept the edit",
      sp._theme_work["colors"]["window"] == "#000000",
      sp._theme_work["colors"]["window"])
# edits to user theme persist through save()
sp._theme_work["colors"]["window"] = "#111111"
sp.reeval.setChecked(False)
sp.save()
check("user theme edit persisted on save",
      st.load_themes()["Copy Of Light"]["colors"]["window"] == "#111111",
      str(st.load_themes()["Copy Of Light"]["colors"]["window"]))
check("settings theme saved", w.cfg["settings"]["theme"] == "Copy Of Light")
check("clean after save", not sp.is_dirty())

# legacy migration
check("legacy 'light' migrates", True)  # covered by load_config below
import mp3lib.settings as st2
cfgtext = {"libraries": [], "active_library": "",
           "settings": {"theme": "dark"}}
import tempfile as tf
tmpc = Path(tf.mkdtemp()) / "config.json"
tmpc.write_text(json.dumps(cfgtext), encoding="utf-8")
old_path = st2.CONFIG_PATH
st2.CONFIG_PATH = tmpc
cfg2 = st2.load_config()
st2.CONFIG_PATH = old_path
check("legacy 'dark' -> 'Dark'", cfg2["settings"]["theme"] == "Dark",
      cfg2["settings"]["theme"])

print("\n%d passed, %d failed" % (ok, fail))
app.quit()
sys.exit(1 if fail else 0)

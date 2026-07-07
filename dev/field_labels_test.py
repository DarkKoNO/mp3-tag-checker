# -*- coding: utf-8 -*-
"""Tests for the field-name alias sets and their application in the GUI."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from PySide6.QtWidgets import QApplication
app = QApplication([])

from pathlib import Path

from mp3lib import settings as st

# SAFETY: never touch the user's real config.json / themes.json
_tmp = Path(tempfile.mkdtemp())
st.CONFIG_PATH = _tmp / "config.json"
st.THEMES_PATH = _tmp / "themes.json"

from mp3lib.gui import common
from mp3lib.settings import DEFAULT_SETTINGS, FIELD_LABEL_SETS

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("PASS %s" % name)
    else:
        fail += 1
        print("FAIL %s  %s" % (name, extra))

# ---- resolution -------------------------------------------------------------
s = dict(DEFAULT_SETTINGS)
common.apply_field_labels(s)
check("default English labels", common.field_label("albumartist") == "Album Artist",
      common.field_label("albumartist"))
s["field_label_set"] = "Czech"
common.apply_field_labels(s)
check("Czech labels", common.field_label("albumartist") == "Interpret alba"
      and common.field_label("genre") == "Žánr", common.field_label("genre"))
s["field_label_set"] = "Technical (tag names)"
common.apply_field_labels(s)
check("technical fallback", common.field_label("albumartist") == "albumartist")
# user set with partial overrides falls back to English
s["field_label_set"] = "Mine"
s["field_label_sets"] = {"Mine": {"albumartist": "AA!"}}
common.apply_field_labels(s)
check("user set override", common.field_label("albumartist") == "AA!")
check("user set fallback to English", common.field_label("title") == "Title")

# every builtin set covers every editable field (except Technical, which is empty)
from mp3lib import tagio
for name in ("English", "Czech"):
    missing = [f for f in tagio.EDITABLE_FIELDS if f not in FIELD_LABEL_SETS[name]]
    check("set '%s' complete" % name, not missing, str(missing))

# ---- settings tab -----------------------------------------------------------
s2 = dict(DEFAULT_SETTINGS)
common.apply_field_labels(s2)
from mp3lib.gui.main_window import MainWindow
w = MainWindow({"libraries": [], "active_library": "", "settings": s2})
sp = w.settings_pane
check("field-name combo present", sp.flabel_combo.count() == 3,
      sp.flabel_combo.count())
check("collect default set", sp._collect()["field_label_set"] == "English")
check("not dirty initially", not sp.is_dirty())
# edit a builtin -> dirty, save refuses to persist builtin
sp.flabel_edits["albumartist"].setText("Album Artist X")
sp.flabel_edits["albumartist"].textEdited.emit("Album Artist X")
check("dirty after edit", sp.is_dirty())
# save-as-new (bypassing input dialog)
import copy
sp._flabel_user["My Names"] = copy.deepcopy(sp._flabels_work)
sp._reload_flabel_combo(select="My Names")
sp._flabels_selected()
check("new set editable + deletable", sp.flabel_del_btn.isEnabled())
check("new set kept the edit",
      sp._flabels_work.get("albumartist") == "Album Artist X",
      str(sp._flabels_work.get("albumartist")))
sp.reeval.setChecked(False)
sp.save()
check("set persisted in settings",
      w.cfg["settings"]["field_label_sets"]["My Names"]["albumartist"]
      == "Album Artist X", "")
check("active set saved", w.cfg["settings"]["field_label_set"] == "My Names")
check("labels applied live", common.field_label("albumartist") == "Album Artist X",
      common.field_label("albumartist"))
check("clean after save", not sp.is_dirty())

# theme spec: new page_text key present and attention is the accent blue
check("page_text in Light theme",
      st.BUILTIN_THEMES["Light"]["colors"]["page_text"] == "#1a1a1a")
check("attention is active-page blue (Light)",
      st.BUILTIN_THEMES["Light"]["colors"]["attention"]
      == st.BUILTIN_THEMES["Light"]["colors"]["page_active_bg"])
check("attention is active-page blue (Dark)",
      st.BUILTIN_THEMES["Dark"]["colors"]["attention"]
      == st.BUILTIN_THEMES["Dark"]["colors"]["page_active_bg"])

print("\n%d passed, %d failed" % (ok, fail))
app.quit()
sys.exit(1 if fail else 0)

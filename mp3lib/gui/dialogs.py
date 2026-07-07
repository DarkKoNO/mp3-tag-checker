"""Dialogs: history/revert, settings, changelog, online cover, artist image."""

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QRadioButton, QScrollArea,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import applier, db, online
from ..settings import (BASE_DIR, DEFAULT_SETTINGS, make_db_filename,
                        read_folders_txt, save_config)
from .common import (STATUS_COLORS, enable_copy, field_label, join_vals,
                     persist_header)

# pseudo "fields" appearing in the changelog / proposals
PSEUDO_FIELD_LABELS = {"_id3v1": "old ID3v1 tag", "_version": "ID3 version",
                       "_encoding": "text encoding", "folder_jpg": "folder.jpg",
                       "artist_jpg": "artist.jpg", "cover": "album cover"}

HIST_FIELDS = ["title", "artist", "albumartist", "album", "track", "year", "genre"]


class ScanDialog(QDialog):
    """Choose what to scan in the active library: the current selection,
    everything, a folder list file, or manually picked folders."""

    def __init__(self, lib, selected=None, parent=None):
        super().__init__(parent)
        self.lib = lib
        self.selected = sorted(selected or [])
        self.entries = []
        self.folders_txt_used = None
        self.setWindowTitle("Start check — %s" % lib["name"])
        self.resize(560, 600)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Root: %s" % lib["root"]))

        self.r_sel = QRadioButton(
            "Selected in the library (%d artist folder(s))" % len(self.selected))
        self.r_all = QRadioButton("All folders in the root")
        self.r_txt = QRadioButton("Folders listed in a text file (folders.txt)")
        self.r_pick = QRadioButton("Pick folders manually")
        if self.selected:
            lay.addWidget(self.r_sel)
        lay.addWidget(self.r_all)
        lay.addWidget(self.r_txt)

        txt_row = QHBoxLayout()
        txt_row.addSpacing(24)
        self.txt_path = QLineEdit(lib.get("folders_txt", "folders.txt"))
        txt_row.addWidget(self.txt_path)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_txt)
        txt_row.addWidget(browse)
        lay.addLayout(txt_row)

        lay.addWidget(self.r_pick)
        pick_btns = QHBoxLayout()
        pick_btns.addSpacing(24)
        self.all_btn = QPushButton("Select all")
        self.none_btn = QPushButton("Select none")
        self.count_lbl = QLabel("")
        pick_btns.addWidget(self.all_btn)
        pick_btns.addWidget(self.none_btn)
        pick_btns.addWidget(self.count_lbl)
        pick_btns.addStretch(1)
        lay.addLayout(pick_btns)
        self.folder_list = QListWidget()
        lay.addWidget(self.folder_list, 1)

        self.full_cb = QCheckBox("Re-read all files (full rescan, slower - use after"
                                 " app updates that track new fields)")
        lay.addWidget(self.full_cb)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Start check")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        # default: scan the selection if there is one, otherwise everything
        if self.selected:
            self.r_sel.setChecked(True)
        else:
            self.r_all.setChecked(True)
        self._folders_loaded = False
        self.r_pick.toggled.connect(self._maybe_load_folders)
        self.all_btn.clicked.connect(lambda: self._set_all(Qt.Checked))
        self.none_btn.clicked.connect(lambda: self._set_all(Qt.Unchecked))

    def _browse_txt(self):
        start = self.txt_path.text() or str(BASE_DIR)
        fp, _ = QFileDialog.getOpenFileName(self, "Folder list file", start,
                                            "Text files (*.txt);;All files (*)")
        if fp:
            self.txt_path.setText(fp)
            self.r_txt.setChecked(True)

    def _maybe_load_folders(self, checked):
        if not checked or self._folders_loaded:
            return
        self._folders_loaded = True
        self.folder_list.clear()
        try:
            dirs = sorted((d.name for d in Path(self.lib["root"]).iterdir()
                           if d.is_dir()), key=str.lower)
        except OSError as e:
            QMessageBox.warning(self, "Cannot list root", str(e))
            return
        for name in dirs:
            it = QListWidgetItem(name)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            self.folder_list.addItem(it)
        self.count_lbl.setText("%d folders" % len(dirs))

    def _set_all(self, state):
        self.r_pick.setChecked(True)
        for i in range(self.folder_list.count()):
            self.folder_list.item(i).setCheckState(state)

    def accept(self):
        if self.selected and self.r_sel.isChecked():
            self.entries = self.selected
            super().accept()
            return
        if self.r_all.isChecked():
            try:
                self.entries = sorted((d.name for d in Path(self.lib["root"]).iterdir()
                                       if d.is_dir()), key=str.lower)
            except OSError as e:
                QMessageBox.warning(self, "Cannot list root", str(e))
                return
        elif self.r_txt.isChecked():
            fp = Path(self.txt_path.text().strip())
            if not fp.is_absolute():
                fp = BASE_DIR / fp
            if not fp.exists():
                QMessageBox.warning(self, "Not found", "Folder list file not found:\n%s" % fp)
                return
            self.entries = read_folders_txt(fp)
            self.folders_txt_used = str(fp)
        else:
            self._maybe_load_folders(True)
            self.entries = [self.folder_list.item(i).text()
                            for i in range(self.folder_list.count())
                            if self.folder_list.item(i).checkState() == Qt.Checked]
        if not self.entries:
            QMessageBox.information(self, "Nothing to scan",
                                    "The selection contains no folders.")
            return
        super().accept()


class LibraryEditDialog(QDialog):
    """Add or edit one library (name + root + default folder list)."""

    def __init__(self, lib=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Library" if lib else "New library")
        lib = lib or {}
        form = QFormLayout(self)
        self.name = QLineEdit(lib.get("name", ""))
        form.addRow("Name:", self.name)
        root_row = QHBoxLayout()
        self.root = QLineEdit(lib.get("root", ""))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_root)
        root_row.addWidget(self.root)
        root_row.addWidget(browse)
        form.addRow("Root folder:", root_row)
        self.folders_txt = QLineEdit(lib.get("folders_txt", "folders.txt"))
        form.addRow("Default folder list:", self.folders_txt)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _browse_root(self):
        d = QFileDialog.getExistingDirectory(self, "Library root folder",
                                             self.root.text() or "")
        if d:
            self.root.setText(d)
            if not self.name.text().strip():
                self.name.setText(Path(d).name)

    def accept(self):
        if not self.name.text().strip():
            QMessageBox.warning(self, "Missing name", "Give the library a name.")
            return
        if not self.root.text().strip():
            QMessageBox.warning(self, "Missing root", "Choose the root folder.")
            return
        super().accept()


class LibrariesDialog(QDialog):
    """Manage the list of libraries. Edits cfg['libraries'] in place."""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.changed = False
        self.added_name = None      # set when a new library gets created
        self.setWindowTitle("Libraries")
        self.resize(760, 380)
        lay = QVBoxLayout(self)
        self.table = QTableWidget()
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.table)
        btns = QHBoxLayout()
        for label, slot in (("Add…", self._add), ("Edit…", self._edit),
                            ("Remove", self._remove)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        lay.addLayout(btns)
        self._reload()

    def _reload(self):
        libs = self.cfg["libraries"]
        self.table.clear()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Name", "Root folder", "Database"])
        self.table.setRowCount(len(libs))
        for r, lib in enumerate(libs):
            self.table.setItem(r, 0, QTableWidgetItem(lib["name"]))
            self.table.setItem(r, 1, QTableWidgetItem(lib["root"]))
            self.table.setItem(r, 2, QTableWidgetItem(lib["db"]))
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.resizeColumnsToContents()

    def _add(self):
        dlg = LibraryEditDialog(parent=self)
        if not dlg.exec():
            return
        name = dlg.name.text().strip()
        if any(lib["name"] == name for lib in self.cfg["libraries"]):
            QMessageBox.warning(self, "Duplicate", "A library with this name exists.")
            return
        self.cfg["libraries"].append({
            "name": name, "root": dlg.root.text().strip(),
            "folders_txt": dlg.folders_txt.text().strip() or "folders.txt",
            "db": make_db_filename(name, self.cfg["libraries"])})
        self.changed = True
        self.added_name = name
        save_config(self.cfg)
        self._reload()

    def _edit(self):
        r = self.table.currentRow()
        if r < 0:
            return
        lib = self.cfg["libraries"][r]
        dlg = LibraryEditDialog(lib, parent=self)
        if not dlg.exec():
            return
        new_name = dlg.name.text().strip()
        if new_name != lib["name"] and any(
                x["name"] == new_name for x in self.cfg["libraries"]):
            QMessageBox.warning(self, "Duplicate", "A library with this name exists.")
            return
        if self.cfg["active_library"] == lib["name"]:
            self.cfg["active_library"] = new_name
        lib["name"] = new_name
        lib["root"] = dlg.root.text().strip()
        lib["folders_txt"] = dlg.folders_txt.text().strip() or "folders.txt"
        self.changed = True
        save_config(self.cfg)
        self._reload()

    def _remove(self):
        r = self.table.currentRow()
        if r < 0:
            return
        lib = self.cfg["libraries"][r]
        if QMessageBox.question(
                self, "Remove library",
                "Remove library '%s' from the list?\n\nIts database file (%s) is"
                " kept on disk, so adding it back later restores everything."
                % (lib["name"], lib["db"])) != QMessageBox.Yes:
            return
        del self.cfg["libraries"][r]
        if self.cfg["active_library"] == lib["name"]:
            self.cfg["active_library"] = (self.cfg["libraries"][0]["name"]
                                          if self.cfg["libraries"] else "")
        self.changed = True
        save_config(self.cfg)
        self._reload()


class HistoryDialog(QDialog):
    """Browse tag versions of an album; revert to any of them."""

    def __init__(self, con, settings, album_dir, parent=None):
        super().__init__(parent)
        self.con, self.settings, self.album_dir = con, settings, album_dir
        self.reverted = False
        self.setWindowTitle("History — %s" % Path(album_dir).name)
        self.resize(1000, 560)

        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split)

        self.batch_list = QListWidget()
        split.addWidget(self.batch_list)
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        enable_copy(self.table)
        split.addWidget(self.table)
        split.setSizes([260, 740])

        btns = QHBoxLayout()
        self.revert_btn = QPushButton("Revert album to selected version")
        self.revert_btn.clicked.connect(self._revert)
        btns.addWidget(self.revert_btn)
        btns.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        lay.addLayout(btns)

        self.batches = applier.album_history(con, album_dir)
        for i, b in enumerate(self.batches):
            label = "%s  —  %s  (%d tracks)" % (b["when"], b["kind"], b["n_tracks"])
            if i == 0:
                label += "   [current]"
            self.batch_list.addItem(QListWidgetItem(label))
        self.batch_list.currentRowChanged.connect(self._show_batch)
        if self.batches:
            self.batch_list.setCurrentRow(0)

    def _show_batch(self, row):
        if row < 0:
            return
        state = applier.album_state_at(self.con, self.album_dir,
                                       self.batches[row]["batch_id"])
        self.table.clear()
        self.table.setColumnCount(1 + len(HIST_FIELDS))
        self.table.setHorizontalHeaderLabels(
            ["File"] + [field_label(f) for f in HIST_FIELDS])
        self.table.setRowCount(len(state))
        for r, (tid, info) in enumerate(sorted(state.items(), key=lambda kv: kv[1]["file"])):
            self.table.setItem(r, 0, QTableWidgetItem(info["file"]))
            for c, f in enumerate(HIST_FIELDS, start=1):
                self.table.setItem(r, c, QTableWidgetItem(join_vals(info["tags"].get(f))))
        self.table.resizeColumnsToContents()
        self.revert_btn.setEnabled(row != 0)

    def _revert(self):
        row = self.batch_list.currentRow()
        if row <= 0:
            return
        b = self.batches[row]
        if QMessageBox.question(
                self, "Revert album",
                "Rewrite all tags of this album back to their state of\n%s (%s)?"
                % (b["when"], b["kind"])) != QMessageBox.Yes:
            return
        res = applier.revert_album(self.con, self.settings, self.album_dir,
                                   b["batch_id"])
        if res["errors"]:
            QMessageBox.warning(self, "Revert finished with errors",
                                "\n".join("%s: %s" % e for e in res["errors"][:10]))
        else:
            QMessageBox.information(self, "Reverted",
                                    "%d files rewritten (%d field changes)."
                                    % (res["files"], res["changes"]))
        self.reverted = True
        self.accept()


class SettingsPane(QWidget):
    """Full-page settings editor with topic tabs. owner = MainWindow."""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QGridLayout, QTabWidget

        from .. import tagio

        self.owner = owner
        self.cfg = owner.cfg
        s = self.cfg["settings"]
        self.w = {}
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        def chk(form, key, label):
            c = QCheckBox(label)
            c.setChecked(bool(s[key]))
            self.w[key] = c
            form.addRow(c)

        # --- Appearance tab (themes: every color + font, user-editable)
        self._build_appearance_tab(tabs, s)

        # --- Field names tab (display aliases for the metadata fields)
        self._build_field_names_tab(tabs, s)

        # --- Writing tab (general write behavior; rule-specific options live
        # in the Options column of the Problem types tab)
        wtab = QWidget()
        form = QFormLayout(wtab)
        chk(form, "preserve_file_times", "Keep file 'date modified' when changing tags")
        self.w["va_name"] = QLineEdit(s["va_name"])
        form.addRow("Album artist for compilations:", self.w["va_name"])
        sp = QSpinBox()
        sp.setRange(2, 100)
        sp.setValue(int(s["history_keep"]))
        self.w["history_keep"] = sp
        form.addRow("Tag versions kept per file:", sp)
        form.addRow(QLabel(
            "<i>Rule-specific options — ID3v1 removal, UTF-8 re-encoding,"
            " track number format, the album artist rule and artist ↔ album"
            " artist copying — are set per rule in the <b>Problem types</b>"
            " tab (Options column).</i>"))
        tabs.addTab(wtab, "Writing")

        # --- Checks tab (required fields, covers, genre)
        ctab = QWidget()
        cform = QFormLayout(ctab)
        cform.addRow(QLabel("<b>Required fields</b> (missing = problem; the app"
                            " proposes a value where it can derive one):"))
        req_grid = QGridLayout()
        self.req_boxes = {}
        for i, field in enumerate(tagio.EDITABLE_FIELDS):
            c = QCheckBox(field_label(field))
            c.setToolTip("Technical tag name: %s" % field)
            c.setChecked(field in s.get("required_fields", []))
            self.req_boxes[field] = c
            req_grid.addWidget(c, i // 4, i % 4)
        cform.addRow(req_grid)
        self.w["genre_policy"] = QComboBox()
        self.w["genre_policy"].addItems(["fill_missing", "preserve"])
        self.w["genre_policy"].setCurrentText(s["genre_policy"])
        cform.addRow("Genre policy:", self.w["genre_policy"])
        for key, label in (("cover_min_px", "Cover is a problem below (px):"),
                           ("cover_warn_px", "Cover worth improving below (px):")):
            spx = QSpinBox()
            spx.setRange(50, 5000)
            spx.setValue(int(s[key]))
            self.w[key] = spx
            cform.addRow(label, spx)
        chk(cform, "write_folder_jpg", "Propose folder.jpg where missing")
        chk(cform, "overwrite_folder_jpg", "Overwrite existing folder.jpg")
        chk(cform, "check_plus_collab",
            "Warn when the album folder name contains '+' (collaboration) but"
            " artist / album artist holds only one value")
        tabs.addTab(ctab, "Checks")

        # --- Multi-value tab
        mtab = QWidget()
        mform = QFormLayout(mtab)
        self.w["multi_sep"] = QComboBox()
        self.w["multi_sep"].setEditable(True)
        self.w["multi_sep"].addItems(["\\\\", "; ", " / ", ", "])
        self.w["multi_sep"].setCurrentText(s["multi_sep"])
        mform.addRow("Separator shown between multiple values (files always use"
                     " the ID3v2.4 standard):", self.w["multi_sep"])
        mform.addRow(QLabel("<b>Fields that may hold multiple values</b>"
                            " (split proposals only apply to these):"))
        mv_grid = QGridLayout()
        self.mv_boxes = {}
        for i, field in enumerate(tagio.EDITABLE_FIELDS):
            c = QCheckBox(field_label(field))
            c.setToolTip("Technical tag name: %s" % field)
            c.setChecked(field in s.get("multi_value_fields", []))
            self.mv_boxes[field] = c
            mv_grid.addWidget(c, i // 4, i % 4)
        mform.addRow(mv_grid)
        mform.addRow(QLabel("<b>Separators that indicate combined values:</b>"))
        chk(mform, "split_semicolon", "Semicolon  ('A; B')")
        chk(mform, "split_backslash", "Backslash  ('A\\B')")
        chk(mform, "split_slash_spaced", "Slash with spaces  ('A / B') — bare"
                                         " slash never splits (AC/DC)")
        chk(mform, "split_comma", "Comma  ('A, B') — careful with 'Waits, Tom'")
        self.w["split_custom"] = QLineEdit(s.get("split_custom", ""))
        mform.addRow("Extra separators (space-separated):", self.w["split_custom"])
        tabs.addTab(mtab, "Multi-value")

        # --- Validation tab (regex + allowed values per field)
        vtab = QWidget()
        vlay = QVBoxLayout(vtab)
        vlay.addWidget(QLabel(
            "A value is OK when it matches the regular expression OR equals one"
            " of the allowed values (separated by ';').  Example: year"
            "  ^\\d{4}$  +  unknown; neznámé"
            "  (the Field column uses the technical tag names)"))
        self.pat_table = QTableWidget(0, 3)
        self.pat_table.setHorizontalHeaderLabels(
            ["Field", "Regular expression", "Allowed values (';' separated)"])
        hdr = self.pat_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(True)   # drag the middle dividers; last fills
        self.pat_table.setColumnWidth(0, 130)
        self.pat_table.setColumnWidth(1, 380)
        for field, pat in s.get("field_patterns", {}).items():
            self._add_pattern_row(field, pat.get("regex", ""), pat.get("allowed", ""))
        vlay.addWidget(self.pat_table)
        prow = QHBoxLayout()
        addb = QPushButton("Add row")
        addb.clicked.connect(lambda: self._add_pattern_row("", "", ""))
        delb = QPushButton("Remove selected row")
        delb.clicked.connect(lambda: self.pat_table.removeRow(
            self.pat_table.currentRow()))
        prow.addWidget(addb)
        prow.addWidget(delb)
        prow.addStretch(1)
        vlay.addLayout(prow)
        tabs.addTab(vtab, "Validation")

        # --- Problem types tab: per-rule enabled / postponed / disabled
        from ..rules import (CONFIGURABLE_RULES, rule_description, rule_label,
                             rule_mode)
        rtab = QWidget()
        rlay = QVBoxLayout(rtab)
        rhead = QLabel(
            "How each type of problem/change is handled — hover a name for a"
            " full explanation of what it means and what applying it does."
            "<br><b>Enabled</b> = detected, shown and applied normally."
            " &nbsp;<b>Postponed</b> = detected and shown, but skipped when"
            " applying until you 'Restore' a row yourself."
            " &nbsp;<b>Disabled</b> = not detected and not shown anywhere."
            "<br>The <b>Options</b> column holds the rule's own settings"
            " (e.g. the exact track number format)."
            "<br><i>Some types also have their own switches on the other tabs"
            " (e.g. required fields, folder.jpg) — both must allow them.</i>")
        rhead.setWordWrap(True)
        rlay.addWidget(rhead)
        from PySide6.QtWidgets import QButtonGroup
        from PySide6.QtWidgets import QGridLayout as _QGrid
        rules_inner = QWidget()
        rgrid = _QGrid(rules_inner)
        rgrid.setHorizontalSpacing(18)
        mode_keys = ["enabled", "postponed", "disabled"]
        mode_heads = [
            ("Enabled", "Detected, shown and applied normally"),
            ("Postponed", "Detected and shown, but skipped when applying"
                          " until you 'Restore' a row yourself"),
            ("Disabled", "Not detected and not shown anywhere"),
        ]
        rgrid.addWidget(QLabel("<b>Problem / change type</b>"), 0, 0)
        for c, (name, tip) in enumerate(mode_heads, start=1):
            h = QLabel("<b>%s</b>" % name)
            h.setToolTip(tip)
            h.setAlignment(Qt.AlignHCenter)
            rgrid.addWidget(h, 0, c)
        rgrid.addWidget(QLabel("<b>Options</b>"), 0, 4)

        # ---- per-rule options (shown in the Options column)
        def _opt_combo(items, current, tooltip):
            cb = QComboBox()
            for label, data in items:
                cb.addItem(label, data)
            for i in range(cb.count()):
                if cb.itemData(i) == current:
                    cb.setCurrentIndex(i)
                    break
            cb.setToolTip(tooltip)
            return cb

        self.rule_opts = {}
        tf = QComboBox()
        # (track_pad, track_totals, track_pad_total) for e.g. track 3 of 9
        for label, combo in (
                ("3   — plain number", (False, False, True)),
                ("03   — zero-padded", (True, False, True)),
                ("3/9   — with total", (False, True, True)),
                ("03/09   — zero-padded, padded total", (True, True, True)),
                ("03/9   — zero-padded, plain total", (True, True, False))):
            tf.addItem(label, combo)
        cur_fmt = (bool(s["track_pad"]), bool(s["track_totals"]),
                   bool(s.get("track_pad_total", True)))
        for i in range(tf.count()):
            pad, totals, padtot = tf.itemData(i)
            if ((pad, totals) == cur_fmt[:2]
                    and (not (pad and totals) or padtot == cur_fmt[2])):
                tf.setCurrentIndex(i)
                break
        tf.setToolTip("How track numbers are written (example: track 3 of 9)")
        self.rule_opts["track_format"] = tf
        self.rule_opts["albumartist"] = _opt_combo(
            [("subset — album artist is your choice", "subset"),
             ("common — derived from the track artists", "common"),
             ("keep — never touch the album artist", "keep")],
            s["albumartist_mode"],
            "subset — the album artist is YOUR choice; the app only makes"
            " sure it is uniform inside the album and that every track's"
            " ARTIST contains it (artist = album artist + optionally more,"
            " e.g. guests).\n"
            "common — the artists that appear on every track become the album"
            " artist; with no common artist the compilation name (Writing"
            " tab) is used.\n"
            "keep — never touch the album artist.")
        self.rule_opts["id3v1"] = _opt_combo(
            [("remove the old tag when writing", True),
             ("keep the old tag in the file", False)],
            bool(s["strip_id3v1"]),
            "Whether writing a file removes its leftover ID3v1 tag (only ever"
            " done after the content check passes)")
        self.rule_opts["encoding"] = _opt_combo(
            [("re-encode all text as UTF-8 when writing", True),
             ("keep existing encodings", False)],
            bool(s["utf8_all_frames"]),
            "Whether writing a file re-encodes all its text frames in UTF-8,"
            " the ID3v2.4 standard")
        self.rule_opts["artist_sync"] = _opt_combo(
            [("copy when the other one is empty", True),
             ("never copy automatically", False)],
            bool(s["sync_artist_albumartist"]),
            "Whether an empty artist is filled from the album artist and"
            " vice versa")

        self.rule_radios = {}
        for i, rule in enumerate(CONFIGURABLE_RULES, start=1):
            lbl = QLabel(rule_label(rule))
            lbl.setToolTip(rule_description(rule))
            rgrid.addWidget(lbl, i, 0)
            group = QButtonGroup(rules_inner)
            radios = []
            cur = rule_mode(s, rule)
            for c, key in enumerate(mode_keys, start=1):
                rb = QRadioButton()
                rb.setToolTip(rule_description(rule))
                rb.setChecked(key == cur)
                group.addButton(rb)
                radios.append(rb)
                cell = QWidget()
                cl = QHBoxLayout(cell)
                cl.setContentsMargins(0, 0, 0, 0)
                cl.setAlignment(Qt.AlignHCenter)
                cl.addWidget(rb)
                rgrid.addWidget(cell, i, c)
            self.rule_radios[rule] = radios
            opt = self.rule_opts.get(rule)
            if opt is not None:
                rgrid.addWidget(opt, i, 4)
        rgrid.setColumnStretch(5, 1)
        rscroll = QScrollArea()
        rscroll.setWidget(rules_inner)
        rscroll.setWidgetResizable(True)
        rlay.addWidget(rscroll, 1)
        tabs.addTab(rtab, "Problem types")

        bottom = QHBoxLayout()
        self.reeval = QCheckBox("Re-evaluate all rules after saving")
        self.reeval.setChecked(True)
        bottom.addWidget(self.reeval)
        reset_btn = QPushButton("Reset column widths && window ratios")
        reset_btn.setToolTip("Forget all remembered table column widths and"
                             " splitter positions everywhere")
        reset_btn.clicked.connect(self._reset_layout)
        bottom.addWidget(reset_btn)
        bottom.addStretch(1)
        save_btn = QPushButton("Save settings")
        save_btn.clicked.connect(self.save)
        bottom.addWidget(save_btn)
        lay.addLayout(bottom)
        self.mark_clean()

    # ------------------------------------------------- appearance / themes ---

    def _build_appearance_tab(self, tabs, s):
        from PySide6.QtWidgets import QFontComboBox, QGridLayout
        from ..settings import THEME_COLOR_SPEC, THEME_FONT_SPEC, load_themes
        self._user_themes = load_themes()
        self._theme_work = None      # editable copy of the selected theme
        self._theme_source = None    # snapshot for dirty detection

        aptab = QWidget()
        aplay = QVBoxLayout(aptab)
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Theme:"))
        self.theme_combo = QComboBox()
        self.theme_combo.setMinimumWidth(220)
        sel_row.addWidget(self.theme_combo)
        dup_btn = QPushButton("Save as new theme…")
        dup_btn.setToolTip("Duplicate the shown theme (including your edits)"
                           " under a new name — the only way to keep changes"
                           " to the built-in Light / Dark themes")
        dup_btn.clicked.connect(self._theme_save_as)
        sel_row.addWidget(dup_btn)
        self.theme_del_btn = QPushButton("Delete theme")
        self.theme_del_btn.setToolTip("Delete this user theme (built-in themes"
                                      " cannot be deleted)")
        self.theme_del_btn.clicked.connect(self._theme_delete)
        sel_row.addWidget(self.theme_del_btn)
        sel_row.addStretch(1)
        aplay.addLayout(sel_row)
        self.theme_note = QLabel("")
        self.theme_note.setWordWrap(True)
        aplay.addWidget(self.theme_note)

        ed_inner = QWidget()
        ed_lay = QVBoxLayout(ed_inner)
        self._theme_editor_widgets = []
        self.color_btns = {}
        for cat, entries in THEME_COLOR_SPEC:
            ed_lay.addWidget(QLabel("<b>%s</b>" % cat))
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            for i, (key, label) in enumerate(entries):
                r, c = i // 2, (i % 2) * 2
                grid.addWidget(QLabel(label), r, c)
                b = QPushButton()
                b.setFixedWidth(110)
                b.setToolTip("Click to choose the color")
                b.clicked.connect(lambda _c, k=key: self._pick_color(k))
                self.color_btns[key] = b
                self._theme_editor_widgets.append(b)
                grid.addWidget(b, r, c + 1)
            grid.setColumnStretch(4, 1)
            ed_lay.addLayout(grid)

        ed_lay.addWidget(QLabel("<b>Fonts</b> (unchecked = system default)"))
        self.font_rows = {}
        fgrid = QGridLayout()
        fgrid.setHorizontalSpacing(12)
        for i, (key, label) in enumerate(THEME_FONT_SPEC):
            cb = QCheckBox(label)
            fc = QFontComboBox()
            sp = QSpinBox()
            sp.setRange(6, 40)
            sp.setSuffix(" pt")
            for wdg in (cb, fc, sp):
                self._theme_editor_widgets.append(wdg)
            cb.toggled.connect(lambda _on, k=key: self._font_changed(k))
            fc.currentFontChanged.connect(lambda _f, k=key: self._font_changed(k))
            sp.valueChanged.connect(lambda _v, k=key: self._font_changed(k))
            self.font_rows[key] = (cb, fc, sp)
            fgrid.addWidget(cb, i, 0)
            fgrid.addWidget(fc, i, 1)
            fgrid.addWidget(sp, i, 2)
        fgrid.setColumnStretch(3, 1)
        ed_lay.addLayout(fgrid)
        ed_lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(ed_inner)
        scroll.setWidgetResizable(True)
        aplay.addWidget(scroll, 1)
        aplay.addWidget(QLabel("<i>The theme is applied when you save the"
                               " settings.</i>"))
        tabs.addTab(aptab, "Appearance")

        self._reload_theme_combo(s.get("theme", "auto"))
        self.theme_combo.currentIndexChanged.connect(self._theme_selected)
        self._theme_selected()

    def _reload_theme_combo(self, select=None):
        from ..settings import BUILTIN_THEMES
        if select is None:
            select = self.theme_combo.currentData() or "auto"
        self.theme_combo.blockSignals(True)
        self.theme_combo.clear()
        self.theme_combo.addItem("Auto (follow Windows)", "auto")
        for name in list(BUILTIN_THEMES) + sorted(self._user_themes):
            suffix = "  (built-in)" if name in BUILTIN_THEMES else ""
            self.theme_combo.addItem(name + suffix, name)
        idx = self.theme_combo.findData(select)
        self.theme_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.theme_combo.blockSignals(False)

    def _theme_editor_dirty(self):
        return (self._theme_work is not None
                and self._theme_work != self._theme_source)

    def _theme_selected(self, *_):
        import copy
        from ..settings import BUILTIN_THEMES, _complete_theme
        name = self.theme_combo.currentData()
        if name == "auto" or name is None:
            self._theme_work = self._theme_source = None
            self.theme_note.setText(
                "<i>Auto uses the built-in Light or Dark theme depending on"
                " the Windows color scheme. Pick a concrete theme to edit"
                " its colors and fonts.</i>")
            for w in self._theme_editor_widgets:
                w.setEnabled(False)
            self.theme_del_btn.setEnabled(False)
            return
        if name in BUILTIN_THEMES:
            theme = _complete_theme(BUILTIN_THEMES[name], base=name)
            self.theme_note.setText(
                "<i><b>%s</b> is built-in and stays unchanged — you can try"
                " out edits below, but they can only be kept via 'Save as"
                " new theme…'.</i>" % name)
            self.theme_del_btn.setEnabled(False)
        else:
            theme = _complete_theme(self._user_themes[name])
            self.theme_note.setText(
                "<i>Your theme — edits are saved with the settings.</i>")
            self.theme_del_btn.setEnabled(True)
        for w in self._theme_editor_widgets:
            w.setEnabled(True)
        self._theme_work = copy.deepcopy(theme)
        self._theme_source = copy.deepcopy(theme)
        self._load_theme_editor()

    def _load_theme_editor(self):
        from PySide6.QtGui import QFont
        for key in self.color_btns:
            self._update_color_btn(key)
        for key, (cb, fc, sp) in self.font_rows.items():
            spec = self._theme_work["fonts"].get(key, {})
            for wdg in (cb, fc, sp):
                wdg.blockSignals(True)
            custom = bool(spec.get("family") or spec.get("size"))
            cb.setChecked(custom)
            if spec.get("family"):
                fc.setCurrentFont(QFont(spec["family"]))
            sp.setValue(spec.get("size") or 10)
            fc.setEnabled(custom)
            sp.setEnabled(custom)
            for wdg in (cb, fc, sp):
                wdg.blockSignals(False)

    def _update_color_btn(self, key):
        from PySide6.QtGui import QColor
        val = self._theme_work["colors"].get(key, "#000000") \
            if self._theme_work else "#000000"
        col = QColor(val)
        text_col = "#000000" if col.lightness() > 127 else "#ffffff"
        b = self.color_btns[key]
        b.setText(val)
        b.setStyleSheet("background-color: %s; color: %s;" % (val, text_col))

    def _pick_color(self, key):
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog
        if self._theme_work is None:
            return
        col = QColorDialog.getColor(
            QColor(self._theme_work["colors"].get(key, "#000000")), self)
        if col.isValid():
            self._theme_work["colors"][key] = col.name()
            self._update_color_btn(key)

    def _font_changed(self, key):
        if self._theme_work is None:
            return
        cb, fc, sp = self.font_rows[key]
        fc.setEnabled(cb.isChecked())
        sp.setEnabled(cb.isChecked())
        if cb.isChecked():
            self._theme_work["fonts"][key] = {
                "family": fc.currentFont().family(), "size": sp.value()}
        else:
            self._theme_work["fonts"][key] = {"family": "", "size": 0}

    def _theme_save_as(self):
        import copy
        from PySide6.QtWidgets import QInputDialog
        from ..settings import BUILTIN_THEMES, save_themes
        if self._theme_work is None:
            QMessageBox.information(self, "Pick a theme",
                                    "Select a concrete theme first (not Auto).")
            return
        name, ok = QInputDialog.getText(self, "Save as new theme",
                                        "Name of the new theme:")
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in BUILTIN_THEMES or name.lower() == "auto":
            QMessageBox.warning(self, "Reserved name",
                                "'%s' is reserved — choose another name." % name)
            return
        if name in self._user_themes and QMessageBox.question(
                self, "Overwrite theme",
                "A theme called '%s' already exists. Overwrite it?" % name
        ) != QMessageBox.Yes:
            return
        self._user_themes[name] = copy.deepcopy(self._theme_work)
        save_themes(self._user_themes)
        self._reload_theme_combo(select=name)
        self._theme_selected()
        self.owner.statusBar().showMessage(
            "Theme '%s' saved. Save the settings to switch to it." % name)

    def _theme_delete(self):
        from ..settings import save_themes
        name = self.theme_combo.currentData()
        if name not in self._user_themes:
            return
        if QMessageBox.question(self, "Delete theme",
                                "Delete the theme '%s'?" % name) != QMessageBox.Yes:
            return
        del self._user_themes[name]
        save_themes(self._user_themes)
        self._reload_theme_combo(select="auto")
        self._theme_selected()

    # ---------------------------------------------------- field name sets ---

    def _build_field_names_tab(self, tabs, s):
        import copy
        from PySide6.QtWidgets import QGridLayout
        from .. import tagio
        self._flabel_user = copy.deepcopy(s.get("field_label_sets") or {})
        self._flabels_work = None
        self._flabels_source = None

        ftab = QWidget()
        flay = QVBoxLayout(ftab)
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Field name set:"))
        self.flabel_combo = QComboBox()
        self.flabel_combo.setMinimumWidth(220)
        sel_row.addWidget(self.flabel_combo)
        dup_btn = QPushButton("Save as new set…")
        dup_btn.setToolTip("Duplicate the shown set (including your edits)"
                           " under a new name — the only way to keep changes"
                           " to the built-in sets")
        dup_btn.clicked.connect(self._flabels_save_as)
        sel_row.addWidget(dup_btn)
        self.flabel_del_btn = QPushButton("Delete set")
        self.flabel_del_btn.clicked.connect(self._flabels_delete)
        sel_row.addWidget(self.flabel_del_btn)
        sel_row.addStretch(1)
        flay.addLayout(sel_row)
        self.flabel_note = QLabel("")
        self.flabel_note.setWordWrap(True)
        flay.addWidget(self.flabel_note)
        head = QLabel(
            "How each metadata field is DISPLAYED everywhere in the app —"
            " tables, the album view, search, this settings page. The"
            " technical tag names (left) never change; only their display"
            " does. An empty alias falls back to the English name.")
        head.setWordWrap(True)
        flay.addWidget(head)

        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setHorizontalSpacing(14)
        self.flabel_edits = {}
        for i, field in enumerate(tagio.EDITABLE_FIELDS):
            r, c = i // 2, (i % 2) * 2
            grid.addWidget(QLabel(field), r, c)
            e = QLineEdit()
            e.textEdited.connect(lambda _t, f=field: self._flabel_edited(f))
            self.flabel_edits[field] = e
            grid.addWidget(e, r, c + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        fscroll = QScrollArea()
        fscroll.setWidget(inner)
        fscroll.setWidgetResizable(True)
        flay.addWidget(fscroll, 1)
        flay.addWidget(QLabel("<i>Applied when you save the settings.</i>"))
        tabs.addTab(ftab, "Field names")

        self._reload_flabel_combo(s.get("field_label_set", "English"))
        self.flabel_combo.currentIndexChanged.connect(self._flabels_selected)
        self._flabels_selected()

    def _reload_flabel_combo(self, select=None):
        from ..settings import FIELD_LABEL_SETS
        if select is None:
            select = self.flabel_combo.currentData() or "English"
        self.flabel_combo.blockSignals(True)
        self.flabel_combo.clear()
        for name in list(FIELD_LABEL_SETS) + sorted(self._flabel_user):
            suffix = "  (built-in)" if name in FIELD_LABEL_SETS else ""
            self.flabel_combo.addItem(name + suffix, name)
        idx = self.flabel_combo.findData(select)
        self.flabel_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.flabel_combo.blockSignals(False)

    def _flabels_dirty(self):
        return (self._flabels_work is not None
                and self._flabels_work != self._flabels_source)

    def _flabels_selected(self, *_):
        import copy
        from ..settings import FIELD_LABEL_SETS
        name = self.flabel_combo.currentData()
        if name in FIELD_LABEL_SETS:
            data = dict(FIELD_LABEL_SETS[name])
            self.flabel_note.setText(
                "<i><b>%s</b> is built-in and stays unchanged — you can try"
                " out edits below, but they can only be kept via 'Save as"
                " new set…'.</i>" % name)
            self.flabel_del_btn.setEnabled(False)
        else:
            data = dict(self._flabel_user.get(name, {}))
            self.flabel_note.setText(
                "<i>Your set — edits are saved with the settings.</i>")
            self.flabel_del_btn.setEnabled(True)
        self._flabels_work = copy.deepcopy(data)
        self._flabels_source = copy.deepcopy(data)
        from ..settings import FIELD_LABEL_SETS as _sets
        english = _sets["English"]
        for field, e in self.flabel_edits.items():
            e.blockSignals(True)
            e.setText(data.get(field, ""))
            e.setPlaceholderText(english.get(field, field))
            e.blockSignals(False)

    def _flabel_edited(self, field):
        if self._flabels_work is None:
            return
        text = self.flabel_edits[field].text().strip()
        if text:
            self._flabels_work[field] = text
        else:
            self._flabels_work.pop(field, None)

    def _flabels_save_as(self):
        import copy
        from PySide6.QtWidgets import QInputDialog
        from ..settings import FIELD_LABEL_SETS
        name, ok = QInputDialog.getText(self, "Save as new set",
                                        "Name of the new field name set:")
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in FIELD_LABEL_SETS:
            QMessageBox.warning(self, "Reserved name",
                                "'%s' is reserved — choose another name." % name)
            return
        if name in self._flabel_user and QMessageBox.question(
                self, "Overwrite set",
                "A set called '%s' already exists. Overwrite it?" % name
        ) != QMessageBox.Yes:
            return
        self._flabel_user[name] = copy.deepcopy(self._flabels_work or {})
        self._reload_flabel_combo(select=name)
        self._flabels_selected()
        self.owner.statusBar().showMessage(
            "Field name set '%s' created. Save the settings to switch to it."
            % name)

    def _flabels_delete(self):
        name = self.flabel_combo.currentData()
        if name not in self._flabel_user:
            return
        if QMessageBox.question(self, "Delete set",
                                "Delete the field name set '%s'?" % name
                                ) != QMessageBox.Yes:
            return
        del self._flabel_user[name]
        self._reload_flabel_combo(select="English")
        self._flabels_selected()

    def _reset_layout(self):
        self.cfg["settings"]["ui_layout"] = {}
        save_config(self.cfg)
        QMessageBox.information(
            self, "Layout reset",
            "All remembered column widths and window ratios were cleared.\n"
            "Views return to their defaults when you open them again.")

    def _add_pattern_row(self, field, regex, allowed):
        r = self.pat_table.rowCount()
        self.pat_table.insertRow(r)
        for c, text in enumerate((field, regex, allowed)):
            self.pat_table.setItem(r, c, QTableWidgetItem(text))

    def _collect(self):
        """Current widget values as a settings-shaped dict."""
        from .. import tagio
        s = {}
        for key, w in self.w.items():
            if isinstance(w, QCheckBox):
                s[key] = w.isChecked()
            elif isinstance(w, QComboBox):
                s[key] = w.currentText()
            elif isinstance(w, QSpinBox):
                s[key] = w.value()
            else:
                s[key] = w.text().strip()
        if not s.get("multi_sep"):
            s["multi_sep"] = DEFAULT_SETTINGS["multi_sep"]
        s["required_fields"] = [f for f, c in self.req_boxes.items()
                                if c.isChecked()]
        s["multi_value_fields"] = [f for f, c in self.mv_boxes.items()
                                   if c.isChecked()]
        patterns = {}
        for r in range(self.pat_table.rowCount()):
            def cell(c):
                it = self.pat_table.item(r, c)
                return (it.text() if it else "").strip()
            field, regex, allowed = cell(0), cell(1), cell(2)
            if field in tagio.EDITABLE_FIELDS and (regex or allowed):
                patterns[field] = {"regex": regex, "allowed": allowed}
        s["field_patterns"] = patterns
        mode_keys = ["enabled", "postponed", "disabled"]
        s["rule_modes"] = {
            rule: next((mode_keys[i] for i, rb in enumerate(radios)
                        if rb.isChecked()), "enabled")
            for rule, radios in self.rule_radios.items()}
        # per-rule options (Problem types tab, Options column)
        pad, totals, padtot = self.rule_opts["track_format"].currentData()
        s["track_pad"], s["track_totals"], s["track_pad_total"] = pad, totals, padtot
        s["albumartist_mode"] = self.rule_opts["albumartist"].currentData()
        s["strip_id3v1"] = self.rule_opts["id3v1"].currentData()
        s["utf8_all_frames"] = self.rule_opts["encoding"].currentData()
        s["sync_artist_albumartist"] = self.rule_opts["artist_sync"].currentData()
        s["theme"] = self.theme_combo.currentData() or "auto"
        import copy as _copy
        s["field_label_set"] = self.flabel_combo.currentData() or "English"
        s["field_label_sets"] = _copy.deepcopy(self._flabel_user)
        return s

    def mark_clean(self):
        self._saved = self._collect()

    def is_dirty(self):
        if self._theme_editor_dirty() or self._flabels_dirty():
            return True
        return self._collect() != getattr(self, "_saved", self._collect())

    def save(self):
        import copy
        from .. import rules
        from ..settings import BUILTIN_THEMES, FIELD_LABEL_SETS, save_themes
        from .common import apply_field_labels, apply_theme
        # theme edits: persist into the user theme, or refuse for built-ins
        if self._theme_editor_dirty():
            name = self.theme_combo.currentData()
            if name in self._user_themes:
                self._user_themes[name] = copy.deepcopy(self._theme_work)
                save_themes(self._user_themes)
                self._theme_source = copy.deepcopy(self._theme_work)
            elif name in BUILTIN_THEMES:
                QMessageBox.information(
                    self, "Built-in theme",
                    "'%s' is built-in and cannot be changed. Your color/font"
                    " edits were NOT saved — use 'Save as new theme…' to keep"
                    " them." % name)
                self._theme_selected()   # reset editor to the built-in values
        # field-name edits: same rules as themes
        if self._flabels_dirty():
            name = self.flabel_combo.currentData()
            if name in FIELD_LABEL_SETS:
                QMessageBox.information(
                    self, "Built-in set",
                    "'%s' is built-in and cannot be changed. Your field-name"
                    " edits were NOT saved — use 'Save as new set…' to keep"
                    " them." % name)
                self._flabels_selected()
            else:
                self._flabel_user[name] = copy.deepcopy(self._flabels_work)
                self._flabels_source = copy.deepcopy(self._flabels_work)
        self.cfg["settings"].update(self._collect())
        s = self.cfg["settings"]
        save_config(self.cfg)
        self.mark_clean()
        apply_theme(s.get("theme", "auto"))
        apply_field_labels(s)
        self.owner.rebuild_search_pane()
        if self.reeval.isChecked():
            rules.evaluate(self.owner.con, s)
        self.owner.refresh_tree()
        self.owner.detail.refresh()
        self.owner.statusBar().showMessage("Settings saved." + (
            " Rules re-evaluated." if self.reeval.isChecked() else ""))


class ChangelogPane(QWidget):
    """Full-page change log. owner = MainWindow (uses its live connection)."""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        lay = QVBoxLayout(self)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter…")
        self.filter.textChanged.connect(self._reload)
        lay.addWidget(self.filter)
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        enable_copy(self.table)
        lay.addWidget(self.table)
        self._reload()
        persist_header(self.owner.cfg, "changelog", self.table.horizontalHeader())

    @property
    def con(self):
        return self.owner.con

    def reload(self):
        self._reload()

    def _reload(self):
        flt = "%" + self.filter.text() + "%"
        rows = self.con.execute(
            "SELECT ts, path, field, old, new, origin FROM changelog"
            " WHERE path LIKE ? OR field LIKE ? OR old LIKE ? OR new LIKE ?"
            " ORDER BY id DESC LIMIT 500", (flt, flt, flt, flt)).fetchall()
        self.table.clear()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["When", "File", "Field", "Old", "New", "Origin"])
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                if c in (3, 4):
                    try:
                        val = join_vals(json.loads(val))
                    except Exception:
                        pass
                if c == 1:
                    val = Path(str(val)).name
                if c == 2:
                    val = PSEUDO_FIELD_LABELS.get(str(val),
                                                  field_label(str(val)))
                self.table.setItem(r, c, QTableWidgetItem(str(val)))
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.resizeColumnsToContents()


class ExceptionsDialog(QDialog):
    """List the permanent exceptions of an album/artist; remove to re-check."""

    def __init__(self, con, settings, adir, artist, parent=None):
        super().__init__(parent)
        self.con, self.settings = con, settings
        self.changed = False
        self.setWindowTitle("Exceptions — %s" % (Path(adir).name if adir else artist))
        self.resize(760, 380)
        self.adir, self.artist = adir, artist
        lay = QVBoxLayout(self)
        self.table = QTableWidget()
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        enable_copy(self.table)
        lay.addWidget(self.table)
        btns = QHBoxLayout()
        rm = QPushButton("Remove selected exceptions (check them again)")
        rm.clicked.connect(self._remove)
        btns.addWidget(rm)
        btns.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        lay.addLayout(btns)
        self._reload()

    def _reload(self):
        rows = self.con.execute(
            "SELECT id, created_at, rule, field, info FROM exceptions"
            " WHERE album_dir=? OR (album_dir IS NULL AND artist_folder=?)"
            " ORDER BY id DESC", (self.adir or "", self.artist or "")).fetchall()
        self._ids = [r[0] for r in rows]
        self.table.clear()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["When", "Rule", "Field", "What"])
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row[1:]):
                it = QTableWidgetItem(str(val or ""))
                it.setForeground(QColor(STATUS_COLORS["exception"]))
                self.table.setItem(r, c, it)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.resizeColumnsToContents()

    def _remove(self):
        rows = sorted({it.row() for it in self.table.selectedItems()},
                      reverse=True)
        if not rows:
            return
        for r in rows:
            if 0 <= r < len(self._ids):
                applier.remove_exception(self.con, self.settings, self._ids[r])
        self.changed = True
        self._reload()


class SavedExpressionsDialog(QDialog):
    """Manage saved search expressions (name + pattern): add, edit in place,
    delete, or pick one to use."""

    def __init__(self, cfg, prefill="", parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.chosen = None
        self.setWindowTitle("Saved expressions")
        self.resize(680, 420)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Double-click a cell to edit it; double-click a row"
                             " while holding nothing to use it via the button."))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Name / what it does", "Expression"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 240)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemChanged.connect(self._edited)
        lay.addWidget(self.table)
        add_row = QHBoxLayout()
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("name (e.g. 'four-digit year')")
        add_row.addWidget(self.new_name)
        self.new_edit = QLineEdit(prefill)
        self.new_edit.setPlaceholderText("expression…")
        add_row.addWidget(self.new_edit, 1)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add)
        add_row.addWidget(add_btn)
        lay.addLayout(add_row)
        btns = QHBoxLayout()
        use_btn = QPushButton("Use selected")
        use_btn.clicked.connect(self._use)
        btns.addWidget(use_btn)
        del_btn = QPushButton("Delete selected")
        del_btn.clicked.connect(self._delete)
        btns.addWidget(del_btn)
        btns.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        btns.addWidget(close)
        lay.addLayout(btns)
        self._reload()

    def _exprs(self):
        return self.cfg["settings"].setdefault("saved_expressions", [])

    def _reload(self):
        self.table.blockSignals(True)
        exprs = self._exprs()
        self.table.setRowCount(len(exprs))
        for r, e in enumerate(exprs):
            self.table.setItem(r, 0, QTableWidgetItem(e.get("name", "")))
            self.table.setItem(r, 1, QTableWidgetItem(e.get("pattern", "")))
        self.table.blockSignals(False)

    def _edited(self, item):
        exprs = self._exprs()
        r = item.row()
        if 0 <= r < len(exprs):
            key = "name" if item.column() == 0 else "pattern"
            exprs[r][key] = item.text()
            save_config(self.cfg)

    def _add(self):
        pattern = self.new_edit.text().strip()
        if not pattern:
            return
        name = self.new_name.text().strip() or pattern
        self._exprs().append({"name": name, "pattern": pattern})
        save_config(self.cfg)
        self._reload()
        self.new_edit.clear()
        self.new_name.clear()

    def _delete(self):
        r = self.table.currentRow()
        exprs = self._exprs()
        if 0 <= r < len(exprs):
            del exprs[r]
            save_config(self.cfg)
            self._reload()

    def _use(self):
        r = self.table.currentRow()
        exprs = self._exprs()
        if 0 <= r < len(exprs):
            self.chosen = exprs[r]["pattern"]
            self.accept()


class _SearchGroup(QFrame):
    """One bracket of the search: (cond OR cond ...) or (cond AND cond ...)."""

    def __init__(self, pane):
        super().__init__()
        self.pane = pane
        self.setFrameStyle(QFrame.StyledPanel)
        v = QVBoxLayout(self)
        head = QHBoxLayout()
        head.addWidget(QLabel("Inside this group match:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["ALL conditions (AND)", "ANY condition (OR)"])
        head.addWidget(self.mode_combo)
        add = QPushButton("+ Condition")
        add.clicked.connect(self.add_condition)
        head.addWidget(add)
        head.addStretch(1)
        rm = QPushButton("Remove group")
        rm.clicked.connect(lambda: self.pane.remove_group(self))
        head.addWidget(rm)
        v.addLayout(head)
        self.cond_lay = QVBoxLayout()
        v.addLayout(self.cond_lay)
        self.add_condition()

    def add_condition(self):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        fld = QComboBox()
        fld.addItem("(any field)", None)
        for f in self.pane.fields:
            fld.addItem(field_label(f), f)
        op = QComboBox()
        op.addItems(self.pane.OPS)
        val = QLineEdit()
        val.setPlaceholderText("value or regular expression")
        val.returnPressed.connect(self.pane.run_search)
        val.installEventFilter(self.pane)   # remember the last-focused value box
        rm = QPushButton("✕")
        rm.setFixedWidth(28)
        rm.clicked.connect(lambda: (self.cond_lay.removeWidget(w), w.deleteLater()))
        for x in (fld, op, val, rm):
            h.addWidget(x)
        h.setStretchFactor(val, 1)
        w._parts = (fld, op, val)
        self.cond_lay.addWidget(w)

    def mode_any(self):
        return self.mode_combo.currentIndex() == 1

    def conditions(self):
        out = []
        for i in range(self.cond_lay.count()):
            w = self.cond_lay.itemAt(i).widget()
            if w is None or not hasattr(w, "_parts"):
                continue
            fld, op, val = w._parts
            out.append((fld.currentData(), op.currentText(), val.text()))
        return out


class SearchPane(QWidget):
    """Metadata search page: condition groups over all scanned files.
    Groups act as brackets: (a OR b) AND (c OR d), or (a AND b) OR (c AND d)."""

    OPS = ["contains", "not contains", "equals", "not equals",
           "matches regex", "not regex", "is empty", "is not empty"]

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        from .. import tagio
        self.owner = owner
        self.fields = list(tagio.EDITABLE_FIELDS)
        self._last_edit = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Search metadata</b> — each group is one pair of"
                             " brackets; e.g. (artist contains X <i>or</i> Y)"
                             " <i>and</i> (year equals Z)."))
        self.groups_lay = QVBoxLayout()
        lay.addLayout(self.groups_lay)
        row = QHBoxLayout()
        row.addWidget(QLabel("Combine groups with:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["ALL groups must match (AND)",
                                  "ANY group matches (OR)"])
        row.addWidget(self.mode_combo)
        addg = QPushButton("+ Add group")
        addg.clicked.connect(self._add_group)
        row.addWidget(addg)
        saved_btn = QPushButton("Saved expressions…")
        saved_btn.clicked.connect(self._saved_expressions)
        row.addWidget(saved_btn)
        go = QPushButton("Search")
        go.clicked.connect(self.run_search)
        row.addWidget(go)
        self.count_lbl = QLabel("")
        row.addWidget(self.count_lbl)
        row.addStretch(1)
        lay.addLayout(row)
        self.results = QTableWidget()
        self.results.setEditTriggers(QTableWidget.NoEditTriggers)
        self.results.setSelectionBehavior(QTableWidget.SelectRows)
        self.results.itemDoubleClicked.connect(self._open)
        enable_copy(self.results)
        lay.addWidget(self.results, 1)
        lay.addWidget(QLabel("<i>Double-click a result to open the track;"
                             " Ctrl+C copies the selected rows.</i>"))
        self._rows = []
        self._add_group()
        persist_header(self.owner.cfg, "search_results",
                       self.results.horizontalHeader())

    def _add_group(self):
        self.groups_lay.addWidget(_SearchGroup(self))

    def remove_group(self, group):
        if len(self.groups()) <= 1:
            return          # keep at least one group
        self.groups_lay.removeWidget(group)
        group.deleteLater()

    def groups(self):
        out = []
        for i in range(self.groups_lay.count()):
            w = self.groups_lay.itemAt(i).widget()
            if isinstance(w, _SearchGroup):
                out.append(w)
        return out

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.FocusIn and isinstance(obj, QLineEdit):
            self._last_edit = obj
        return super().eventFilter(obj, event)

    def _saved_expressions(self):
        prefill = self._last_edit.text() if self._last_edit else ""
        dlg = SavedExpressionsDialog(self.owner.cfg, prefill, self)
        dlg.exec()
        if dlg.chosen:
            target = self._last_edit
            if target is None:      # no value box focused yet: use the first one
                grps = self.groups()
                if grps and grps[0].cond_lay.count():
                    w = grps[0].cond_lay.itemAt(0).widget()
                    target = w._parts[2] if w and hasattr(w, "_parts") else None
            if target is not None:
                target.setText(dlg.chosen)

    @staticmethod
    def _match(op, text, needle):
        import re as _re
        t, nd = text.casefold(), needle.casefold()
        if op == "contains":
            return nd in t
        if op == "not contains":
            return nd not in t
        if op == "equals":
            return t == nd
        if op == "not equals":
            return t != nd
        if op == "matches regex":
            try:
                return _re.search(needle, text) is not None
            except _re.error:
                return False
        if op == "not regex":
            try:
                return _re.search(needle, text) is None
            except _re.error:
                return True
        if op == "is empty":
            return text == ""
        return text != ""      # is not empty

    def run_search(self):
        from .. import tagio
        con = self.owner.con
        sep = self.owner.cfg["settings"]["multi_sep"]
        group_specs = []
        for grp in self.groups():
            conds = [c for c in grp.conditions()
                     if c[2] or c[1] in ("is empty", "is not empty")]
            if conds:
                group_specs.append((grp.mode_any(), conds))
        meta = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
            "SELECT id, artist_folder, album_dir, filename FROM tracks"
            " WHERE missing=0")}
        hits = []
        top_any = self.mode_combo.currentIndex() == 1
        for tid, tags_json in con.execute(
                "SELECT track_id, tags FROM snapshots WHERE id IN"
                " (SELECT MAX(id) FROM snapshots GROUP BY track_id)"):
            if tid not in meta:
                continue
            tags = json.loads(tags_json)
            texts = {f: sep.join(tags.get(f, [])) for f in tagio.EDITABLE_FIELDS}
            shown = []
            group_results = []
            for mode_any, conds in group_specs:
                passes = []
                for field, op, val in conds:
                    negative = op.startswith("not ") or op == "is empty"
                    if field is None:       # (any field)
                        if negative:
                            p = all(self._match(op, t, val) for t in texts.values())
                        else:
                            p = any(self._match(op, t, val) for t in texts.values())
                    else:
                        p = self._match(op, texts.get(field, ""), val)
                        shown.append("%s = %s" % (field_label(field),
                                                  texts.get(field, "")))
                    passes.append(p)
                group_results.append(any(passes) if mode_any else all(passes))
            ok = ((any(group_results) if top_any else all(group_results))
                  if group_results else True)
            if ok:
                hits.append((tid, meta[tid], "; ".join(dict.fromkeys(shown))))
            if len(hits) >= 2000:
                break
        self._rows = hits
        self.count_lbl.setText("%d matches%s" % (
            len(hits), " (capped at 2000)" if len(hits) >= 2000 else ""))
        self.results.clear()
        self.results.setColumnCount(4)
        self.results.setHorizontalHeaderLabels(["Artist", "Album", "File", "Matched"])
        self.results.setRowCount(len(hits))
        for r, (tid, (artist, adir, fname), shown) in enumerate(hits):
            self.results.setItem(r, 0, QTableWidgetItem(artist))
            self.results.setItem(r, 1, QTableWidgetItem(self.owner.album_display(adir)))
            self.results.setItem(r, 2, QTableWidgetItem(fname))
            self.results.setItem(r, 3, QTableWidgetItem(shown))
        self.results.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.results.resizeColumnsToContents()

    def _open(self, item):
        r = item.row()
        if 0 <= r < len(self._rows):
            self.owner.open_track_from_search(self._rows[r][0])


class CoverSearchDialog(QDialog):
    """Search MusicBrainz + Cover Art Archive for a better album cover."""

    def __init__(self, con, artist_name, album_name, album_dir, parent=None):
        super().__init__(parent)
        self.con, self.album_dir = con, album_dir
        self.chosen = False
        self.setWindowTitle("Find cover — %s / %s" % (artist_name, album_name))
        self.resize(860, 520)
        lay = QVBoxLayout(self)

        row = QHBoxLayout()
        self.q_artist = QLineEdit(artist_name)
        self.q_album = QLineEdit(album_name)
        search = QPushButton("Search MusicBrainz")
        search.clicked.connect(self._search)
        row.addWidget(QLabel("Artist:"))
        row.addWidget(self.q_artist)
        row.addWidget(QLabel("Album:"))
        row.addWidget(self.q_album)
        row.addWidget(search)
        lay.addLayout(row)

        mid = QHBoxLayout()
        self.results = QListWidget()
        self.results.currentRowChanged.connect(self._preview)
        mid.addWidget(self.results, 1)
        right = QVBoxLayout()
        self.preview = QLabel("Select a release to preview its cover,\n"
                              "or drag an image file here,\n"
                              "or use 'Load from disk…'")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(420, 380)
        right.addWidget(self.preview)
        disk_btn = QPushButton("Load from disk…")
        disk_btn.clicked.connect(self._from_disk)
        right.addWidget(disk_btn)
        self.use_btn = QPushButton("Use this cover (embed in all tracks + folder.jpg)")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self._use)
        right.addWidget(self.use_btn)
        mid.addLayout(right, 1)
        lay.addLayout(mid)

        self.releases = []
        self._cover_cache = {}
        self._current = None      # (mime, data, note) of the previewed image
        self.setAcceptDrops(True)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for url in ev.mimeData().urls():
            if url.isLocalFile():
                self._load_local(url.toLocalFile())
                break

    def _from_disk(self):
        fp, _ = QFileDialog.getOpenFileName(
            self, "Choose a cover image", "",
            "Images (*.jpg *.jpeg *.png *.webp *.bmp);;All files (*)")
        if fp:
            self._load_local(fp)

    def _load_local(self, fp):
        from io import BytesIO

        from PIL import Image
        try:
            data = Path(fp).read_bytes()
            with Image.open(BytesIO(data)) as im:
                fmt = (im.format or "JPEG").upper()
        except Exception as e:
            QMessageBox.warning(self, "Cannot read image", str(e))
            return
        mime = "image/png" if fmt == "PNG" else "image/jpeg"
        if fmt not in ("JPEG", "PNG"):
            from io import BytesIO as _B
            with Image.open(_B(data)) as im:
                buf = _B()
                im.convert("RGB").save(buf, "JPEG", quality=92)
                data = buf.getvalue()
            mime = "image/jpeg"
        pm = QPixmap()
        if not pm.loadFromData(data):
            QMessageBox.warning(self, "Cannot read image", Path(fp).name)
            return
        self._current = (mime, data, "local file: %s" % Path(fp).name)
        self.preview.setPixmap(pm.scaled(420, 420, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))
        self.use_btn.setEnabled(True)

    def _search(self):
        self.results.clear()
        self.preview.setText("Searching…")
        self.preview.repaint()
        try:
            self.releases = online.search_releases(
                self.q_artist.text().strip(), self.q_album.text().strip())
        except Exception as e:
            QMessageBox.warning(self, "Search failed", str(e))
            return
        if not self.releases:
            self.preview.setText("No releases found.")
        for rel in self.releases:
            self.results.addItem("%s — %s  (%s %s %s)  score %s" % (
                rel["artist"], rel["title"], rel["date"], rel["country"],
                rel["format"], rel["score"]))

    def _preview(self, row):
        self.use_btn.setEnabled(False)
        if row < 0 or row >= len(self.releases):
            return
        rid = self.releases[row]["id"]
        if rid not in self._cover_cache:
            self.preview.setText("Loading cover…")
            self.preview.repaint()
            try:
                self._cover_cache[rid] = online.fetch_cover(rid, size=500)
            except Exception as e:
                self._cover_cache[rid] = None
                self.preview.setText("Error: %s" % e)
                return
        got = self._cover_cache[rid]
        if got is None:
            self.preview.setText("No cover in Cover Art Archive for this release.")
            return
        rel = self.releases[row]
        self._current = (got[0], got[1], "%s — %s (%s)"
                         % (rel["artist"], rel["title"], rel["date"]))
        pm = QPixmap()
        pm.loadFromData(got[1])
        self.preview.setPixmap(pm.scaled(420, 420, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))
        self.use_btn.setEnabled(True)

    def _use(self):
        if not self._current:
            return
        mime, data, note = self._current
        cid = self.con.execute(
            "INSERT INTO pending_covers(album_dir, mime, data, note) VALUES (?,?,?,?)",
            (self.album_dir, mime, data, note)).lastrowid
        row_t = self.con.execute(
            "SELECT artist_folder FROM tracks WHERE album_dir=? LIMIT 1",
            (self.album_dir,)).fetchone()
        db.upsert_proposal(self.con, None, row_t[0] if row_t else "", self.album_dir,
                           "cover", ["current embedded art"],
                           ["pending_cover:%d" % cid], "online", rule="cover")
        self.con.commit()
        self.chosen = True
        QMessageBox.information(
            self, "Cover proposed",
            "The cover was stored as a proposal. It will be embedded into all"
            " tracks of the album when you apply.")
        self.accept()


class ArtistImageDialog(QDialog):
    """Find artist.jpg online (Deezer, TheAudioDB), or load it from disk."""

    def __init__(self, con, settings, artist_folder, display_name, root, parent=None):
        super().__init__(parent)
        self.con, self.settings = con, settings
        self.artist_folder, self.root = artist_folder, root
        self.saved = False
        self.setWindowTitle("Artist image — %s" % display_name)
        self.resize(760, 620)
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.q = QLineEdit(display_name)
        row.addWidget(self.q)
        btn = QPushButton("Search online")
        btn.clicked.connect(self._search)
        row.addWidget(btn)
        file_btn = QPushButton("Load from disk…")
        file_btn.clicked.connect(self._from_disk)
        row.addWidget(file_btn)
        lay.addLayout(row)

        self.results = QListWidget()
        self.results.setMaximumHeight(150)
        self.results.currentRowChanged.connect(self._preview_candidate)
        lay.addWidget(self.results)

        self.preview = QLabel("Search online, load an image from disk,\n"
                              "or drag an image file here")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(480, 360)
        lay.addWidget(self.preview, 1)
        self.save_btn = QPushButton("Save as artist.jpg")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        lay.addWidget(self.save_btn)
        self._data = None
        self._cands = []
        self._url_cache = {}
        self.setAcceptDrops(True)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for url in ev.mimeData().urls():
            if url.isLocalFile():
                try:
                    data = Path(url.toLocalFile()).read_bytes()
                except OSError:
                    continue
                self._show_data(data, "file: %s" % Path(url.toLocalFile()).name)
                break

    def _search(self):
        self.preview.setText("Searching Deezer + TheAudioDB…")
        self.preview.repaint()
        self._cands = online.search_artist_images(self.q.text().strip())
        self.results.clear()
        for c in self._cands:
            self.results.addItem("%s — %s" % (c["source"], c["label"]))
        if not self._cands:
            self.preview.setText("Nothing found online — you can still use"
                                 " 'Load from disk…'.")

    def _from_disk(self):
        fp, _ = QFileDialog.getOpenFileName(
            self, "Choose an artist image", "",
            "Images (*.jpg *.jpeg *.png *.webp *.bmp);;All files (*)")
        if not fp:
            return
        try:
            data = Path(fp).read_bytes()
        except OSError as e:
            QMessageBox.warning(self, "Cannot read file", str(e))
            return
        self._show_data(data, "file: %s" % Path(fp).name)

    def _preview_candidate(self, row):
        if row < 0 or row >= len(self._cands):
            return
        c = self._cands[row]
        if c["url"] not in self._url_cache:
            self.preview.setText("Loading…")
            self.preview.repaint()
            try:
                self._url_cache[c["url"]] = online.fetch_image(c["url"])
            except Exception as e:
                self._url_cache[c["url"]] = None
                self.preview.setText("Error: %s" % e)
                return
        data = self._url_cache[c["url"]]
        if not data:
            self.preview.setText("This candidate has no usable image.")
            self.save_btn.setEnabled(False)
            return
        self._show_data(data, "%s — %s" % (c["source"], c["label"]))

    def _show_data(self, data, note):
        pm = QPixmap()
        if not pm.loadFromData(data):
            self.preview.setText("Not a readable image (%s)." % note)
            self.save_btn.setEnabled(False)
            return
        self._data = data
        self.preview.setPixmap(pm.scaled(560, 460, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))
        self.preview.setToolTip(note)
        self.save_btn.setEnabled(True)

    def _save(self):
        from io import BytesIO

        from PIL import Image

        data = self._data
        try:
            with Image.open(BytesIO(data)) as im:
                if (im.format or "").upper() != "JPEG":
                    buf = BytesIO()
                    im.convert("RGB").save(buf, "JPEG", quality=92)
                    data = buf.getvalue()
        except Exception:
            pass   # save the raw bytes if PIL cannot convert
        target = Path(self.root) / self.artist_folder / "artist.jpg"
        target.write_bytes(data)
        self.con.execute("UPDATE artists SET artist_jpg=1 WHERE folder=?",
                         (self.artist_folder,))
        db.log_change(self.con, None, str(target), "artist_jpg",
                      [], [self.preview.toolTip() or "artist image set"], "online")
        self.con.execute("DELETE FROM issues WHERE album_dir IS NULL"
                         " AND artist_folder=? AND rule='artist_jpg'",
                         (self.artist_folder,))
        self.con.commit()
        self.saved = True
        QMessageBox.information(self, "Saved", "artist.jpg written to %s" % target)
        self.accept()

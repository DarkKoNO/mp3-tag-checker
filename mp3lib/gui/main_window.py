"""Main window: page bar (Library / Search / Change log / Settings),
action bar (library, scan, apply, internet), and the library tree views."""

from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressDialog, QPushButton,
    QSplitter, QStackedWidget, QTreeView, QVBoxLayout, QWidget,
)

from .. import applier, db, online, scanner
from ..rules import (IMAGE_RULES, RULE_SEVERITY, is_non_fixable,
                     missing_field_of, rule_description, rule_label,
                     rule_priority)
from ..settings import active_library, lib_db_path, save_config
from ..updater import local_version
from .common import (SEV_RANK, Worker, dot_icon, enable_copy, field_label,
                     persist_header, persist_splitter, worse)
from .detail_panel import DetailPanel
from .dialogs import (ChangelogPane, LibrariesDialog, ScanDialog,
                      ScanReportDialog, SearchPane, SettingsPane,
                      UpdateCheckThread, UpdateDialog, run_update_flow)

KIND_ROLE = Qt.UserRole + 1
KEY_ROLE = Qt.UserRole + 2

PAGES = ["Library", "Search", "Change log", "Settings"]


class _LibraryTree(QTreeView):
    """Tree that ignores clicks on empty space: the right panel always mirrors
    the selection, so the selection must never be cleared by a stray click."""

    def mousePressEvent(self, ev):
        if not self.indexAt(ev.position().toPoint()).isValid():
            return
        super().mousePressEvent(ev)


class MainWindow(QMainWindow):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.lib = active_library(cfg)
        # zero libraries is fine: an empty in-memory db keeps all views working
        self.con = db.connect(lib_db_path(self.lib) if self.lib else ":memory:")
        self.worker = None
        self._lib_widgets = []      # disabled while no library exists
        self.setWindowTitle("MP3 Tag Checker %s" % local_version())
        self.resize(1500, 900)

        central = QWidget()
        root_lay = QVBoxLayout(central)
        root_lay.setContentsMargins(6, 6, 6, 0)
        root_lay.setSpacing(4)
        self.setCentralWidget(central)

        # ---- row 1: main menu (pages)
        page_bar = QWidget()
        page_bar.setObjectName("pageBar")
        page_row = QHBoxLayout(page_bar)
        page_row.setContentsMargins(8, 6, 8, 6)
        page_row.setSpacing(6)
        self.page_btns = []
        page_group = QButtonGroup(self)
        page_group.setExclusive(True)
        for i, name in enumerate(PAGES):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setMinimumWidth(140)
            page_group.addButton(b)
            b.clicked.connect(lambda _c, idx=i: self.switch_page(idx))
            page_row.addWidget(b)
            self.page_btns.append(b)
        self.page_btns[0].setChecked(True)
        page_row.addStretch(1)
        root_lay.addWidget(page_bar)

        # ---- rows 2 + 3: actions, grouped into captioned blocks
        def act_group(title, *widgets):
            f = QFrame()
            f.setObjectName("actGroup")
            v = QVBoxLayout(f)
            v.setContentsMargins(10, 3, 10, 5)
            v.setSpacing(1)
            cap = QLabel(title)
            cap.setObjectName("actCaption")
            v.addWidget(cap)
            h = QHBoxLayout()
            h.setSpacing(6)
            for w in widgets:
                h.addWidget(w)
            v.addLayout(h)
            return f

        act_row = QHBoxLayout()
        act_row.setSpacing(10)
        self.lib_combo = QComboBox()
        self.lib_combo.setMinimumWidth(170)
        self.lib_combo.setToolTip("The music library you are working with")
        lib_btn = QPushButton("Libraries…")
        lib_btn.setToolTip("Add, edit or remove libraries (root folders)")
        lib_btn.clicked.connect(self.manage_libraries)
        act_row.addWidget(act_group("Library", self.lib_combo, lib_btn))

        action_btns = []
        for label, slot, tip in (
                ("Scan library…", self.start_scan,
                 "Read the files of the selected (or all) folders from disk"
                 " and run all checks. Nothing is written to the MP3s."),
                ("Apply selected", self.apply_selected,
                 "Write the open proposed changes of whatever is selected in"
                 " the catalog on the LEFT (artists / albums / tracks / a"
                 " change type) into the MP3 files. To apply single rows"
                 " picked in the right panel, use 'Apply selected changes'"
                 " at the bottom there."),
                ("Refresh view", self.refresh_tree,
                 "Redraw the lists from the database - a light refresh,"
                 " nothing is read from disk. To re-read the files"
                 " themselves, use 'Rescan' under an album/artist, or Scan"
                 " library."),
                ("Remove…", self.remove_selected,
                 "Remove the artists/albums/tracks selected in the catalog"
                 " from the library database. Files on disk are NOT touched;"
                 " scanning the library again adds them back.")):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            action_btns.append(b)
            self._lib_widgets.append(b)
        self._actions_grp = act_group("Actions", *action_btns)
        act_row.addWidget(self._actions_grp)

        net_btn = QPushButton("Internet check…")
        self._lib_widgets.append(net_btn)
        net_btn.setToolTip("Look up the selected artists/albums on MusicBrainz"
                           " and create separate internet proposals")
        net_btn.clicked.connect(self.start_online_check)
        self.online_add_cb = QCheckBox("Show additions")
        self.online_add_cb.setToolTip("Internet proposals that fill empty fields")
        self.online_add_cb.setChecked(
            bool(self.cfg["settings"].get("show_online_add", True)))
        self.online_diff_cb = QCheckBox("Show differences")
        self.online_diff_cb.setToolTip(
            "Internet proposals that disagree with the current value")
        self.online_diff_cb.setChecked(
            bool(self.cfg["settings"].get("show_online_diff", True)))
        for cb in (self.online_add_cb, self.online_diff_cb):
            cb.toggled.connect(self._online_toggled)
        clear_btn = QPushButton("Clear…")
        clear_btn.setToolTip("Delete all unapplied internet-metadata proposals")
        clear_btn.clicked.connect(self.clear_online)
        self._lib_widgets.append(clear_btn)
        self._internet_grp = act_group("Internet", net_btn, self.online_add_cb,
                                       self.online_diff_cb, clear_btn)
        act_row.addWidget(self._internet_grp)

        act_row.addStretch(1)
        self.path_label = QLabel("")
        act_row.addWidget(self.path_label)
        root_lay.addLayout(act_row)

        # row 3: how the catalog (left side) is sorted and filtered
        # (wrapped in a widget so it can hide on non-Library pages)
        self.sort_bar = QWidget()
        sort_row = QHBoxLayout(self.sort_bar)
        sort_row.setContentsMargins(0, 0, 0, 0)
        sort_row.setSpacing(10)
        self.artist_btn = QPushButton("By artist")
        self.artist_btn.setToolTip(
            "Sort the catalog by artist: artist → album → track")
        self.type_btn = QPushButton("By change type")
        self.type_btn.setToolTip(
            "Sort the catalog by type of change/problem: each top-level entry"
            " is one kind of change - hover it for a full explanation")
        for b in (self.artist_btn, self.type_btn):
            b.setCheckable(True)
            self._lib_widgets.append(b)
        self.artist_btn.setChecked(True)
        view_group = QButtonGroup(self)
        view_group.addButton(self.artist_btn)
        view_group.addButton(self.type_btn)
        view_group.setExclusive(True)
        self.artist_btn.toggled.connect(self.refresh_tree)
        sort_row.addWidget(act_group("Sort by", self.artist_btn, self.type_btn))

        self.sev_combo = QComboBox()
        self.sev_combo.addItems(["All", "Red + yellow", "Red only",
                                 "Missing files (gray)", "With exceptions"])
        self.sev_combo.setToolTip(
            "Show only entries of the chosen severity (red = real problem,"
            " yellow = should be improved, gray = file missing on disk), or"
            " only entries with exceptions")
        self.sev_combo.currentIndexChanged.connect(self.refresh_tree)
        self._lib_widgets.append(self.sev_combo)
        self.images_cb = QCheckBox("Show image problems")
        self.images_cb.setToolTip("Covers, folder.jpg and artist.jpg problems"
                                  " - hide them to focus on the tags")
        self.images_cb.setChecked(
            bool(self.cfg["settings"].get("show_image_problems", True)))
        self.images_cb.toggled.connect(self._images_toggled)
        self._lib_widgets.append(self.images_cb)
        sort_row.addWidget(act_group("Filter", self.sev_combo, self.images_cb))
        sort_row.addStretch(1)
        root_lay.addWidget(self.sort_bar)

        # chrome styling (page bar, button blocks, fonts) comes from the
        # active theme's application-level stylesheet - see common.apply_theme

        # ---- pages stack
        self.stack = QStackedWidget()
        root_lay.addWidget(self.stack, 1)

        # page 0: library (tree | detail)
        lib_page = QWidget()
        lp = QHBoxLayout(lib_page)
        lp.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        llay = QVBoxLayout(left)
        llay.setContentsMargins(4, 4, 4, 4)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter…")
        self.filter_edit.setToolTip(
            "Type to filter the catalog below by name")
        llay.addWidget(self.filter_edit)
        self.tree = _LibraryTree()
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        llay.addWidget(self.tree)
        split.addWidget(left)
        self.detail = DetailPanel(self)
        split.addWidget(self.detail)
        split.setSizes([520, 980])
        persist_splitter(self.cfg, "main_split", split)
        lp.addWidget(split)
        self.stack.addWidget(lib_page)
        self._lib_widgets.append(lib_page)

        # pages 1-3
        self.search_pane = SearchPane(self)
        self.stack.addWidget(self.search_pane)
        self.changelog_pane = ChangelogPane(self)
        self.stack.addWidget(self.changelog_pane)
        self.settings_pane = SettingsPane(self)
        self.stack.addWidget(self.settings_pane)

        self.model = QStandardItemModel()
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setRecursiveFilteringEnabled(True)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.filter_edit.textChanged.connect(self.proxy.setFilterFixedString)
        self.tree.setModel(self.proxy)
        self.tree.selectionModel().selectionChanged.connect(self._selection_changed)

        persist_header(self.cfg, "main_tree", self.tree.header())
        self._reload_lib_combo()
        self.lib_combo.currentTextChanged.connect(self._on_lib_changed)
        self._update_path_label()
        self._update_lib_enabled()
        self.refresh_tree()
        self.statusBar().showMessage(
            "Ready." if self.lib else
            "No library yet — click 'Libraries…' and add one.")

        # auto-update: quietly ask GitHub for a newer version once the window
        # is up; a popup appears only when there is one (Settings — Updates)
        if self.cfg["settings"].get("auto_update_check", True):
            QTimer.singleShot(2000, self._startup_update_check)

    # ------------------------------------------------------------ updates ---

    def _startup_update_check(self):
        self._upd_check = UpdateCheckThread(self)
        self._upd_check.done.connect(self._startup_update_result)
        self._upd_check.start()

    def _startup_update_result(self, result):
        # the startup check is silent unless there really is a new version
        if result.get("error") or not result.get("update"):
            return
        if result["version"] == self.cfg["settings"].get("skipped_version", ""):
            return
        dlg = UpdateDialog(result, self)
        dlg.exec()
        if dlg.choice == "update":
            run_update_flow(self, result)
        elif dlg.choice == "skip":
            self.cfg["settings"]["skipped_version"] = result["version"]
            save_config(self.cfg)

    def _update_lib_enabled(self):
        """Gray out the library-dependent parts while no library exists."""
        on = self.lib is not None
        for w in self._lib_widgets:
            w.setEnabled(on)
        self.lib_combo.setEnabled(on)

    def show_images(self):
        return self.images_cb.isChecked()

    def _image_excludes(self):
        return None if self.show_images() else IMAGE_RULES

    def _images_toggled(self, on):
        self.cfg["settings"]["show_image_problems"] = on
        save_config(self.cfg)
        self.refresh_tree()
        self.detail.refresh()

    def _exceptions_only(self):
        return self.sev_combo.currentIndex() == 4

    def _missing_only(self):
        return self.sev_combo.currentIndex() == 3

    # ------------------------------------------------------------- helpers ---

    def switch_page(self, idx):
        cur = self.stack.currentIndex()
        if cur == 3 and idx != 3 and self.settings_pane.is_dirty():
            r = QMessageBox.question(
                self, "Unsaved settings",
                "You changed settings without saving. Save them now?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                self.page_btns[3].setChecked(True)
                return
            if r == QMessageBox.Save:
                self.settings_pane.save()
            else:
                self._rebuild_settings_pane()
        self.stack.setCurrentIndex(idx)
        self.page_btns[idx].setChecked(True)
        # library-specific controls only make sense on the Library page;
        # the Library selector itself stays (Search/Change log follow it)
        on_lib = idx == 0
        self._actions_grp.setVisible(on_lib)
        self._internet_grp.setVisible(on_lib)
        self.sort_bar.setVisible(on_lib)
        if idx == 2:
            self.changelog_pane.reload()

    def _rebuild_settings_pane(self):
        """Discard unsaved edits by recreating the pane from the saved config."""
        old = self.settings_pane
        pos = self.stack.indexOf(old)
        self.settings_pane = SettingsPane(self)
        self.stack.removeWidget(old)
        old.deleteLater()
        self.stack.insertWidget(pos, self.settings_pane)

    def rebuild_search_pane(self):
        """Recreate the search page (e.g. after the field-name set changed)."""
        old = self.search_pane
        pos = self.stack.indexOf(old)
        self.search_pane = SearchPane(self)
        self.stack.removeWidget(old)
        old.deleteLater()
        self.stack.insertWidget(pos, self.search_pane)

    def open_track_from_search(self, track_id):
        self.switch_page(0)
        self.detail.show_track(track_id)

    def online_filter(self):
        """Set for db.open_proposals: which internet proposal kinds are visible."""
        kinds = set()
        if self.online_add_cb.isChecked():
            kinds.add("add")
        if self.online_diff_cb.isChecked():
            kinds.add("diff")
        return kinds

    def _online_toggled(self, _on):
        self.cfg["settings"]["show_online_add"] = self.online_add_cb.isChecked()
        self.cfg["settings"]["show_online_diff"] = self.online_diff_cb.isChecked()
        save_config(self.cfg)
        self.refresh_tree()
        self.detail.refresh()

    def clear_online(self):
        n = self.con.execute(
            "SELECT COUNT(*) FROM proposals WHERE rule='online_meta'"
            " AND status IN ('pending','edited','postponed','needs_input')"
        ).fetchone()[0]
        if not n:
            QMessageBox.information(self, "Nothing to clear",
                                    "There are no unapplied internet proposals.")
            return
        if QMessageBox.question(
                self, "Clear internet proposals",
                "Delete all %d unapplied internet-metadata proposals?" % n
        ) != QMessageBox.Yes:
            return
        self.con.execute(
            "DELETE FROM proposals WHERE rule='online_meta'"
            " AND status IN ('pending','edited','postponed','needs_input')")
        self.con.commit()
        self.refresh_tree()
        self.detail.refresh()

    def _update_path_label(self):
        self.path_label.setText(
            "root: %s" % (self.lib["root"] if self.lib else "(no library)"))

    def _reload_lib_combo(self):
        self.lib_combo.blockSignals(True)
        self.lib_combo.clear()
        self.lib_combo.addItems([lib["name"] for lib in self.cfg["libraries"]])
        if self.lib:
            self.lib_combo.setCurrentText(self.lib["name"])
        self.lib_combo.blockSignals(False)

    def _on_lib_changed(self, name):
        if not name or (self.lib and name == self.lib["name"]):
            return
        if self.worker is not None:
            QMessageBox.information(self, "Busy",
                                    "Wait for the running operation to finish.")
            self._reload_lib_combo()
            return
        self.cfg["active_library"] = name
        save_config(self.cfg)
        self.lib = active_library(self.cfg)
        self.con.close()
        self.con = db.connect(lib_db_path(self.lib) if self.lib else ":memory:")
        self.detail.show_nothing()
        self._update_path_label()
        self._update_lib_enabled()
        self.refresh_tree()
        self.changelog_pane.reload()
        self.statusBar().showMessage("Switched to library '%s'." % name)

    def manage_libraries(self):
        dlg = LibrariesDialog(self.cfg, self)
        dlg.exec()
        names = [x["name"] for x in self.cfg["libraries"]]
        if dlg.added_name in names:
            # a freshly created library becomes active right away
            self.cfg["active_library"] = dlg.added_name
        if self.cfg.get("active_library", "") not in names:
            self.cfg["active_library"] = names[0] if names else ""
        save_config(self.cfg)
        old_db = lib_db_path(self.lib) if self.lib else None
        self.lib = active_library(self.cfg)
        new_db = lib_db_path(self.lib) if self.lib else None
        self._reload_lib_combo()
        if new_db != old_db:
            self.con.close()
            self.con = db.connect(new_db if new_db else ":memory:")
            self.detail.show_nothing()
            self.refresh_tree()
        self._update_path_label()
        self._update_lib_enabled()
        # straight into scanning the newly added library
        if (dlg.added_name and self.lib
                and self.lib["name"] == dlg.added_name):
            self.start_scan()

    def album_display(self, adir):
        root = self.lib.get("root", "")
        try:
            rel = Path(adir).relative_to(root)
            return str(Path(*rel.parts[1:])) if len(rel.parts) > 1 else rel.parts[0]
        except (ValueError, IndexError):
            return Path(adir).name

    def _min_rank(self):
        # All=0, Red+yellow=yellow(2), Red only=red(3), Missing/Exceptions=0
        return {0: 0, 1: 2, 2: 3, 3: 0, 4: 0}[self.sev_combo.currentIndex()]

    def _exception_scopes(self):
        """(artists_with_any_exception, albums_with_exceptions,
        artists_with_artist_level_exceptions)."""
        arts, albs, art_level = set(), set(), set()
        for a, d in self.con.execute(
                "SELECT artist_folder, album_dir FROM exceptions"):
            arts.add(a)
            if d:
                albs.add(d)
            else:
                art_level.add(a)
        return arts, albs, art_level

    @staticmethod
    def _item(text, kind=None, key=None, sev=None, editable=False):
        it = QStandardItem(text)
        it.setEditable(editable)
        if kind:
            it.setData(kind, KIND_ROLE)
            it.setData(key, KEY_ROLE)
        if sev is not None or kind:
            it.setIcon(dot_icon(sev))
        return it

    # ---------------------------------------------------------------- tree ---

    def refresh_tree(self, *_):
        expanded_keys = self._expanded_keys()
        selected_keys = self._selected_keys()
        sm = self.tree.selectionModel()
        if sm is not None:
            sm.blockSignals(True)   # a rebuild is not a user (de)selection
        try:
            self.model.clear()
            if self.artist_btn.isChecked():
                self.model.setHorizontalHeaderLabels(["Library", "Open proposals"])
                self._build_artist_tree()
            else:
                self.model.setHorizontalHeaderLabels(["Change / problem type", "Count"])
                self._build_changes_tree()
            self.tree.setColumnWidth(0, 380)
            self._restore_expanded(expanded_keys)
            self._restore_selection(selected_keys)
        finally:
            if sm is not None:
                sm.blockSignals(False)

    def _selected_keys(self):
        sm = self.tree.selectionModel()
        if sm is None:
            return []
        out = []
        for pidx in sm.selectedRows(0):
            idx = self.proxy.mapToSource(pidx)
            out.append((idx.data(KIND_ROLE), idx.data(KEY_ROLE)))
        return out

    def _restore_selection(self, keys):
        if not keys:
            return
        from PySide6.QtCore import QItemSelectionModel
        sm = self.tree.selectionModel()
        wanted = set(keys)

        def walk(parent):
            for r in range(self.model.rowCount(parent)):
                idx = self.model.index(r, 0, parent)
                if (idx.data(KIND_ROLE), idx.data(KEY_ROLE)) in wanted:
                    sm.select(self.proxy.mapFromSource(idx),
                              QItemSelectionModel.Select
                              | QItemSelectionModel.Rows)
                walk(idx)
        walk(self.model.invisibleRootItem().index())

    def _severities(self):
        alb_sev, art_sev, trk_sev = {}, {}, {}
        live_tracks = {r[0] for r in self.con.execute(
            "SELECT id FROM tracks WHERE missing=0")}
        live_albums = {r[0] for r in self.con.execute(
            "SELECT DISTINCT album_dir FROM tracks WHERE missing=0")}
        extra = "" if self.show_images() else \
            " WHERE rule NOT IN (%s)" % ",".join("'%s'" % r for r in IMAGE_RULES)
        for tid, artist, adir, sev in self.con.execute(
                "SELECT track_id, artist_folder, album_dir, severity FROM issues"
                + extra):
            if tid is not None and tid not in live_tracks:
                continue        # leftover entry of a renamed/deleted file
            if adir is not None and adir not in live_albums:
                continue
            if tid is not None:
                trk_sev[tid] = worse(trk_sev.get(tid), sev)
            if adir is not None:
                alb_sev[adir] = worse(alb_sev.get(adir), sev)
            art_sev[artist] = worse(art_sev.get(artist), sev)
        # files missing on disk -> gray, propagated up to album and artist
        for tid, artist, adir in self.con.execute(
                "SELECT id, artist_folder, album_dir FROM tracks WHERE missing=1"):
            trk_sev[tid] = worse(trk_sev.get(tid), "gray")
            alb_sev[adir] = worse(alb_sev.get(adir), "gray")
            art_sev[artist] = worse(art_sev.get(artist), "gray")
        return alb_sev, art_sev, trk_sev

    def _open_counts(self):
        alb_open, art_open = {}, {}
        extra = db.online_condition(self.online_filter()) \
            + db.rules_condition(self._image_excludes())
        for artist, adir, n in self.con.execute(
                "SELECT artist_folder, album_dir, COUNT(*) FROM proposals"
                " WHERE status IN ('pending','edited')"
                " AND (track_id IS NULL OR track_id IN"
                "      (SELECT id FROM tracks WHERE missing=0))"
                " AND (album_dir IS NULL OR album_dir IN"
                "      (SELECT DISTINCT album_dir FROM tracks WHERE missing=0))"
                + extra + " GROUP BY artist_folder, album_dir"):
            if adir:
                alb_open[adir] = alb_open.get(adir, 0) + n
            art_open[artist] = art_open.get(artist, 0) + n
        return alb_open, art_open

    def _build_artist_tree(self):
        alb_sev, art_sev, trk_sev = self._severities()
        alb_open, art_open = self._open_counts()
        min_rank = self._min_rank()
        exc_only = self._exceptions_only()
        exc_arts, exc_albs, exc_art_level = (
            self._exception_scopes() if exc_only else (set(), set(), set()))
        # missing (gray) entries are shown too, so a deleted file no longer just
        # vanishes; the "Missing files" filter narrows to exactly those
        missing_only = self._missing_only()
        miss_tids = {r[0] for r in self.con.execute(
            "SELECT id FROM tracks WHERE missing=1")}
        miss_albums = {r[0] for r in self.con.execute(
            "SELECT DISTINCT album_dir FROM tracks WHERE missing=1")}
        miss_artists = {r[0] for r in self.con.execute(
            "SELECT DISTINCT artist_folder FROM tracks WHERE missing=1")}
        root_item = self.model.invisibleRootItem()
        artists = [r[0] for r in self.con.execute(
            "SELECT DISTINCT artist_folder FROM tracks"
            " ORDER BY artist_folder COLLATE NOCASE")]
        for artist in artists:
            if exc_only and artist not in exc_arts:
                continue
            if missing_only and artist not in miss_artists:
                continue
            if SEV_RANK.get(art_sev.get(artist), 0) < min_rank:
                continue
            a_item = self._item(artist, "artist", artist, art_sev.get(artist))
            a_n = self._item(str(art_open.get(artist, "") or ""))
            for (adir,) in self.con.execute(
                    "SELECT DISTINCT album_dir FROM tracks WHERE artist_folder=?"
                    " ORDER BY album_dir", (artist,)):
                if (exc_only and adir not in exc_albs
                        and artist not in exc_art_level):
                    continue
                if missing_only and adir not in miss_albums:
                    continue
                if SEV_RANK.get(alb_sev.get(adir), 0) < min_rank:
                    continue
                b_item = self._item(self.album_display(adir), "album", adir,
                                    alb_sev.get(adir))
                b_n = self._item(str(alb_open.get(adir, "") or ""))
                for tid, fname, miss in self.con.execute(
                        "SELECT id, filename, missing FROM tracks WHERE album_dir=?"
                        " ORDER BY filename", (adir,)):
                    if missing_only and tid not in miss_tids:
                        continue
                    if min_rank and SEV_RANK.get(trk_sev.get(tid), 0) < min_rank:
                        continue
                    label = (fname + "  — missing on disk") if miss else fname
                    b_item.appendRow([self._item(label, "track", tid,
                                                 trk_sev.get(tid)),
                                      self._item("")])
                a_item.appendRow([b_item, b_n])
            root_item.appendRow([a_item, a_n])

    def _build_changes_tree(self):
        """Top level = type of change/problem, children = albums, then tracks."""
        alb_sev, _art_sev, trk_sev = self._severities()
        min_rank = self._min_rank()
        exc_only = self._exceptions_only()
        _exc_arts, exc_albs, exc_art_level = (
            self._exception_scopes() if exc_only else (set(), set(), set()))
        fnames = dict(self.con.execute("SELECT id, filename FROM tracks"))
        # entries whose files no longer exist on disk (renamed/moved/deleted)
        # are leftovers waiting for cleanup — never shown as work to do
        live_tracks = {r[0] for r in self.con.execute(
            "SELECT id FROM tracks WHERE missing=0")}
        live_albums = {r[0] for r in self.con.execute(
            "SELECT DISTINCT album_dir FROM tracks WHERE missing=0")}
        # postponed / needs-input proposals stay visible here - only
        # exceptions disappear from the normal views
        all_props = db.open_proposals(self.con,
                                      statuses=db.ALL_OPEN_STATUSES,
                                      online_filter=self.online_filter(),
                                      exclude_rules=self._image_excludes())
        all_props = [p for p in all_props
                     if (p["track_id"] is None or p["track_id"] in live_tracks)
                     and (p["album_dir"] is None or p["album_dir"] in live_albums)]

        groups = {}
        for p in all_props:
            if p["rule"]:
                gk, label = ("rule", p["rule"]), rule_label(p["rule"])
            else:
                gk, label = (("fieldfb", p["field"]),
                             "Change field '%s'" % field_label(p["field"]))
            g = groups.setdefault(gk, {"label": label,
                                       "sev": RULE_SEVERITY.get(gk[1], "yellow"),
                                       "albums": defaultdict(list), "n": 0})
            g["albums"][p["album_dir"]].append(p["track_id"])
            g["n"] += 1

        prop_fields = {(p["track_id"], p["field"]) for p in all_props
                       if p["track_id"]}
        for tid, artist, adir, rule, sev in self.con.execute(
                "SELECT track_id, artist_folder, album_dir, rule, severity FROM issues"):
            if not is_non_fixable(rule):
                continue
            if rule in IMAGE_RULES and not self.show_images():
                continue
            if tid is not None and tid not in live_tracks:
                continue
            if adir is not None and adir not in live_albums:
                continue
            f = missing_field_of(rule)
            if f and (tid, f) in prop_fields:
                continue
            gk = ("issue", rule)
            g = groups.setdefault(gk, {"label": rule_label(rule),
                                       "sev": sev, "albums": defaultdict(list),
                                       "n": 0})
            g["albums"][adir].append(tid)
            g["n"] += 1

        root_item = self.model.invisibleRootItem()
        # STABLE order: severity, then the fixed workflow priority of the
        # type, then its label — never the open-item count, which would make
        # the whole tree shuffle every time something is applied
        order = sorted(groups.items(),
                       key=lambda kv: (-SEV_RANK.get(kv[1]["sev"], 0),
                                       rule_priority(kv[0][1]),
                                       kv[1]["label"].lower()))
        for (ckind, key), g in order:
            if SEV_RANK.get(g["sev"], 0) < min_rank:
                continue
            prefix = "FIX: " if ckind in ("rule", "fieldfb") else "CHECK: "
            t_item = self._item(prefix + g["label"], "ctype", (ckind, key, g["label"]),
                                g["sev"])
            desc = (rule_description(key) if ckind in ("rule", "issue") else
                    "Older proposals for field '%s' recorded without a rule."
                    % key)
            if desc:
                t_item.setToolTip(desc)
            t_n = self._item(str(g["n"]))
            for adir in sorted(g["albums"], key=lambda d: (d is None, d or "")):
                tids = [t for t in g["albums"][adir] if t is not None]
                if adir is None:
                    continue
                if exc_only and adir not in exc_albs:
                    continue
                artist = self.con.execute(
                    "SELECT artist_folder FROM tracks WHERE album_dir=? LIMIT 1",
                    (adir,)).fetchone()
                label = "%s — %s  (%d)" % (artist[0] if artist else "?",
                                           self.album_display(adir),
                                           len(g["albums"][adir]))
                b_item = self._item(label, "album", adir, alb_sev.get(adir))
                for tid in sorted(set(tids)):
                    b_item.appendRow([self._item(fnames.get(tid, "?"), "track", tid,
                                                 trk_sev.get(tid)),
                                      self._item("")])
                t_item.appendRow([b_item, self._item(str(len(g["albums"][adir])))])
            if None in g["albums"]:
                arts = self.con.execute(
                    "SELECT DISTINCT artist_folder FROM issues WHERE rule=?"
                    " AND album_dir IS NULL ORDER BY artist_folder", (key,)).fetchall()
                for (artist,) in arts:
                    if exc_only and artist not in exc_art_level:
                        continue
                    t_item.appendRow([self._item(artist, "artist", artist, g["sev"]),
                                      self._item("")])
            if exc_only and t_item.rowCount() == 0:
                continue
            root_item.appendRow([t_item, t_n])

    def _expanded_keys(self):
        keys = set()

        def walk(parent):
            for r in range(self.model.rowCount(parent)):
                idx = self.model.index(r, 0, parent)
                if self.tree.isExpanded(self.proxy.mapFromSource(idx)):
                    keys.add((idx.data(KIND_ROLE), idx.data(KEY_ROLE)))
                walk(idx)
        try:
            walk(self.model.invisibleRootItem().index())
        except Exception:
            pass
        return keys

    def _restore_expanded(self, keys):
        def walk(parent):
            for r in range(self.model.rowCount(parent)):
                idx = self.model.index(r, 0, parent)
                if (idx.data(KIND_ROLE), idx.data(KEY_ROLE)) in keys:
                    self.tree.setExpanded(self.proxy.mapFromSource(idx), True)
                walk(idx)
        walk(self.model.invisibleRootItem().index())

    def _selection_changed(self, *_):
        idxs = self.tree.selectionModel().selectedRows(0)
        if not idxs:
            # the right panel always mirrors the selection: nothing selected
            # on the left -> nothing shown on the right
            self.detail.show_nothing()
            return
        if len(idxs) > 1:
            artists, albums, tracks, ctypes = self._selected_scope()
            if ctypes and not (artists or albums or tracks):
                self.detail.show_ctypes(ctypes)
            elif artists and not albums and not tracks:
                self.detail.show_artists(artists)
            elif albums or artists or tracks:
                # mixed / multi-album selection: combined view of everything
                # relevant, filtered to the selected albums
                adirs = list(dict.fromkeys(albums))
                if artists:
                    qs = ",".join("?" * len(artists))
                    for (adir,) in self.con.execute(
                            "SELECT DISTINCT album_dir FROM tracks WHERE"
                            " artist_folder IN (%s) AND missing=0"
                            " ORDER BY album_dir" % qs, artists):
                        if adir not in adirs:
                            adirs.append(adir)
                if tracks:
                    qs = ",".join("?" * len(tracks))
                    for (adir,) in self.con.execute(
                            "SELECT DISTINCT album_dir FROM tracks"
                            " WHERE id IN (%s)" % qs, tracks):
                        if adir not in adirs:
                            adirs.append(adir)
                if adirs:
                    self.detail.show_albums(adirs)
            return
        idx = self.proxy.mapToSource(idxs[0])
        kind, key = idx.data(KIND_ROLE), idx.data(KEY_ROLE)
        if kind == "artist":
            self.detail.show_artist(key)
        elif kind == "album":
            self.detail.show_album(key)
        elif kind == "track":
            self.detail.show_track(key)
        elif kind == "ctype":
            self.detail.show_ctype(*key)

    def _selected_scope(self):
        artists, albums, tracks, ctypes = [], [], [], []
        for pidx in self.tree.selectionModel().selectedRows(0):
            idx = self.proxy.mapToSource(pidx)
            kind, key = idx.data(KIND_ROLE), idx.data(KEY_ROLE)
            if kind == "artist":
                artists.append(key)
            elif kind == "album":
                albums.append(key)
            elif kind == "track":
                tracks.append(key)
            elif kind == "ctype":
                ctypes.append(key)
        return artists, albums, tracks, ctypes

    # ---------------------------------------------------------------- scan ---

    def start_scan(self):
        if self.lib is None:
            QMessageBox.information(self, "No library",
                                    "Add a library first (Libraries… button).")
            return
        root = self.lib.get("root", "")
        if not root or not Path(root).is_dir():
            QMessageBox.warning(
                self, "No root folder",
                "The library '%s' has no reachable root folder.\n"
                "Set it via the Libraries… button." % self.lib["name"])
            return
        # selection in the tree -> default scan scope
        artists, albums, _t, _c = self._selected_scope()
        selected = set(artists)
        for adir in albums:
            row = self.con.execute(
                "SELECT artist_folder FROM tracks WHERE album_dir=? LIMIT 1",
                (adir,)).fetchone()
            if row:
                selected.add(row[0])
        dlg = ScanDialog(self.lib, selected=sorted(selected), parent=self)
        if not dlg.exec():
            return
        entries = dlg.entries
        if dlg.folders_txt_used:
            self.lib["folders_txt"] = dlg.folders_txt_used
        save_config(self.cfg)
        if QMessageBox.question(
                self, "Scan", "Scan %d folders under\n%s ?\n\n(Nothing is written"
                " to the MP3s during a scan.)" % (len(entries), root)) != QMessageBox.Yes:
            return

        settings = self.cfg["settings"]
        full = dlg.full_cb.isChecked()
        auto_remove = dlg.autoremove_cb.isChecked()
        self._run_worker(
            lambda con, prog: scanner.scan(con, settings, root, entries,
                                           progress=prog, full=full,
                                           auto_remove_gone=auto_remove),
            "Scanning…", self._scan_done)

    def remove_selected(self):
        """Remove the catalog selection from the library database (files on
        disk stay untouched; a new scan re-adds them)."""
        artists, albums, tracks, _ct = self._selected_scope()
        if not (artists or albums or tracks):
            QMessageBox.information(
                self, "Nothing selected",
                "Select artists, albums or tracks in the catalog first.")
            return
        parts = []
        if artists:
            parts.append("%d artist(s)" % len(artists))
        if albums:
            parts.append("%d album(s)" % len(albums))
        if tracks:
            parts.append("%d track(s)" % len(tracks))
        what = ", ".join(parts)
        if QMessageBox.question(
                self, "Remove from library",
                "Remove %s from the library?\n\nOnly the database entries are"
                " removed — the files on disk are NOT touched. Scanning the"
                " library again adds them back." % what) != QMessageBox.Yes:
            return
        db.remove_scope(self.con, artists or None, albums or None,
                        tracks or None)
        self.detail.show_nothing()
        self.refresh_tree()
        self.statusBar().showMessage("Removed %s from the library." % what)

    def rescan_scope(self, artist_folders=None, album_dirs=None):
        """Bottom-row 'Rescan': re-read the shown files from disk and re-run
        all checks — the same as 'Start check' limited to this scope. Always a
        FULL re-read (full=True) so newly tracked fields (e.g. APEv2 state) are
        picked up even when a file's size/mtime is unchanged; the scope is small
        (one album/artist), so the cost is negligible."""
        if self.lib is None:
            return
        root = self.lib.get("root", "")
        if not root or not Path(root).is_dir():
            QMessageBox.warning(self, "No root folder",
                                "The library root folder is not reachable.")
            return
        artists = set(artist_folders or [])
        for adir in album_dirs or []:
            row = self.con.execute(
                "SELECT artist_folder FROM tracks WHERE album_dir=? LIMIT 1",
                (adir,)).fetchone()
            if row:
                artists.add(row[0])
        if not artists:
            return
        settings = self.cfg["settings"]
        entries = sorted(artists)
        self._run_worker(
            lambda con, prog: scanner.scan(con, settings, root, entries,
                                           progress=prog, full=True),
            "Refreshing…", self._rescan_done)

    def _rescan_done(self, res):
        if "error" in res:
            QMessageBox.critical(self, "Refresh failed", res["error"])
            return
        # a quiet refresh stays quiet — the log window only opens when there
        # is something to report (problems or files that vanished from disk)
        if res.get("errors") or res.get("missing_folders") or res.get("gone"):
            ScanReportDialog(self.con, res, parent=self,
                             title="Refresh finished").exec()
        self.refresh_tree()
        self.detail.refresh()
        self.statusBar().showMessage(
            "Refreshed: %d file(s) checked, %d re-read from disk."
            % (res["files"], res["read"]))

    def _scan_done(self, res):
        if "error" in res:
            QMessageBox.critical(self, "Scan failed", res["error"])
            return
        ScanReportDialog(self.con, res, parent=self).exec()
        self.refresh_tree()

    # ------------------------------------------------------------- internet ---

    def start_online_check(self):
        artists, albums, _tracks, _ct = self._selected_scope()
        where, params = None, None
        if albums:
            adirs = list(albums)
        elif artists:
            qs = ",".join("?" * len(artists))
            adirs = [r[0] for r in self.con.execute(
                "SELECT DISTINCT album_dir FROM tracks WHERE artist_folder"
                " IN (%s) AND missing=0" % qs, artists)]
        else:
            QMessageBox.information(
                self, "Select scope",
                "Select artists or albums in the tree first — the internet"
                " check runs on the selection.")
            return
        if QMessageBox.question(
                self, "Internet check",
                "Look up %d album(s) on MusicBrainz?\n\nThis creates separate"
                " 'internet' proposals (~2 s per album, nothing is written"
                " to files)." % len(adirs)) != QMessageBox.Yes:
            return
        settings = self.cfg["settings"]

        def job(con, prog):
            out = {"matched": 0, "proposals": 0, "misses": []}
            for i, adir in enumerate(adirs):
                prog(i + 1, len(adirs), adir)
                try:
                    r = online.scan_album_online(con, settings, adir)
                except Exception as e:
                    out["misses"].append("%s — %s" % (Path(adir).name, e))
                    continue
                if r["matched"]:
                    out["matched"] += 1
                    out["proposals"] += r["proposals"]
                else:
                    out["misses"].append(Path(adir).name)
            return out

        self._run_worker(job, "Checking MusicBrainz…", self._online_done)

    def _online_done(self, res):
        if "error" in res:
            QMessageBox.critical(self, "Internet check failed", res["error"])
            return
        msg = ("Matched %d album(s), created %d internet proposal(s)."
               % (res["matched"], res["proposals"]))
        if res["misses"]:
            msg += "\n\nNot matched / errors (%d):\n  " % len(res["misses"]) \
                   + "\n  ".join(res["misses"][:12])
        QMessageBox.information(self, "Internet check finished", msg)
        self.refresh_tree()
        self.detail.refresh()

    # --------------------------------------------------------------- apply ---

    def apply_selected(self):
        artists, albums, tracks, ctypes = self._selected_scope()
        if ctypes and not (artists or albums or tracks):
            if len(ctypes) > 1:
                QMessageBox.information(
                    self, "One type at a time",
                    "Select a single change type to apply it everywhere,"
                    " or use its own Apply button in the panel.")
                return
            ckind, key, _label = ctypes[0]
            if ckind == "rule":
                self.apply_scope(rule=key)
            elif ckind == "fieldfb":
                self.apply_scope(field=key)
            else:
                QMessageBox.information(
                    self, "No automatic fix",
                    "This is a problem type without an automatic fix -"
                    " open the albums to handle it.")
            return
        if not (artists or albums or tracks):
            QMessageBox.information(self, "Nothing selected",
                                    "Select artists, albums or tracks in the tree first.")
            return
        self.apply_scope(artist_folders=artists or None,
                         album_dirs=albums or None,
                         track_ids=tracks or None)

    def apply_scope(self, artist_folders=None, album_dirs=None, track_ids=None,
                    rule=None, field=None, prop_ids=None):
        online_filter = self.online_filter()
        exclude_rules = self._image_excludes()
        props = db.open_proposals(self.con, artist_folders, album_dirs, track_ids,
                                  rule=rule, field=field,
                                  online_filter=online_filter,
                                  exclude_rules=exclude_rules,
                                  prop_ids=prop_ids)
        if not props:
            QMessageBox.information(self, "Nothing to apply",
                                    "No open proposals in the selection.")
            return
        n_tracks = len({p["track_id"] for p in props if p["track_id"] is not None})
        if QMessageBox.question(
                self, "Apply changes",
                "Write %d proposed change(s) into %d file(s)?\n\n"
                "Previous tag versions stay stored in the database"
                " and can be restored via History."
                % (len(props), n_tracks)) != QMessageBox.Yes:
            return
        settings = self.cfg["settings"]
        self._run_worker(
            lambda con, prog: applier.apply_proposals(
                con, settings, artist_folders, album_dirs, track_ids,
                rule=rule, field=field, online_filter=online_filter,
                exclude_rules=exclude_rules, prop_ids=prop_ids, progress=prog),
            "Writing tags…", self._apply_done)

    def _apply_done(self, res):
        if "error" in res:
            QMessageBox.critical(self, "Apply failed", res["error"])
        elif res["errors"]:
            QMessageBox.warning(
                self, "Applied with errors",
                "%d files written, %d field changes.\n\nErrors:\n  %s"
                % (res["files"], res["changes"],
                   "\n  ".join("%s: %s" % e for e in res["errors"][:10])))
        else:
            QMessageBox.information(
                self, "Applied",
                "%d files written, %d field changes.\nEverything is in the change log."
                % (res["files"], res["changes"]))
        self.refresh_tree()
        self.detail.refresh()

    # -------------------------------------------------------------- worker ---

    def _run_worker(self, fn, title, on_done):
        if self.worker is not None:
            QMessageBox.information(self, "Busy", "Another operation is running.")
            return
        self.progress_dlg = QProgressDialog(title, None, 0, 100, self)
        self.progress_dlg.setWindowModality(Qt.WindowModal)
        self.progress_dlg.setMinimumDuration(300)
        self.progress_dlg.setCancelButton(None)
        self.progress_dlg.setMinimumWidth(520)

        self.worker = Worker(fn, db_path=lib_db_path(self.lib))
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(lambda res: self._on_done(res, on_done))
        self.worker.start()

    def _on_progress(self, done, total, text):
        if total:
            self.progress_dlg.setMaximum(total)
            self.progress_dlg.setValue(done)
        self.progress_dlg.setLabelText(Path(text).name if "\\" in text else text)

    def _on_done(self, res, on_done):
        w = self.worker
        self.worker = None
        if w is not None:
            w.wait()          # let run() return fully before the QThread may be GC'd
            w.deleteLater()
        self.progress_dlg.close()
        on_done(res)

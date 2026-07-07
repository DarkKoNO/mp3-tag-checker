"""Right-hand detail panel: artist / album / track views with
current vs. proposed columns and inline editing of the proposed state."""

import json
from collections import Counter, defaultdict
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea,
    QSplitter, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

import os

from .. import applier, db, tagio
from ..rules import (IMAGE_RULES, RULE_SEVERITY, missing_field_of,
                     rule_description, rule_label)
from .common import (SEV_COLORS, SEV_RANK, STATUS_COLORS,
                     copy_button, enable_copy, field_label, join_vals,
                     persist_header, persist_splitter, sel_label, split_vals)
from .dialogs import (ArtistImageDialog, CoverSearchDialog, ExceptionsDialog,
                      HistoryDialog)

ALBUM_PRIMARY = ["album", "albumartist", "year", "genre"]
# fields that only make sense per track, never edited album-wide
ALBUM_EXCLUDE = {"title", "track"}
PSEUDO_LABEL = {"_id3v1": "old ID3v1 tag", "_version": "ID3 version",
                "folder_jpg": "folder.jpg", "cover": "album cover"}


def entry_tip(e):
    """Hover text for an entry row: its specific note (if any) followed by
    the general explanation of the problem type."""
    desc = rule_description(e.get("rule"))
    note = e.get("note")
    if note and desc:
        return "%s\n\n%s" % (note, desc)
    return note or desc


def _clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w:
            w.deleteLater()
        elif item.layout():
            _clear(item.layout())


class DetailPanel(QWidget):
    """Owner (MainWindow) provides con, cfg, and refresh_tree()."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.lay = QVBoxLayout(self)
        self.lay.setAlignment(Qt.AlignTop)
        self.current = None   # ('artist', x) | ('album', x) | ('track', id)
        self.show_nothing()

    @property
    def con(self):
        return self.owner.con

    @property
    def cfg(self):
        return self.owner.cfg

    def sep(self):
        return self.cfg["settings"]["multi_sep"]

    # ------------------------------------------------------------- views ---

    def show_nothing(self):
        _clear(self.lay)
        self.current = None
        hint = QLabel("Select an artist, album or track on the left.\n\n"
                      "Colors:  green = OK,  yellow = should be improved,"
                      "  red = real problem.")
        hint.setAlignment(Qt.AlignCenter)
        self.lay.addWidget(hint)

    def refresh(self):
        if self.current is None:
            return
        kind, key = self.current
        if kind == "artist":
            self.show_artist(key)
        elif kind == "artists":
            self.show_artists(list(key))
        elif kind == "album":
            self.show_album(key)
        elif kind == "albums":
            self.show_albums(list(key))
        elif kind == "ctype":
            self.show_ctype(*key)
        elif kind == "ctypes":
            self.show_ctypes(list(key))
        else:
            self.show_track(key)

    # ---- shared bottom-row buttons ----

    def _refresh_btn(self, artists=None, albums=None):
        b = QPushButton("Rescan")
        b.setToolTip("Re-read the shown files from disk and re-run all checks"
                     " — the same as 'Scan library' limited to what you see"
                     " here")
        b.clicked.connect(lambda: self.owner.rescan_scope(
            artist_folders=artists, album_dirs=albums))
        return b

    def _sel_action_btns(self):
        """Buttons acting on the rows selected in the current entries tree
        (the same actions are available via right-click). Returned in pairs:
        apply, postpone + remove postpone, exception."""
        apply_btn = QPushButton("Apply selected changes")
        apply_btn.setToolTip("Write only the proposal rows selected in the"
                             " list into the files. Postponed rows are never"
                             " applied — use 'Remove postpone' first.")
        apply_btn.clicked.connect(self._apply_selected)
        post_btn = QPushButton("Postpone selected")
        post_btn.setToolTip("Keep the selected proposals visible but skip them"
                            " when applying (the album stays yellow)")
        post_btn.setStyleSheet("color: %s; font-weight: bold;" % STATUS_COLORS["postponed"])
        post_btn.clicked.connect(self._postpone_selected)
        unpost_btn = QPushButton("Remove postpone")
        unpost_btn.setToolTip("Make the selected postponed proposals"
                              " applicable again")
        unpost_btn.setStyleSheet("color: %s; font-weight: bold;" % STATUS_COLORS["postponed"])
        unpost_btn.clicked.connect(self._restore_selected)
        exc_btn = QPushButton("Mark as exception")
        exc_btn.setToolTip("Permanently ignore the selected items: hidden and"
                           " never checked again (undo via Exceptions…)")
        exc_btn.setStyleSheet("color: %s; font-weight: bold;" % STATUS_COLORS["exception"])
        exc_btn.clicked.connect(self._except_selected)
        return [apply_btn, post_btn, unpost_btn, exc_btn]

    def _apply_selected(self):
        ids = [e["prop_id"] for e in self._selected_entries()
               if e["kind"] == "prop" and e["status"] in ("pending", "edited")]
        if not ids:
            QMessageBox.information(
                self, "Nothing to apply",
                "Select proposal rows first. Postponed rows are never"
                " applied — use 'Remove postpone' on them first.")
            return
        self.owner.apply_scope(prop_ids=ids)

    def _issues_list(self, rows):
        lst = QListWidget()
        for sev, msg in rows:
            it = QListWidgetItem(msg)
            it.setForeground(QColor(SEV_COLORS.get(sev, "#000")))
            lst.addItem(it)
        lst.setMaximumHeight(140)
        return lst

    # ---- change type ----

    def show_ctype(self, ckind, key, label):
        """Summary for a change/problem type: affected albums + apply-all button.
        ckind: 'rule' (fix proposals), 'fieldfb' (older proposals without rule),
        'issue' (problems without automatic fix)."""
        _clear(self.lay)
        self.current = ("ctype", (ckind, key, label))
        head_lbl = QLabel("<h2>%s</h2>" % label)
        desc = rule_description(key) if ckind in ("rule", "issue") else \
            "Older proposals for field '%s' recorded without a rule." % key
        if desc:
            head_lbl.setToolTip(desc)
        self.lay.addWidget(head_lbl)

        open_qs = ",".join("'%s'" % s for s in db.ALL_OPEN_STATUSES)
        if ckind == "rule":
            rows = self.con.execute(
                "SELECT artist_folder, album_dir, COUNT(*) FROM proposals"
                " WHERE status IN (%s) AND rule=?"
                " GROUP BY album_dir ORDER BY artist_folder, album_dir"
                % open_qs, (key,)).fetchall()
        elif ckind == "fieldfb":
            rows = self.con.execute(
                "SELECT artist_folder, album_dir, COUNT(*) FROM proposals"
                " WHERE status IN (%s) AND field=? AND rule IS NULL"
                " GROUP BY album_dir ORDER BY artist_folder, album_dir"
                % open_qs, (key,)).fetchall()
        else:
            rows = self.con.execute(
                "SELECT artist_folder, album_dir, COUNT(*) FROM issues WHERE rule=?"
                " GROUP BY artist_folder, album_dir ORDER BY artist_folder, album_dir",
                (key,)).fetchall()
        total = sum(r[2] for r in rows)
        kind_txt = ("proposed change(s)" if ckind in ("rule", "fieldfb")
                    else "problem(s), no automatic fix - handle per album")
        self.lay.addWidget(QLabel("%d %s in %d album(s)/artist(s)."
                                  % (total, kind_txt, len(rows))))

        # content first, action buttons at the bottom
        detailed = getattr(self, "_ctype_detailed", False)
        affected_albums = [adir for _a, adir, _n in rows if adir]
        if detailed:
            groups = []
            for artist_f, adir, _n in rows:
                if not adir:
                    continue
                fnames = dict(self.con.execute(
                    "SELECT id, filename FROM tracks WHERE album_dir=?", (adir,)))
                entries = [e for e in self._collect_album_entries(adir, fnames)
                           if e["rule"] == key
                           or (ckind == "fieldfb" and e.get("field") == key)]
                groups.append(("%s — %s" % (artist_f, self.owner.album_display(adir)),
                               entries))
            self.lay.addWidget(self._detail_entries_tree(groups), 1)
            self.lay.addWidget(QLabel(
                "<i>Double-click a row to open its album; right-click selected"
                " rows for postpone / exception actions.</i>"))
        else:
            table = QTableWidget(len(rows), 3)
            table.setHorizontalHeaderLabels(["Artist", "Album", "Count"])
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            self._ctype_rows = rows
            for r, (artist, adir, n) in enumerate(rows):
                table.setItem(r, 0, QTableWidgetItem(artist or ""))
                table.setItem(r, 1, QTableWidgetItem(
                    self.owner.album_display(adir) if adir else "«artist level»"))
                table.setItem(r, 2, QTableWidgetItem(str(n)))
            table.cellDoubleClicked.connect(self._ctype_open_album)
            table.resizeColumnsToContents()
            enable_copy(table)
            persist_header(self.cfg, "ctype_albums", table.horizontalHeader())
            self.lay.addWidget(table, 1)
            self.lay.addWidget(QLabel("<i>Double-click a row to open the album.</i>"))

        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Actions:</b>"))
        if ckind in ("rule", "fieldfb"):
            btn = QPushButton("Apply ALL '%s' changes everywhere (%d)" % (label, total))
            btn.setToolTip("Write this type of change into every affected file"
                           " in the whole library")
            if ckind == "rule":
                btn.clicked.connect(lambda: self.owner.apply_scope(rule=key))
            else:
                btn.clicked.connect(lambda: self.owner.apply_scope(field=key))
            row.addWidget(btn)
        row.addWidget(self._refresh_btn(albums=affected_albums))
        det_btn = QPushButton("Show album summary" if detailed
                              else "Show all changes")
        det_btn.setToolTip("Switch between the per-album summary and the full"
                           " list of individual changes")
        det_btn.setCheckable(True)
        det_btn.setChecked(detailed)
        det_btn.toggled.connect(self._toggle_ctype_detailed)
        row.addWidget(det_btn)
        if detailed:
            for b in self._sel_action_btns():
                row.addWidget(b)
        row.addStretch(1)
        self.lay.addLayout(row)

    def _ctype_open_album(self, row, _col):
        artist, adir, _n = self._ctype_rows[row]
        if adir:
            self.show_album(adir)
        else:
            self.show_artist(artist)

    def show_ctypes(self, ctypes):
        """Combined view for a multi-selection of change/problem types:
        all their changes grouped by artist — album."""
        _clear(self.lay)
        self.current = ("ctypes", tuple(ctypes))
        labels = [label for _ck, _key, label in ctypes]
        head = QLabel("<h2>%d change types selected</h2>" % len(ctypes))
        head.setToolTip("\n".join(labels))
        self.lay.addWidget(head)
        self.lay.addWidget(QLabel(", ".join(labels)))

        rule_keys = {key for ckind, key, _l in ctypes if ckind in ("rule", "issue")}
        field_keys = {"field:%s" % key for ckind, key, _l in ctypes
                      if ckind == "fieldfb"}
        open_qs = ",".join("'%s'" % s for s in db.ALL_OPEN_STATUSES)
        pairs = set()
        if rule_keys:
            qs = ",".join("?" * len(rule_keys))
            pairs |= set(self.con.execute(
                "SELECT DISTINCT artist_folder, album_dir FROM proposals"
                " WHERE status IN (%s) AND rule IN (%s)" % (open_qs, qs),
                list(rule_keys)))
            pairs |= set(self.con.execute(
                "SELECT DISTINCT artist_folder, album_dir FROM issues"
                " WHERE rule IN (%s)" % qs, list(rule_keys)))
        for ckind, key, _l in ctypes:
            if ckind == "fieldfb":
                pairs |= set(self.con.execute(
                    "SELECT DISTINCT artist_folder, album_dir FROM proposals"
                    " WHERE status IN (%s) AND field=? AND rule IS NULL"
                    % open_qs, (key,)))
        wanted = rule_keys | field_keys
        groups = []
        adirs = []
        for artist_f, adir in sorted(pairs, key=lambda p: (p[0] or "",
                                                           p[1] or "")):
            if not adir:
                continue
            adirs.append(adir)
            fnames = dict(self.con.execute(
                "SELECT id, filename FROM tracks WHERE album_dir=?", (adir,)))
            entries = [e for e in self._collect_album_entries(adir, fnames)
                       if e["rule"] in wanted]
            groups.append(("%s — %s" % (artist_f or "?",
                                        self.owner.album_display(adir)),
                           entries))
        self.lay.addWidget(self._detail_entries_tree(groups), 1)
        self.lay.addWidget(QLabel(
            "<i>Double-click a row to open its album; right-click selected"
            " rows for postpone / exception actions.</i>"))

        btns = QHBoxLayout()
        btns.addWidget(QLabel("<b>Actions:</b>"))
        for b in self._sel_action_btns():
            btns.addWidget(b)
        btns.addWidget(self._refresh_btn(albums=adirs))
        btns.addStretch(1)
        self.lay.addLayout(btns)

    # ---- shared read-only detailed list (artist view / change-type view) ----

    def _detail_entries_tree(self, groups):
        """Read-only tree of entries grouped under labels; double-click a row
        to open its album, right-click for postpone/exception actions."""
        bold = QFont()
        bold.setBold(True)
        tree = QTreeWidget()
        tree.setColumnCount(6)
        tree.setHeaderLabels(["Where / file", "Problem / change", "Field",
                              "Current", "Proposed", "Source"])
        tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        tree.setContextMenuPolicy(Qt.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._entry_menu)
        # selection-based actions (postpone/exception/restore) work here too
        self.entry_tree = tree
        enable_copy(tree)
        for label, entries in groups:
            if not entries:
                continue
            worst = max(entries, key=lambda e: SEV_RANK.get(e["sev"], 0))["sev"]
            top = QTreeWidgetItem(["%s   (%d)" % (label, len(entries))])
            top.setFont(0, bold)
            top.setForeground(0, QColor(SEV_COLORS.get(worst, "#000")))
            top.setFirstColumnSpanned(True)
            for e in sorted(entries,
                            key=lambda e: (e["file"], e.get("field") or "")):
                if e["kind"] == "prop":
                    field_lbl = PSEUDO_LABEL.get(e["field"],
                                                 field_label(e["field"]))
                    cur_txt = join_vals(e["current"], self.sep())
                    proposed_txt = join_vals(e["proposed"], self.sep())
                    if e.get("rule") == "id3v1_conflict":
                        cur_txt += "   (ID3v2 — current)"
                        proposed_txt += "   (old ID3v1)"
                    src = e["source"]
                    if e["status"] == "postponed":
                        src += " · postponed"
                    elif e["status"] == "needs_input":
                        proposed_txt = "‹fill in manually›"
                        src += " · needs input"
                    item = QTreeWidgetItem(
                        [e["file"], e["label"], field_lbl, cur_txt,
                         proposed_txt, src])
                    if e["status"] == "postponed":
                        for c in range(6):
                            item.setForeground(c, QColor(STATUS_COLORS["postponed"]))
                else:
                    item = QTreeWidgetItem(
                        ["%s · %s" % (e["file"], e["message"]), e["label"],
                         "", "", "", ""])
                    item.setForeground(0, QColor(SEV_COLORS.get(e["sev"], "#000")))
                tip = entry_tip(e)
                if tip:
                    for c in range(6):
                        item.setToolTip(c, tip)
                item.setData(0, Qt.UserRole, e)
                top.addChild(item)
            tree.addTopLevelItem(top)
        tree.expandAll()
        for c in range(6):
            tree.resizeColumnToContents(c)
        tree.itemDoubleClicked.connect(self._detail_tree_open)
        persist_header(self.cfg, "detail_entries", tree.header())
        return tree

    def _detail_tree_open(self, item, _col):
        e = item.data(0, Qt.UserRole)
        if e and e.get("album_dir"):
            self.show_album(e["album_dir"])

    def _toggle_artist_detailed(self, on):
        self._artist_detailed = on
        self.refresh()

    def _toggle_ctype_detailed(self, on):
        self._ctype_detailed = on
        self.refresh()

    # ---- artist ----

    def show_artist(self, artist):
        _clear(self.lay)
        self.current = ("artist", artist)
        head_row = QHBoxLayout()
        head_row.addWidget(sel_label("<h2>%s</h2>" % artist))
        head_row.addWidget(copy_button(lambda a=artist: a, "Copy artist name"))
        head_row.addStretch(1)
        self.lay.addLayout(head_row)

        n_open = self.con.execute(
            "SELECT COUNT(*) FROM proposals WHERE artist_folder=?"
            " AND status IN ('pending','edited')", (artist,)).fetchone()[0]
        issues = [(sev, msg) for sev, msg, rule in self.con.execute(
            "SELECT severity, message, rule FROM issues WHERE artist_folder=?"
            " AND album_dir IS NULL", (artist,))
            if self.owner.show_images() or rule not in IMAGE_RULES]
        info = QLabel("%d open proposal(s) for this artist." % n_open)
        self.lay.addWidget(info)
        if issues:
            self.lay.addWidget(self._issues_list(issues))

        # content first, action buttons at the bottom
        detailed = getattr(self, "_artist_detailed", False)
        albums = self.con.execute(
            "SELECT album_dir, COUNT(*) FROM tracks WHERE artist_folder=? AND missing=0"
            " GROUP BY album_dir ORDER BY album_dir", (artist,)).fetchall()
        if detailed:
            groups = []
            for adir, _n in albums:
                fnames = dict(self.con.execute(
                    "SELECT id, filename FROM tracks WHERE album_dir=?", (adir,)))
                groups.append((self.owner.album_display(adir),
                               self._collect_album_entries(adir, fnames)))
            self.lay.addWidget(self._detail_entries_tree(groups), 1)
            self.lay.addWidget(QLabel(
                "<i>Double-click a row to open its album; right-click selected"
                " rows for postpone / exception actions.</i>"))
        else:
            table = QTableWidget(len(albums), 3)
            table.setHorizontalHeaderLabels(["Album folder", "Tracks",
                                             "Open proposals"])
            table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            table.setSelectionBehavior(QAbstractItemView.SelectRows)
            adirs = [a for a, _n in albums]
            for r, (adir, n) in enumerate(albums):
                table.setItem(r, 0, QTableWidgetItem(self.owner.album_display(adir)))
                table.setItem(r, 1, QTableWidgetItem(str(n)))
                no = self.con.execute(
                    "SELECT COUNT(*) FROM proposals WHERE album_dir=?"
                    " AND status IN ('pending','edited')", (adir,)).fetchone()[0]
                table.setItem(r, 2, QTableWidgetItem(str(no)))
            table.cellDoubleClicked.connect(
                lambda r, _c, dirs=adirs: self.show_album(dirs[r]))
            table.resizeColumnsToContents()
            enable_copy(table)
            persist_header(self.cfg, "artist_albums", table.horizontalHeader())
            self.lay.addWidget(table, 1)
            self.lay.addWidget(QLabel(
                "<i>Double-click an album to open its detail.</i>"))

        btns = QHBoxLayout()
        btns.addWidget(QLabel("<b>Actions:</b>"))
        apply_btn = QPushButton("Apply ALL proposals for this artist (%d)" % n_open)
        apply_btn.setToolTip("Write every open proposed change of this artist"
                             " into the MP3 files")
        apply_btn.setEnabled(n_open > 0)
        apply_btn.clicked.connect(
            lambda: self.owner.apply_scope(artist_folders=[artist]))
        btns.addWidget(apply_btn)
        btns.addWidget(self._refresh_btn(artists=[artist]))
        img_btn = QPushButton("Find artist image…")
        img_btn.setToolTip("Search online (or pick from disk) an artist.jpg"
                           " for this artist folder")
        img_btn.clicked.connect(lambda: self._artist_image(artist))
        btns.addWidget(img_btn)
        det_btn = QPushButton("Show album summary" if detailed
                              else "Show all changes")
        det_btn.setToolTip("Switch between the album summary and the full"
                           " list of individual changes")
        det_btn.setCheckable(True)
        det_btn.setChecked(detailed)
        det_btn.toggled.connect(self._toggle_artist_detailed)
        btns.addWidget(det_btn)
        if detailed:
            for b in self._sel_action_btns():
                btns.addWidget(b)
        btns.addStretch(1)
        self.lay.addLayout(btns)

    def show_artists(self, artists):
        """Combined view for a multi-selection of artists."""
        _clear(self.lay)
        self.current = ("artists", tuple(artists))
        self.lay.addWidget(QLabel("<h2>%d artists selected</h2>" % len(artists)))
        qs = ",".join("?" * len(artists))
        n_open = self.con.execute(
            "SELECT COUNT(*) FROM proposals WHERE artist_folder IN (%s)"
            " AND status IN ('pending','edited')" % qs, artists).fetchone()[0]
        self.lay.addWidget(QLabel("%d open proposal(s) across the selection."
                                  % n_open))
        albums = self.con.execute(
            "SELECT artist_folder, album_dir, COUNT(*) FROM tracks"
            " WHERE artist_folder IN (%s) AND missing=0"
            " GROUP BY album_dir ORDER BY artist_folder, album_dir" % qs,
            artists).fetchall()
        # same two modes as the single-artist view: album summary, or the
        # full list of individual changes (artist — album — changes)
        detailed = getattr(self, "_artists_detailed", False)
        if detailed:
            groups = []
            for artist_f, adir, _n in albums:
                fnames = dict(self.con.execute(
                    "SELECT id, filename FROM tracks WHERE album_dir=?", (adir,)))
                groups.append(("%s — %s" % (artist_f,
                                            self.owner.album_display(adir)),
                               self._collect_album_entries(adir, fnames)))
            self.lay.addWidget(self._detail_entries_tree(groups), 1)
            self.lay.addWidget(QLabel(
                "<i>Double-click a row to open its album; right-click selected"
                " rows for postpone / exception actions.</i>"))
        else:
            table = QTableWidget(len(albums), 4)
            table.setHorizontalHeaderLabels(
                ["Artist", "Album folder", "Tracks", "Open proposals"])
            table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            table.setSelectionBehavior(QAbstractItemView.SelectRows)
            rows = [(artist, adir) for artist, adir, _n in albums]
            for r, (artist, adir, n) in enumerate(albums):
                table.setItem(r, 0, QTableWidgetItem(artist))
                table.setItem(r, 1, QTableWidgetItem(self.owner.album_display(adir)))
                table.setItem(r, 2, QTableWidgetItem(str(n)))
                no = self.con.execute(
                    "SELECT COUNT(*) FROM proposals WHERE album_dir=?"
                    " AND status IN ('pending','edited')", (adir,)).fetchone()[0]
                table.setItem(r, 3, QTableWidgetItem(str(no)))

            def open_row(r, c, rows=rows):
                artist, adir = rows[r]
                if c == 0:
                    self.show_artist(artist)
                else:
                    self.show_album(adir)
            table.cellDoubleClicked.connect(open_row)
            table.resizeColumnsToContents()
            enable_copy(table)
            persist_header(self.cfg, "artists_albums", table.horizontalHeader())
            self.lay.addWidget(table, 1)
            self.lay.addWidget(QLabel(
                "<i>Double-click the artist column to view that artist,"
                " any other column to open the album.</i>"))

        btns = QHBoxLayout()
        btns.addWidget(QLabel("<b>Actions:</b>"))
        apply_btn = QPushButton("Apply ALL proposals for these %d artists (%d)"
                                % (len(artists), n_open))
        apply_btn.setToolTip("Write every open proposed change of the selected"
                             " artists into the MP3 files")
        apply_btn.setEnabled(n_open > 0)
        apply_btn.clicked.connect(
            lambda: self.owner.apply_scope(artist_folders=list(artists)))
        btns.addWidget(apply_btn)
        btns.addWidget(self._refresh_btn(artists=list(artists)))
        det_btn = QPushButton("Show album summary" if detailed
                              else "Show all changes")
        det_btn.setToolTip("Switch between the album summary and the full"
                           " list of individual changes")
        det_btn.setCheckable(True)
        det_btn.setChecked(detailed)
        det_btn.toggled.connect(self._toggle_artists_detailed)
        btns.addWidget(det_btn)
        if detailed:
            for b in self._sel_action_btns():
                btns.addWidget(b)
        btns.addStretch(1)
        self.lay.addLayout(btns)

    def _toggle_artists_detailed(self, on):
        self._artists_detailed = on
        self.refresh()

    def show_albums(self, adirs):
        """Combined view for a multi-selection of albums: all problems and
        proposed changes of the selected albums, like a filtered artist view."""
        _clear(self.lay)
        self.current = ("albums", tuple(adirs))
        self.lay.addWidget(QLabel("<h2>%d albums selected</h2>" % len(adirs)))
        qs = ",".join("?" * len(adirs))
        n_open = self.con.execute(
            "SELECT COUNT(*) FROM proposals WHERE album_dir IN (%s)"
            " AND status IN ('pending','edited')" % qs, list(adirs)).fetchone()[0]
        self.lay.addWidget(QLabel("%d open proposal(s) across the selection."
                                  % n_open))

        groups = []
        for adir in adirs:
            row = self.con.execute(
                "SELECT artist_folder FROM tracks WHERE album_dir=? LIMIT 1",
                (adir,)).fetchone()
            fnames = dict(self.con.execute(
                "SELECT id, filename FROM tracks WHERE album_dir=?", (adir,)))
            groups.append(("%s — %s" % (row[0] if row else "?",
                                        self.owner.album_display(adir)),
                           self._collect_album_entries(adir, fnames)))
        self.lay.addWidget(self._detail_entries_tree(groups), 1)
        self.lay.addWidget(QLabel(
            "<i>Double-click a row to open its album; right-click selected"
            " rows for postpone / exception actions.</i>"))

        btns = QHBoxLayout()
        btns.addWidget(QLabel("<b>Actions:</b>"))
        apply_btn = QPushButton("Apply ALL proposals for these %d albums (%d)"
                                % (len(adirs), n_open))
        apply_btn.setToolTip("Write every open proposed change of the selected"
                             " albums into the MP3 files")
        apply_btn.setEnabled(n_open > 0)
        apply_btn.clicked.connect(
            lambda: self.owner.apply_scope(album_dirs=list(adirs)))
        btns.addWidget(apply_btn)
        btns.addWidget(self._refresh_btn(albums=list(adirs)))
        for b in self._sel_action_btns():
            btns.addWidget(b)
        btns.addStretch(1)
        self.lay.addLayout(btns)

    def _artist_image(self, artist):
        # display name = most common artist tag value in this folder
        names = Counter()
        for (tid,) in self.con.execute(
                "SELECT id FROM tracks WHERE artist_folder=? AND missing=0 LIMIT 40",
                (artist,)):
            snap = db.latest_snapshot(self.con, tid)
            if snap:
                for a in snap["tags"].get("albumartist") or snap["tags"].get("artist", []):
                    names[a] += 1
        display = names.most_common(1)[0][0] if names else artist
        dlg = ArtistImageDialog(self.con, self.cfg["settings"], artist, display,
                                self.owner.lib["root"], self)
        dlg.exec()
        if dlg.saved:
            self.owner.refresh_tree()
            self.refresh()

    # ---- album ----

    def show_album(self, adir):
        _clear(self.lay)
        self.current = ("album", adir)
        artist_row = self.con.execute(
            "SELECT artist_folder FROM tracks WHERE album_dir=? LIMIT 1",
            (adir,)).fetchone()
        artist = artist_row[0] if artist_row else "?"

        tracks = self.con.execute(
            "SELECT id, filename, path FROM tracks WHERE album_dir=? AND missing=0"
            " ORDER BY filename", (adir,)).fetchall()
        snaps = {tid: (db.latest_snapshot(self.con, tid) or {}).get("tags", {})
                 for tid, _f, _p in tracks}

        top_widget = QWidget()
        head_row = QHBoxLayout(top_widget)
        head_row.setContentsMargins(0, 0, 0, 0)
        left_head = QVBoxLayout()
        album_name = self.owner.album_display(adir)
        title_row = QHBoxLayout()
        title_row.addWidget(sel_label(
            "<h2><span style='color:gray;'>Album:</span> %s</h2>" % album_name))
        title_row.addWidget(copy_button(lambda n=album_name: n, "Copy album name"))
        open_btn = QPushButton("Open folder")
        open_btn.setToolTip(adir)
        open_btn.clicked.connect(lambda: os.startfile(adir))
        title_row.addWidget(open_btn)
        title_row.addStretch(1)
        left_head.addLayout(title_row)
        artist_row = QHBoxLayout()
        artist_row.addWidget(sel_label("<i><span style='color:gray;'>Artist:"
                                       "</span> %s — %d tracks</i>"
                                       % (artist, len(tracks))))
        artist_row.addWidget(copy_button(lambda a=artist: a, "Copy artist name"))
        artist_row.addStretch(1)
        left_head.addLayout(artist_row)

        # album-level current values (uniform or «varies»); every extended field
        # appears once any track has a value or a proposal for it, or all of
        # them when 'Show all fields' is ticked (to fill unused ones)
        show_all = getattr(self, "_album_show_all", False)
        album_fields = list(ALBUM_PRIMARY)
        for field in tagio.EDITABLE_FIELDS:
            if field in ALBUM_PRIMARY or field in ALBUM_EXCLUDE:
                continue
            if show_all:
                album_fields.append(field)
                continue
            has_val = any(snaps[tid].get(field) for tid in snaps)
            has_prop = self.con.execute(
                "SELECT 1 FROM proposals WHERE album_dir=? AND field=?"
                " AND status IN ('pending','edited') LIMIT 1",
                (adir, field)).fetchone()
            if has_val or has_prop:
                album_fields.append(field)
        # album-level fields: table look (bold field name, read-only current),
        # but the Proposed column is a real input box — NOTHING is stored
        # until Enter is pressed or the row's Add button is clicked, so
        # half-written text (e.g. while copy-pasting several values) is safe
        bold = QFont()
        bold.setBold(True)
        self._album_prop_header = QLabel(self._PROP_HEADER_DEFAULT)
        self._album_dirty_fields = set()
        # unconfirmed edits saved by _album_field_edited before a rebuild;
        # consumed exactly once so a later, unrelated refresh starts clean
        pending_edits = getattr(self, "_album_pending_edits", {})
        self._album_pending_edits = {}
        left_head.addWidget(self._album_prop_header)
        table = QTableWidget(len(album_fields), 4)
        table.setHorizontalHeaderLabels(["Field", "Current", "Proposed", ""])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._album_field_rows = album_fields
        self._album_edits = {}
        for r, field in enumerate(album_fields):
            vals = {join_vals(snaps[tid].get(field, []), self.sep()) for tid in snaps}
            cur = vals.pop() if len(vals) == 1 else "«varies»"
            props = self.con.execute(
                "SELECT proposed FROM proposals WHERE album_dir=? AND field=?"
                " AND status IN ('pending','edited') AND track_id IS NOT NULL",
                (adir, field)).fetchall()
            pvals = {join_vals(json.loads(p[0]), self.sep()) for p in props}
            proposed = pvals.pop() if len(pvals) == 1 else ("«varies»" if pvals else "")
            it0 = QTableWidgetItem(field_label(field))
            it0.setFont(bold)
            it0.setFlags(it0.flags() & ~Qt.ItemIsEditable)
            it0.setToolTip("Technical tag name: %s" % field)
            it1 = QTableWidgetItem(cur or "—")
            it1.setFlags(it1.flags() & ~Qt.ItemIsEditable)
            table.setItem(r, 0, it0)
            table.setItem(r, 1, it1)
            edit = QLineEdit(proposed)
            edit.setPlaceholderText("(no change)")
            edit.setToolTip(
                "Write the new value, then press Enter or click Add — nothing"
                " is stored until you do. Confirming the current value (or an"
                " emptied box) removes the proposed change again.")
            edit.returnPressed.connect(
                lambda f=field, a=adir: self._album_field_edited(a, f))
            self._album_edits[field] = edit
            table.setCellWidget(r, 2, edit)
            add_btn = QPushButton("Add")
            add_btn.setToolTip("Save into the proposed changes"
                               " (same as pressing Enter)")
            add_btn.setVisible(False)
            add_btn.clicked.connect(
                lambda _c, f=field, a=adir: self._album_field_edited(a, f))
            edit.textEdited.connect(
                lambda _t, f=field, e=edit, b=add_btn, init=proposed:
                self._album_edit_changed(f, e, b, init))
            table.setCellWidget(r, 3, add_btn)
            pend = pending_edits.get(field)
            if pend is not None and pend.strip() != proposed.strip():
                edit.setText(pend)
                self._album_edit_changed(field, edit, add_btn, proposed)
        table.resizeColumnsToContents()
        table.setColumnWidth(2, max(260, table.columnWidth(2)))
        table.setColumnWidth(3, 60)
        enable_copy(table)
        persist_header(self.cfg, "album_fields", table.horizontalHeader())
        self.album_table = table
        left_head.addWidget(table, 1)
        all_cb = QCheckBox("Show all fields (fill unused ones: composer,"
                           " publisher, ISRC, …)")
        all_cb.setChecked(show_all)
        all_cb.toggled.connect(self._toggle_album_all_fields)
        left_head.addWidget(all_cb)
        head_row.addLayout(left_head, 1)

        # cover thumbnail from the first track
        cover_lbl = QLabel()
        cover_lbl.setFixedSize(160, 160)
        cover_lbl.setAlignment(Qt.AlignCenter)
        cover_lbl.setStyleSheet("border: 1px solid #999;")
        try:
            got = tagio.get_cover_data(tracks[0][2]) if tracks else None
            if got:
                pm = QPixmap()
                pm.loadFromData(got[1])
                cover_lbl.setPixmap(pm.scaled(158, 158, Qt.KeepAspectRatio,
                                              Qt.SmoothTransformation))
                cover_lbl.setToolTip("Embedded cover of the first track")
            else:
                cover_lbl.setText("no cover")
        except Exception:
            cover_lbl.setText("(unreadable)")
        head_row.addWidget(cover_lbl, 0, Qt.AlignTop)

        fname_by_id = {tid: f for tid, f, _p in tracks}
        entries = self._collect_album_entries(adir, fname_by_id)
        n_props = sum(1 for e in entries if e["kind"] == "prop")

        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Problems &amp; proposed changes (%d):</b>"
                              % len(entries)))
        mode = getattr(self, "_album_mode", "problem")
        for m, label in (("problem", "Group by problem"), ("song", "Group by song")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(mode == m)
            b.clicked.connect(lambda _c, m=m: self._set_album_mode(m))
            head.addWidget(b)
        head.addStretch(1)

        self.entry_tree = QTreeWidget()
        self.entry_tree.setColumnCount(5)
        self.entry_tree.setHeaderLabels(
            ["Problem / file", "Field", "Current",
             "Proposed (double-click to edit)", "Source"])
        self.entry_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.entry_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.entry_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.entry_tree.customContextMenuRequested.connect(self._entry_menu)
        enable_copy(self.entry_tree)
        self._fill_entry_tree(entries, mode)
        self.entry_tree.itemChanged.connect(self._entry_edited)
        self.entry_tree.itemDoubleClicked.connect(self._entry_double_clicked)

        bottom_widget = QWidget()
        bot_lay = QVBoxLayout(bottom_widget)
        bot_lay.setContentsMargins(0, 0, 0, 0)
        bot_lay.addLayout(head)
        bot_lay.addWidget(self.entry_tree)

        # user-adjustable ratio between the fields (top) and problems (bottom)
        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(top_widget)
        vsplit.addWidget(bottom_widget)
        vsplit.setStretchFactor(0, 0)
        vsplit.setStretchFactor(1, 1)
        persist_splitter(self.cfg, "album_vsplit", vsplit)
        self.lay.addWidget(vsplit, 1)

        n_apply = sum(1 for e in entries
                      if e["kind"] == "prop" and e["status"] in ("pending", "edited"))
        n_exc = self.con.execute(
            "SELECT COUNT(*) FROM exceptions WHERE album_dir=?", (adir,)).fetchone()[0]
        btns = QHBoxLayout()
        btns.addWidget(QLabel("<b>Actions:</b>"))
        apply_btn = QPushButton("Apply this album (%d)" % n_apply)
        apply_btn.setToolTip("Write all open proposed changes of this album"
                             " into the MP3 files")
        apply_btn.setEnabled(n_apply > 0)
        apply_btn.clicked.connect(lambda: self.owner.apply_scope(album_dirs=[adir]))
        btns.addWidget(apply_btn)
        sel_btns = self._sel_action_btns()
        btns.addWidget(sel_btns[0])                 # Apply selected changes
        btns.addWidget(self._refresh_btn(albums=[adir]))
        for b in sel_btns[1:]:                      # postpone pair + exception
            btns.addWidget(b)
        excl_btn = QPushButton("Exceptions… (%d)" % n_exc)
        excl_btn.setToolTip("List the permanently ignored items of this album;"
                            " remove one to check it again")
        excl_btn.setStyleSheet("color: %s;" % STATUS_COLORS["exception"])
        excl_btn.clicked.connect(lambda: self._exceptions(adir, artist))
        btns.addWidget(excl_btn)
        del_btn = QPushButton("Delete manual")
        del_btn.setToolTip("Delete the selected manual changes (Source = manual)")
        del_btn.clicked.connect(self._delete_selected)
        btns.addWidget(del_btn)
        hist_btn = QPushButton("History…")
        hist_btn.setToolTip("Browse earlier tag versions of this album and"
                            " revert to any of them")
        hist_btn.clicked.connect(lambda: self._history(adir))
        btns.addWidget(hist_btn)
        cover_btn = QPushButton("Find cover online…")
        cover_btn.setToolTip("Search MusicBrainz / Cover Art Archive (or pick"
                             " a file from disk) for a better album cover")
        cover_btn.clicked.connect(lambda: self._find_cover(adir, artist, snaps))
        btns.addWidget(cover_btn)
        btns.addStretch(1)
        self.lay.addLayout(btns)

    _PROP_HEADER_DEFAULT = ("<b>Proposed</b> — write into the boxes below,"
                            " then press Enter or click Add")

    @staticmethod
    def _prop_header_dirty():
        return ("<b style='color:%s;'>Unsaved edit — press Enter or click Add"
                " to put it into the proposed changes!</b>"
                % STATUS_COLORS["attention"])

    def _album_edit_changed(self, field, edit, btn, init):
        """Text typed but not confirmed yet: make the needed Enter/Add step
        clearly visible (highlighted header + highlighted Add button)."""
        dirty = edit.text().strip() != init.strip()
        btn.setVisible(dirty)
        btn.setStyleSheet("background-color: %s; color: white;"
                          " font-weight: bold;" % STATUS_COLORS["attention"]
                          if dirty else "")
        if dirty:
            self._album_dirty_fields.add(field)
        else:
            self._album_dirty_fields.discard(field)
        self._album_prop_header.setText(
            self._prop_header_dirty() if self._album_dirty_fields
            else self._PROP_HEADER_DEFAULT)

    def _album_field_edited(self, adir, field):
        """Enter / Add confirmed: the box's value becomes a manual proposal
        for every track. Confirming the current value removes the proposal
        again (set_manual_proposal deletes no-change proposals); confirming
        an emptied box removes any open proposal for the field."""
        text = self._album_edits[field].text().strip()
        if "«" in text:
            return
        if not text:
            open_qs = ",".join("'%s'" % s for s in db.ALL_OPEN_STATUSES)
            self.con.execute(
                "DELETE FROM proposals WHERE album_dir=? AND field=?"
                " AND status IN (%s)" % open_qs, (adir, field))
            self.con.commit()
        else:
            vals = split_vals(text, self.sep())
            for (tid,) in self.con.execute(
                    "SELECT id FROM tracks WHERE album_dir=? AND missing=0",
                    (adir,)):
                applier.set_manual_proposal(self.con, tid, field, vals)
        # refresh() rebuilds the whole table, which would wipe half-written
        # text in the OTHER rows - carry their unconfirmed edits over
        self._album_pending_edits = {
            f: self._album_edits[f].text()
            for f in self._album_dirty_fields if f != field}
        self.refresh()
        self.owner.refresh_tree()

    def _collect_album_entries(self, adir, fname_by_id):
        """Merge open proposals (all statuses) and issues into one list; issues
        already answered by a proposal are not repeated."""
        props = db.open_proposals(self.con, album_dirs=[adir],
                                  statuses=db.ALL_OPEN_STATUSES,
                                  online_filter=self.owner.online_filter())
        prop_fields = {(p["track_id"], p["field"]) for p in props if p["track_id"]}
        covered = {(p["track_id"], p["rule"]) for p in props if p["rule"]}
        entries = []
        for p in props:
            rule = p["rule"] or "field:%s" % p["field"]
            entries.append({
                "kind": "prop", "rule": rule,
                "label": (rule_label(rule) if not rule.startswith("field:")
                          else "Change '%s'" % field_label(p["field"])),
                "sev": RULE_SEVERITY.get(rule, "yellow"),
                "track_id": p["track_id"],
                "artist_folder": p["artist_folder"], "album_dir": adir,
                "file": fname_by_id.get(p["track_id"], "«album»"),
                "field": p["field"], "current": p["current"],
                "proposed": p["proposed"], "source": p["source"],
                "status": p["status"], "note": p.get("note"),
                "prop_id": p["id"]})
        artist_row = self.con.execute(
            "SELECT artist_folder FROM tracks WHERE album_dir=? LIMIT 1",
            (adir,)).fetchone()
        artist = artist_row[0] if artist_row else ""
        if not self.owner.show_images():
            entries = [e for e in entries if e["rule"] not in IMAGE_RULES]
        for tid, rule, sev, msg in self.con.execute(
                "SELECT track_id, rule, severity, message FROM issues"
                " WHERE album_dir=?", (adir,)):
            if (tid, rule) in covered:
                continue
            if rule in IMAGE_RULES and not self.owner.show_images():
                continue
            f = missing_field_of(rule)
            if f and (tid, f) in prop_fields:
                continue
            fname = fname_by_id.get(tid, "«album»")
            if msg.startswith(fname + ": "):
                msg = msg[len(fname) + 2:]
            entries.append({
                "kind": "issue", "rule": rule,
                "label": rule_label(rule), "sev": sev,
                "track_id": tid, "artist_folder": artist, "album_dir": adir,
                "file": fname, "field": None, "message": msg, "status": None})
        return entries

    def _set_album_mode(self, mode):
        self._album_mode = mode
        self.refresh()

    def _toggle_album_all_fields(self, on):
        self._album_show_all = on
        self.refresh()

    def _fill_entry_tree(self, entries, mode):
        bold = QFont()
        bold.setBold(True)
        groups = defaultdict(list)
        for e in entries:
            groups[e["label"] if mode == "problem" else e["file"]].append(e)

        def order(kv):
            worst = max(SEV_RANK.get(e["sev"], 0) for e in kv[1])
            if mode == "problem":
                return (-worst, -len(kv[1]), kv[0].lower())
            return (kv[0].lower(),)

        for key, group in sorted(groups.items(), key=order):
            worst = max(group, key=lambda e: SEV_RANK.get(e["sev"], 0))["sev"]
            top = QTreeWidgetItem(["%s   (%d)" % (key, len(group))])
            top.setFont(0, bold)
            top.setForeground(0, QColor(SEV_COLORS.get(worst, "#000")))
            top.setFirstColumnSpanned(True)
            if mode == "problem":
                # group header = the change type: explain it on hover
                desc = rule_description(group[0].get("rule"))
                if desc:
                    top.setToolTip(0, desc)
            for e in sorted(group, key=lambda e: (e["file"], e.get("field") or "")):
                if e["kind"] == "prop":
                    field_lbl = PSEUDO_LABEL.get(e["field"], field_label(e["field"]))
                    c0 = e["file"] if mode == "problem" else e["label"]
                    cur_txt = join_vals(e["current"], self.sep())
                    proposed_txt = join_vals(e["proposed"], self.sep())
                    if e.get("rule") == "id3v1_conflict":
                        cur_txt += "   (ID3v2 — current)"
                        proposed_txt += "   (old ID3v1)"
                    src = e["source"]
                    if e["status"] == "postponed":
                        c0 = "[postponed] " + c0
                        src += " · postponed"
                    elif e["status"] == "needs_input":
                        proposed_txt = "‹fill in manually›"
                        src += " · needs input"
                    item = QTreeWidgetItem(
                        [c0, field_lbl, cur_txt, proposed_txt, src])
                    if e["status"] == "postponed":
                        for c in range(5):
                            item.setForeground(c, QColor(STATUS_COLORS["postponed"]))
                    if e["track_id"] and e["field"] in tagio.EDITABLE_FIELDS:
                        item.setFlags(item.flags() | Qt.ItemIsEditable)
                else:
                    c0 = ("%s · %s" % (e["file"], e["message"])
                          if mode == "problem" else e["message"])
                    item = QTreeWidgetItem([c0, "", "", "", ""])
                    item.setForeground(0, QColor(SEV_COLORS.get(e["sev"], "#000")))
                tip = entry_tip(e)
                if tip:
                    for c in range(5):
                        item.setToolTip(c, tip)
                item.setData(0, Qt.UserRole, e)
                top.addChild(item)
            self.entry_tree.addTopLevelItem(top)
        self.entry_tree.expandAll()
        for c in range(5):
            self.entry_tree.resizeColumnToContents(c)
        self.entry_tree.setColumnWidth(0, max(240, self.entry_tree.columnWidth(0)))
        self.entry_tree.setColumnWidth(3, max(180, self.entry_tree.columnWidth(3)))
        persist_header(self.cfg, "album_entries", self.entry_tree.header())

    def _entry_double_clicked(self, item, column):
        e = item.data(0, Qt.UserRole)
        if (column == 3 and e and e["kind"] == "prop" and e["track_id"]
                and e["field"] in tagio.EDITABLE_FIELDS):
            self.entry_tree.editItem(item, 3)

    def _entry_edited(self, item, column):
        if column != 3:
            return
        e = item.data(0, Qt.UserRole)
        if not e or e["kind"] != "prop" or not e["track_id"]:
            return
        text = item.text(3).rstrip()
        if text.endswith("(old ID3v1)"):    # display annotation, not a value
            text = text[:-len("(old ID3v1)")].rstrip()
        vals = split_vals(text, self.sep())
        applier.set_manual_proposal(self.con, e["track_id"], e["field"], vals)
        self.owner.refresh_tree()

    def _selected_entries(self):
        out = []
        for item in self.entry_tree.selectedItems():
            e = item.data(0, Qt.UserRole)
            if e:
                out.append(e)
        return out

    def _entry_menu(self, pos):
        """Context menu adapted to the selection: only actions that can do
        something with the selected rows are offered."""
        from PySide6.QtWidgets import QMenu
        from .common import dot_icon
        entries = self._selected_entries()
        if not entries:
            return
        applicable = [e for e in entries if e["kind"] == "prop"
                      and e["status"] in ("pending", "edited")]
        postponed = [e for e in entries if e["kind"] == "prop"
                     and e["status"] == "postponed"]
        menu = QMenu(self)
        if applicable:
            menu.addAction("Apply selected change(s)", self._apply_selected)
            menu.addAction(dot_icon(STATUS_COLORS["postponed"]), "Postpone",
                           self._postpone_selected)
        if postponed:
            menu.addAction(dot_icon(STATUS_COLORS["postponed"]),
                           "Remove postpone (make applicable again)",
                           self._restore_selected)
        menu.addAction(dot_icon(STATUS_COLORS["exception"]),
                       "Mark as exception (ignore forever)",
                       self._except_selected)
        if any(e["kind"] == "prop" and e["source"] == "manual" for e in entries):
            menu.addAction("Delete manual change(s)", self._delete_selected)
        if any(e.get("rule") == "id3v1_conflict" for e in entries):
            menu.addSeparator()
            if postponed:
                menu.addAction("Use old ID3v1 value (removes its postpone)",
                               self._restore_selected)
            menu.addAction("Keep ID3v2 value && allow removing the old tag",
                           self._keep_v2_selected)
        menu.exec(self.entry_tree.viewport().mapToGlobal(pos))

    def _delete_selected(self):
        ids = [e["prop_id"] for e in self._selected_entries()
               if e["kind"] == "prop" and e["source"] == "manual"]
        if not ids:
            QMessageBox.information(
                self, "Nothing to delete",
                "Only manual changes can be deleted — select rows whose"
                " Source is 'manual'.")
            return
        qs = ",".join("?" * len(ids))
        self.con.execute("DELETE FROM proposals WHERE id IN (%s)" % qs, ids)
        self.con.commit()
        self.refresh()
        self.owner.refresh_tree()

    def _keep_v2_selected(self):
        entries = [e for e in self._selected_entries()
                   if e.get("rule") == "id3v1_conflict"]
        if not entries:
            return
        applier.resolve_v1_keep_v2(self.con, self.cfg["settings"], entries)
        self.refresh()
        self.owner.refresh_tree()

    def _postpone_selected(self):
        ids = [e["prop_id"] for e in self._selected_entries()
               if e["kind"] == "prop" and e["status"] in ("pending", "edited")]
        applier.set_proposal_status(self.con, ids, "postponed")
        self.refresh()
        self.owner.refresh_tree()

    def _restore_selected(self):
        ids = [e["prop_id"] for e in self._selected_entries()
               if e["kind"] == "prop" and e["status"] == "postponed"]
        applier.set_proposal_status(self.con, ids, "pending")
        self.refresh()
        self.owner.refresh_tree()

    def _except_selected(self):
        entries = self._selected_entries()
        if not entries:
            return
        if QMessageBox.question(
                self, "Mark as exception",
                "Permanently ignore %d selected item(s)?\n\nThey disappear from"
                " the problem list and no longer color the album. Undo any time"
                " via the Exceptions… button." % len(entries)) != QMessageBox.Yes:
            return
        applier.mark_exceptions(self.con, self.cfg["settings"], entries)
        self.refresh()
        self.owner.refresh_tree()

    def _exceptions(self, adir, artist):
        dlg = ExceptionsDialog(self.con, self.cfg["settings"], adir, artist, self)
        dlg.exec()
        if dlg.changed:
            self.refresh()
            self.owner.refresh_tree()

    def _history(self, adir):
        dlg = HistoryDialog(self.con, self.cfg["settings"], adir, self)
        dlg.exec()
        if dlg.reverted:
            self.owner.refresh_tree()
            self.refresh()

    def _find_cover(self, adir, artist, snaps):
        album_name = ""
        artist_name = artist
        for tags in snaps.values():
            if tags.get("album"):
                album_name = tags["album"][0]
            if tags.get("albumartist"):
                artist_name = tags["albumartist"][0]
            elif tags.get("artist"):
                artist_name = tags["artist"][0]
            if album_name:
                break
        dlg = CoverSearchDialog(self.con, artist_name, album_name, adir, self)
        dlg.exec()
        if dlg.chosen:
            self.refresh()
            self.owner.refresh_tree()

    # ---- track ----

    def show_track(self, track_id):
        _clear(self.lay)
        self.current = ("track", track_id)
        row = self.con.execute(
            "SELECT filename, path, album_dir FROM tracks WHERE id=?",
            (track_id,)).fetchone()
        if row is None:
            return
        fname, path, adir = row
        snap = db.latest_snapshot(self.con, track_id)
        tags = snap["tags"] if snap else {}

        name_row = QHBoxLayout()
        name_row.addWidget(sel_label("<h3>%s</h3>" % fname))
        name_row.addWidget(copy_button(lambda f=fname: f, "Copy file name"))
        name_row.addStretch(1)
        self.lay.addLayout(name_row)
        meta = "ID3v%s%s · %s kbps · %s" % (
            tags.get("_version", "?"),
            " + old ID3v1" if tags.get("_has_v1") else "",
            (tags.get("_bitrate") or 0) // 1000,
            ", ".join("%s×%s" % (c["w"], c["h"]) for c in tags.get("_cover", [])) or "no cover")
        self.lay.addWidget(sel_label("<i>%s</i>" % meta))
        txxx = tags.get("_txxx") or {}
        if txxx:
            lbl = sel_label("<i>Custom tags (TXXX): %s</i>" % ", ".join(
                "%s = %s" % (k, "; ".join(v)) for k, v in sorted(txxx.items())))
            lbl.setWordWrap(True)
            self.lay.addWidget(lbl)

        issues = [(sev, msg) for sev, msg, rule in self.con.execute(
            "SELECT severity, message, rule FROM issues WHERE track_id=?",
            (track_id,))
            if self.owner.show_images() or rule not in IMAGE_RULES]
        if issues:
            self.lay.addWidget(self._issues_list(issues))

        props = {p["field"]: p for p in db.open_proposals(self.con,
                                                          track_ids=[track_id])}
        show_all = getattr(self, "_show_all_fields", False)
        fields = [f for f in tagio.EDITABLE_FIELDS
                  if f in tagio.PRIMARY_FIELDS or tags.get(f) or f in props
                  or show_all]
        self._track_fields = fields
        self.track_table = QTableWidget(len(fields), 3)
        self.track_table.setHorizontalHeaderLabels(["Field", "Current", "Proposed"])
        self._track_id = track_id
        for r, field in enumerate(fields):
            it0 = QTableWidgetItem(field_label(field))
            it0.setFlags(it0.flags() & ~Qt.ItemIsEditable)
            it1 = QTableWidgetItem(join_vals(tags.get(field, []), self.sep()))
            it1.setFlags(it1.flags() & ~Qt.ItemIsEditable)
            p = props.get(field)
            it2 = QTableWidgetItem(join_vals(p["proposed"], self.sep()) if p else "")
            self.track_table.setItem(r, 0, it0)
            self.track_table.setItem(r, 1, it1)
            self.track_table.setItem(r, 2, it2)
        self.track_table.itemChanged.connect(self._track_edited)
        self.track_table.resizeColumnsToContents()
        self.track_table.setColumnWidth(2, max(240, self.track_table.columnWidth(2)))
        enable_copy(self.track_table)
        persist_header(self.cfg, "track_fields", self.track_table.horizontalHeader())
        self.lay.addWidget(self.track_table)
        all_cb = QCheckBox("Show all editable fields (composer, publisher, ISRC, …)")
        all_cb.setChecked(show_all)
        all_cb.toggled.connect(self._toggle_all_fields)
        self.lay.addWidget(all_cb)
        note = QLabel("Empty 'Proposed' = no change. Multi-value fields use '%s'."
                      % self.sep().strip())
        self.lay.addWidget(note)

        n_open = len(props)
        btns = QHBoxLayout()
        btns.addWidget(QLabel("<b>Actions:</b>"))
        apply_btn = QPushButton("Apply this track (%d)" % n_open)
        apply_btn.setToolTip("Write the open proposed changes of this track"
                             " into the MP3 file")
        apply_btn.setEnabled(n_open > 0)
        apply_btn.clicked.connect(lambda: self.owner.apply_scope(track_ids=[track_id]))
        btns.addWidget(apply_btn)
        btns.addWidget(self._refresh_btn(albums=[adir]))
        btns.addStretch(1)
        self.lay.addLayout(btns)

    def _toggle_all_fields(self, on):
        self._show_all_fields = on
        self.refresh()

    def _track_edited(self, item):
        if item.column() != 2 or item.row() >= len(self._track_fields):
            return
        field = self._track_fields[item.row()]
        vals = split_vals(item.text(), self.sep())
        applier.set_manual_proposal(self.con, self._track_id, field, vals)
        self.owner.refresh_tree()

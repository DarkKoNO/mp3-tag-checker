"""Shared GUI helpers: severity icons, multi-value text, background worker."""

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

from .. import db

# Severity / status colors. These dicts are MUTATED IN PLACE by apply_theme,
# so every module that imported them sees the active theme's colors.
# 'gray' = the file is missing on disk (a leftover library entry). It ranks
# below yellow so a real red/yellow problem still shows at a parent node, but
# above green so an otherwise-clean album with a missing track no longer looks OK.
SEV_COLORS = {"red": "#d9534f", "yellow": "#e0a800", "gray": "#8f8f8f",
              "green": "#4a9e4a", None: "#4a9e4a"}
SEV_RANK = {"red": 3, "yellow": 2, "gray": 1, None: 0, "green": 0}
STATUS_COLORS = {
    "postponed": "#8f8f8f",     # gray   = postponed (visible, not applied)
    "exception": "#9575cd",     # purple = exception (permanently ignored)
    "attention": "#3874c8",     # accent = something needs confirming
    "decided": "#2e9e5b",       # green  = a per-field 'keep ID3v2' decision
}

# Display names of metadata fields. FIELD_LABELS holds the active alias set
# (Settings - Field names) and is MUTATED IN PLACE by apply_field_labels;
# FIELD_UI is the historical fallback for a few multi-word fields.
FIELD_LABELS = {}
FIELD_UI = {"origartist": "original artist", "origdate": "original date",
            "discsubtitle": "disc subtitle", "artistsort": "artist sort",
            "albumsort": "album sort", "titlesort": "title sort",
            "bpm": "BPM", "isrc": "ISRC"}


def success_box(parent, settings, title, text):
    """A success confirmation (OK + a 'don't show again' checkbox). When the
    user opted out — via the checkbox or Settings — the popup is skipped and
    the message goes to the main window's status bar instead. Warnings and
    errors are never routed through here; they always show."""
    from PySide6.QtWidgets import (QApplication, QCheckBox, QMainWindow,
                                   QMessageBox)
    if not settings.get("success_popups", True):
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QMainWindow):
                w.statusBar().showMessage(
                    "%s — %s" % (title, " ".join(text.split())), 8000)
                break
        return
    box = QMessageBox(QMessageBox.Information, title, text,
                      QMessageBox.Ok, parent)
    cb = QCheckBox("Don't show these confirmations again"
                   " (warnings and errors still appear)")
    box.setCheckBox(cb)
    box.exec()
    if cb.isChecked():
        settings["success_popups"] = False
        from ..settings import save_config
        # settings IS the main window's cfg['settings'] — persist right away
        for w in QApplication.topLevelWidgets():
            if getattr(w, "cfg", None) and w.cfg.get("settings") is settings:
                save_config(w.cfg)
                break


def field_label(field):
    """How a metadata field is displayed everywhere in the GUI."""
    if not field:
        return ""
    from .. import tagio
    if tagio.is_extra(field):
        # dynamic tag (named comment, custom TXXX, lyrics, …): named after the
        # frame it lives in, so it cannot collide with a user alias
        return tagio.extra_label(field)
    return FIELD_LABELS.get(field) or FIELD_UI.get(field, field)


def apply_field_labels(settings):
    """Activate the field-name alias set chosen in the settings."""
    from ..settings import resolve_field_labels
    FIELD_LABELS.clear()
    FIELD_LABELS.update(resolve_field_labels(settings))

_icons = {}
_default_font = None    # captured at first theme apply


def dot_icon(severity):
    """Colored dot for a severity name ('red', 'yellow', ...) or any
    '#rrggbb' color (used e.g. for postpone/exception menu entries)."""
    key = severity or "green"
    if key not in _icons:
        color = SEV_COLORS.get(key,
                               key if str(key).startswith("#") else "#4a9e4a")
        pm = QPixmap(14, 14)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(1, 1, 12, 12)
        p.end()
        _icons[key] = QIcon(pm)
    return _icons[key]


def worse(a, b):
    return a if SEV_RANK.get(a, 0) >= SEV_RANK.get(b, 0) else b


def join_vals(vals, sep="; "):
    return sep.join(vals or [])


def split_vals(text, sep="; "):
    parts = [p.strip() for p in text.split(sep.strip())]
    return [p for p in parts if p]


# A tag value can hold real line breaks (lyrics above all). A table cell paints
# every line of its text but is only one line high, so the lines end up drawn on
# top of each other. Values are therefore shown on ONE line with a visible ⏎ for
# each break; the untouched original travels in RAW_ROLE so copying, and the
# tooltip, still give back the real multi-line text.
NL_MARK = " ⏎ "
RAW_ROLE = Qt.UserRole + 20


def flat(text):
    """The one-line display form of a possibly multi-line value."""
    if "\n" not in text and "\r" not in text:
        return text
    return NL_MARK.join(ln.strip() for ln in text.splitlines() if ln.strip())


def unflat(text):
    """Turn a display form the user edited back into real line breaks."""
    return "\n".join(p.strip() for p in text.split(NL_MARK.strip()))


def value_item(text, editable=False):
    """A table cell for a tag value: one-line display, full text in the tooltip
    and in RAW_ROLE (what Ctrl+C / the copy icon hand out)."""
    from PySide6.QtWidgets import QTableWidgetItem
    it = QTableWidgetItem(flat(text))
    if not editable:
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    if text:
        it.setToolTip(text)
        it.setData(RAW_ROLE, text)
    return it


def raw_text(item):
    """What an item really holds — the multi-line original when there is one."""
    if item is None:
        return ""
    return item.data(RAW_ROLE) or item.text()


_save_timer = None
_save_cfg = None


def _debounced_save(cfg):
    """Write config.json at most once per second while the user drags things."""
    global _save_timer, _save_cfg
    from PySide6.QtCore import QTimer

    from ..settings import save_config
    _save_cfg = cfg
    if _save_timer is None:
        _save_timer = QTimer()
        _save_timer.setSingleShot(True)
        _save_timer.timeout.connect(lambda: save_config(_save_cfg))
    _save_timer.start(1000)


def persist_header(cfg, key, header):
    """Restore saved column widths and remember future user changes."""
    lay = cfg["settings"].setdefault("ui_layout", {})
    widths = lay.get(key)
    if widths:
        for i, w in enumerate(widths):
            if i < header.count() and w > 20:
                header.resizeSection(i, w)

    def on_resize(*_):
        lay[key] = [header.sectionSize(i) for i in range(header.count())]
        _debounced_save(cfg)
    header.sectionResized.connect(on_resize)


def persist_splitter(cfg, key, splitter):
    """Restore saved splitter ratio and remember future user changes."""
    lay = cfg["settings"].setdefault("ui_layout", {})
    sizes = lay.get(key)
    if sizes and len(sizes) == len(splitter.sizes()) and any(sizes):
        splitter.setSizes(sizes)

    def on_move(*_):
        lay[key] = splitter.sizes()
        _debounced_save(cfg)
    splitter.splitterMoved.connect(on_move)


def enable_copy(view):
    """Ctrl+C copies the selected cells/rows of a table or tree as text."""
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import QApplication, QTableWidget, QTreeWidget

    def do_copy():
        lines = []
        if isinstance(view, QTableWidget):
            rows = {}
            for it in view.selectedItems():
                # a one-line ⏎ display must copy out as the real multi-line value
                rows.setdefault(it.row(), {})[it.column()] = raw_text(it)
            for r in sorted(rows):
                cols = rows[r]
                lines.append("\t".join(cols.get(c, "")
                                       for c in range(view.columnCount())
                                       if c in cols or len(cols) > 1))
        elif isinstance(view, QTreeWidget):
            for it in view.selectedItems():
                lines.append("\t".join(
                    it.text(c) for c in range(view.columnCount())
                    if it.text(c)))
        if lines:
            QApplication.clipboard().setText("\n".join(lines))
    sc = QShortcut(QKeySequence.Copy, view)
    sc.setContext(Qt.WidgetShortcut)
    sc.activated.connect(do_copy)


def add_hover_copy(table, value_col):
    """Make the values in `value_col` text-selectable (select part of a value
    with the mouse and press Ctrl+C) and show a one-click copy icon at the end
    of the row under the pointer. The value cells become read-only selectable
    labels, so whole-cell selection is not needed."""
    from PySide6.QtCore import QEvent, QObject, Qt, QTimer
    from PySide6.QtWidgets import QApplication, QLabel, QToolButton

    btn = QToolButton(table.viewport())
    btn.setText("⧉")
    btn.setToolTip("Copy this value to the clipboard")
    btn.setCursor(Qt.PointingHandCursor)
    btn.setAutoRaise(True)
    btn.hide()
    btn._row = None

    hide_timer = QTimer(table)
    hide_timer.setSingleShot(True)
    hide_timer.setInterval(140)

    def _hide():
        btn.hide()
        btn._row = None
    hide_timer.timeout.connect(_hide)

    def do_copy():
        if btn._row is None:
            return
        it = table.item(btn._row, value_col)
        if it is not None:
            QApplication.clipboard().setText(raw_text(it))
    btn.clicked.connect(do_copy)

    def show_for(row):
        hide_timer.stop()
        it = table.item(row, value_col)
        if it is None or not it.text().strip() or it.text() == "—":
            _hide()
            return
        rect = table.visualRect(table.model().index(row, value_col))
        size = max(14, min(rect.height() - 2, 20))
        btn.resize(size, size)
        btn.move(rect.right() - size - 2, rect.top() + (rect.height() - size) // 2)
        btn._row = row
        btn.raise_()
        btn.show()

    class _RowHover(QObject):
        def __init__(self, row):
            super().__init__(table)
            self.row = row

        def eventFilter(self, _obj, ev):
            if ev.type() == QEvent.Enter:
                show_for(self.row)
            elif ev.type() == QEvent.Leave:
                hide_timer.start()      # brief grace so moving onto the icon works
            return False

    class _BtnHover(QObject):
        def eventFilter(self, _obj, ev):
            if ev.type() == QEvent.Enter:
                hide_timer.stop()
            elif ev.type() == QEvent.Leave:
                hide_timer.start()
            return False

    filters = []
    for r in range(table.rowCount()):
        it = table.item(r, value_col)
        text = it.text() if it is not None else ""
        if not text.strip() or text == "—":
            continue
        lbl = QLabel(text)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse
                                    | Qt.TextSelectableByKeyboard)
        lbl.setCursor(Qt.IBeamCursor)
        lbl.setContentsMargins(4, 0, 22, 0)     # leave room for the copy icon
        lbl.setToolTip("Select text and press Ctrl+C, or click the copy icon")
        table.setCellWidget(r, value_col, lbl)  # item stays underneath for copy
        f = _RowHover(r)
        lbl.installEventFilter(f)
        filters.append(f)

    bf = _BtnHover()
    btn.installEventFilter(bf)
    table._hover_copy = (btn, hide_timer, filters, bf)   # keep refs alive


def sel_label(text, rich=False):
    """A QLabel whose text can be selected with the mouse and copied."""
    from PySide6.QtWidgets import QLabel
    lbl = QLabel(text)
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lbl


def copy_button(get_text, tooltip="Copy to clipboard"):
    """A small one-click copy icon button."""
    from PySide6.QtWidgets import QApplication, QToolButton
    b = QToolButton()
    b.setText("⧉")
    b.setAutoRaise(True)
    b.setToolTip(tooltip)
    b.setFixedWidth(24)
    b.clicked.connect(lambda: QApplication.clipboard().setText(get_text()))
    return b


def _theme_stylesheet(theme):
    """Application-wide stylesheet built from the theme (chrome + fonts)."""
    c = theme["colors"]
    css = """
        #pageBar {{ background: {page_bar_bg}; border-radius: 8px; }}
        #pageBar QPushButton {{ font-size: 14px; font-weight: bold;
                   padding: 8px 26px; border: none; border-radius: 6px;
                   background: transparent; color: {page_text}; }}
        #pageBar QPushButton:hover {{ background: {page_hover_bg}; }}
        #pageBar QPushButton:checked {{ background: {page_active_bg};
                   color: {page_active_text}; }}
        QFrame#actGroup {{ background: {group_bg}; border-radius: 6px; }}
        QLabel#actCaption {{ font-size: 10px; font-weight: bold;
                   color: {caption}; }}
        QToolTip {{ background: {tooltip_base}; color: {tooltip_text};
                   border: 1px solid {caption}; }}
    """.format(**c)

    def font_css(spec):
        parts = []
        if spec.get("family"):
            parts.append('font-family: "%s";' % spec["family"])
        if spec.get("size"):
            parts.append("font-size: %dpt;" % spec["size"])
        return " ".join(parts)

    lists = font_css(theme["fonts"].get("lists", {}))
    if lists:
        css += ("QTreeView, QTreeWidget, QTableView, QTableWidget,"
                " QListWidget {{ {0} }}\n".format(lists))
    menu = font_css(theme["fonts"].get("menu", {}))
    if menu:
        # placed last so it overrides the size from the #pageBar rule above
        css += "#pageBar QPushButton {{ {0} }}\n".format(menu)
    return css


def apply_theme(theme_name):
    """Apply a theme by name ('auto' = follow Windows; 'Light'/'Dark' =
    built-ins; anything else = a user theme from themes.json). Every color
    and font comes from the theme - nothing depends on the OS palette, so
    'Light' is light on every machine."""
    from PySide6.QtGui import QColor, QFont, QPalette
    from PySide6.QtWidgets import QApplication

    from ..settings import resolve_theme
    app = QApplication.instance()
    if app is None:
        return
    theme = resolve_theme(theme_name)
    c = theme["colors"]

    # severity / status colors: mutate the shared dicts + drop cached icons
    SEV_COLORS.update({"red": c["severity_red"], "yellow": c["severity_yellow"],
                       "gray": c.get("postponed", "#8f8f8f"),
                       "green": c["severity_green"], None: c["severity_green"]})
    STATUS_COLORS.update({"postponed": c["postponed"],
                          "exception": c["exception"],
                          "attention": c["attention"]})
    _icons.clear()

    app.setStyle("Fusion")      # deterministic base style on every machine
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(c["window"]))
    pal.setColor(QPalette.WindowText, QColor(c["window_text"]))
    pal.setColor(QPalette.Base, QColor(c["base"]))
    pal.setColor(QPalette.AlternateBase, QColor(c["alternate_base"]))
    pal.setColor(QPalette.ToolTipBase, QColor(c["tooltip_base"]))
    pal.setColor(QPalette.ToolTipText, QColor(c["tooltip_text"]))
    pal.setColor(QPalette.Text, QColor(c["text"]))
    pal.setColor(QPalette.Button, QColor(c["button"]))
    pal.setColor(QPalette.ButtonText, QColor(c["button_text"]))
    pal.setColor(QPalette.BrightText, QColor(c["severity_red"]))
    pal.setColor(QPalette.Link, QColor(c["link"]))
    pal.setColor(QPalette.Highlight, QColor(c["highlight"]))
    pal.setColor(QPalette.HighlightedText, QColor(c["highlighted_text"]))
    pal.setColor(QPalette.PlaceholderText, QColor(c["placeholder"]))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(c["disabled_text"]))
    app.setPalette(pal)
    # palette changes after startup don't reliably reach widgets that
    # already exist (especially with style sheets) - push it explicitly
    for w in app.allWidgets():
        w.setPalette(pal)
        w.update()

    global _default_font
    if _default_font is None:
        _default_font = QFont(app.font())   # remember the system default once
    base_font = theme["fonts"].get("base", {})
    f = QFont(_default_font)
    if base_font.get("family"):
        f.setFamily(base_font["family"])
    if base_font.get("size"):
        f.setPointSize(base_font["size"])
    app.setFont(f)

    app.setStyleSheet(_theme_stylesheet(theme))


class Worker(QThread):
    """Runs fn(con, progress_cb) with its own DB connection in a thread."""
    progress = Signal(int, int, str)
    done = Signal(object)

    def __init__(self, fn, db_path=None, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._db_path = db_path

    def run(self):
        con = db.connect(self._db_path)
        try:
            result = self._fn(con, lambda a, b, t: self.progress.emit(a, b, t))
            self.done.emit(result)
        except Exception as e:
            self.done.emit({"error": "%s: %s" % (type(e).__name__, e)})
        finally:
            con.close()

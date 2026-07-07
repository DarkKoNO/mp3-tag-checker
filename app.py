#!/usr/bin/env python3
"""MP3 Tag Checker - GUI application entry point."""

import sys
import time
import traceback
from pathlib import Path

ERROR_LOG = Path(__file__).resolve().parent / "error.log"


def _log_error(exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write("\n=== %s ===\n%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass
    sys.stderr.write(text)
    if "DLL load failed" in text or "shiboken" in text.lower():
        sys.stderr.write(
            "\nHINT: PySide6 could not load its libraries. This usually means"
            "\nthe 'Microsoft Visual C++ Redistributable (x64)' is missing -"
            "\ninstall it from https://aka.ms/vs/17/release/vc_redist.x64.exe"
            "\nand start the app again.\n")


def _install_excepthook():
    """Log unexpected errors to error.log and show them, instead of dying silently."""
    def hook(exc_type, exc, tb):
        _log_error(exc_type, exc, tb)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance():
                QMessageBox.critical(
                    None, "Unexpected error",
                    "Something went wrong (details in error.log):\n\n%s" % exc)
        except Exception:
            pass
    sys.excepthook = hook


def main():
    from PySide6.QtWidgets import QApplication

    from mp3lib.gui.main_window import MainWindow
    from mp3lib.settings import load_config

    from mp3lib.gui.common import apply_field_labels, apply_theme

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    cfg = load_config()
    apply_theme(cfg["settings"].get("theme", "auto"))
    apply_field_labels(cfg["settings"])
    win = MainWindow(cfg)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    _install_excepthook()
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # startup errors (imports, config, window construction) land here
        _log_error(*sys.exc_info())
        sys.exit(1)

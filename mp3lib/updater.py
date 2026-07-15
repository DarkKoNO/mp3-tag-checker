"""Auto-update: version check against GitHub and self-update.

The repository keeps version.json (version number + human-readable
changelog) in the main branch. The app compares its local copy of that
file with the one on raw.githubusercontent.com; a higher remote version
means an update exists.

Updating downloads the main-branch ZIP, extracts it to a temp folder and
hands over to a small batch script that waits for the app to exit, copies
the new files over the installation and restarts run.bat. User data
(config.json, themes.json, folders.txt, the *.db databases, .venv) is
gitignored, therefore never inside the ZIP, therefore never touched.
No files are deleted - the copy only adds and overwrites.
"""

import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from .settings import BASE_DIR

GITHUB_REPO = "DarkKoNO/mp3-tag-checker"
BRANCH = "main"
REPO_URL = "https://github.com/%s" % GITHUB_REPO
VERSION_URL = ("https://raw.githubusercontent.com/%s/%s/version.json"
               % (GITHUB_REPO, BRANCH))
ZIP_URL = ("https://github.com/%s/archive/refs/heads/%s.zip"
           % (GITHUB_REPO, BRANCH))
VERSION_PATH = BASE_DIR / "version.json"


def read_local():
    """The local version.json as a dict: {'version': ..., 'changelog': [...]}.
    Never raises - a missing/broken file reads as version 0.0.0."""
    try:
        data = json.loads(VERSION_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": "0.0.0", "changelog": []}


def local_version():
    return str(read_local().get("version", "0.0.0"))


# version.json as of process start = the version of the CODE this process is
# actually running. version.json on disk can move ahead of it while the app
# stays open (an installed update, or the folder being synced/overwritten) —
# then the only missing step is a restart, and the GUI says so.
RUNNING_VERSION = local_version()


def parse_version(text):
    """'1.2.3' -> (1, 2, 3). Non-numeric parts count as 0."""
    parts = []
    for p in str(text).strip().split("."):
        m = re.match(r"\d+", p.strip())
        parts.append(int(m.group()) if m else 0)
    return tuple(parts) if parts else (0,)


def is_newer(remote, local):
    a, b = parse_version(remote), parse_version(local)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _entries(data):
    """The changelog list of a version.json dict, bad items dropped."""
    out = []
    for e in data.get("changelog") or []:
        if isinstance(e, dict) and e.get("version"):
            out.append({"version": str(e["version"]),
                        "date": str(e.get("date", "")),
                        "changes": [str(c) for c in e.get("changes") or []]})
    return out


def check_for_update(timeout=10):
    """Fetch version.json from GitHub and compare with the local one.

    Returns {'update': bool, 'version': remote, 'local': local,
             'notes': [changelog entries newer than the local version]}.
    Network/parse errors raise - callers decide how loud to be (the
    startup check stays silent, the Settings tab shows the message).
    """
    import requests
    r = requests.get(VERSION_URL, timeout=timeout)
    r.raise_for_status()
    remote = r.json()
    if not isinstance(remote, dict) or not remote.get("version"):
        raise ValueError("version.json on GitHub has an unexpected format")
    rv = str(remote["version"])
    lv = local_version()
    notes = [e for e in _entries(remote) if is_newer(e["version"], lv)]
    notes.sort(key=lambda e: parse_version(e["version"]), reverse=True)
    return {"update": is_newer(rv, lv), "version": rv, "local": lv,
            "notes": notes}


def download_update(progress_cb=None):
    """Download the main-branch ZIP and extract it to a temp folder.

    progress_cb(done_bytes, total_bytes) - total is 0 when GitHub does not
    announce a length (it usually does not for branch ZIPs).
    Returns the extracted repository folder (inside the temp folder).
    """
    import requests
    tmp = Path(tempfile.mkdtemp(prefix="mp3tagchecker_update_"))
    zip_path = tmp / "update.zip"
    with requests.get(ZIP_URL, stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        with zip_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)
    src_root = tmp / "src"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(src_root)
    zip_path.unlink()
    dirs = [p for p in src_root.iterdir() if p.is_dir()]
    if len(dirs) != 1 or not (dirs[0] / "app.py").exists() \
            or not (dirs[0] / "run.bat").exists():
        raise RuntimeError("The downloaded archive does not look like"
                           " MP3 Tag Checker - update aborted.")
    return dirs[0]


_BAT_TEMPLATE = r"""@echo off
title MP3 Tag Checker - installing update
echo Waiting for MP3 Tag Checker to close...
set tries=0
:wait
set /a tries+=1
if %tries% geq 120 goto copy
tasklist /fi "PID eq {pid}" 2>nul | find " {pid} " >nul
if errorlevel 1 goto copy
timeout /t 1 /nobreak >nul
goto wait
:copy
echo Installing the new version...
robocopy "{src}" "{dst}" /E /R:3 /W:2 /NFL /NDL /NJH /NJS /NP
if errorlevel 8 goto failed
echo Restarting MP3 Tag Checker...
cd /d "{dst}"
start "" "{dst}\run.bat"
rd /s /q "{tmp}" 2>nul & exit
:failed
echo.
echo The new files could not be copied - the update was NOT installed.
echo Close every program that might use the application folder and try
echo again, or download the new version manually from
echo {repo}
pause
exit /b 1
"""


def apply_update_and_restart(src_dir):
    """Start the handover batch script (waits for this process to exit,
    copies the new files over BASE_DIR, restarts run.bat, cleans up).
    The caller must quit the application right after this returns."""
    src_dir = Path(src_dir)
    tmp = src_dir.parent.parent          # the mkdtemp folder
    bat = tmp / "apply_update.bat"
    bat.write_text(
        _BAT_TEMPLATE.format(pid=os.getpid(), src=str(src_dir),
                             dst=str(BASE_DIR), tmp=str(tmp), repo=REPO_URL),
        encoding="cp1252", errors="replace")
    subprocess.Popen(["cmd.exe", "/c", str(bat)],
                     cwd=str(tmp),
                     creationflags=subprocess.CREATE_NEW_CONSOLE)

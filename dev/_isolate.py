"""Import this FIRST in any dev test/script that imports mp3lib, so all user
data (config.json, themes.json, library databases) is redirected to a throwaway
temp dir. This guarantees a test can never read or write the real config or
libraries. See mp3lib/settings.py (DATA_DIR) and dev/README.md.

    import _isolate  # noqa: F401  -- must come before importing mp3lib
"""
import os
import tempfile

os.environ.setdefault("MP3TAGGER_DATA_DIR",
                      tempfile.mkdtemp(prefix="mp3tagger-test-"))

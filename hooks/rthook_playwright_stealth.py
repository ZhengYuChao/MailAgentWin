"""
PyInstaller runtime hook for playwright_stealth.
Patches playwright_stealth.stealth.from_file to load JS files
from the correct _MEIPASS directory when running as a frozen app.
"""
import sys
import os

if getattr(sys, 'frozen', False):
    import pathlib

    _meipass = pathlib.Path(sys._MEIPASS)

    # Patch playwright_stealth so it reads from _MEIPASS/playwright_stealth/js/
    def _patch_stealth():
        try:
            import playwright_stealth.stealth as _stealth_mod

            _js_base = _meipass / "playwright_stealth" / "js"

            def _patched_from_file(filename):
                return (_js_base / filename).read_text(encoding="utf-8")

            _stealth_mod.StealthConfig.from_file = staticmethod(_patched_from_file)
        except Exception:
            pass

    _patch_stealth()

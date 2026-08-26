# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from pathlib import Path

try:
    from PyInstaller.utils.hooks import collect_data_files, collect_submodules
except Exception:
    def collect_data_files(pkg):
        return []
    def collect_submodules(pkg):
        return []

block_cipher = None

# ── Helper: find site-packages path of a package ───────────────────────────────
def _pkg_dir(pkg_name):
    """Return the folder of an installed package, or None if not found."""
    try:
        import importlib
        m = importlib.import_module(pkg_name)
        return Path(m.__file__).parent
    except Exception:
        return None

# ── Static data files ───────────────────────────────────────────────────────────
datas = [
    ('src/ui/static', 'src/ui/static'),
    ('img', 'img'),
]

# Optional user-generated data files
for opt_file in ('prompt.txt', 'prompt_daily.txt', 'user_agent.txt'):
    if Path(opt_file).exists():
        datas.append((opt_file, '.'))

# playwright_stealth: explicitly include the js/ folder (critical fix)
_stealth_dir = _pkg_dir('playwright_stealth')
if _stealth_dir:
    print(f"[SPEC] playwright_stealth found at: {_stealth_dir}")
    _js_dir = _stealth_dir / 'js'
    if _js_dir.exists():
        # Add entire js directory tree (all .js files in js/ and js/evasions/)
        datas.append((str(_js_dir), 'playwright_stealth/js'))
        print(f"[SPEC] Added playwright_stealth/js tree from: {_js_dir}")
    else:
        print(f"[SPEC] WARNING: playwright_stealth/js NOT found at: {_js_dir}")
else:
    print("[SPEC] WARNING: playwright_stealth package NOT found — JS files will be missing!")

# playwright: explicitly include the driver/ folder
_playwright_dir = _pkg_dir('playwright')
if _playwright_dir:
    _driver_dir = _playwright_dir / 'driver'
    if _driver_dir.exists():
        datas.append((str(_driver_dir), 'playwright/driver'))
        print(f"[SPEC] Added playwright/driver tree from: {_driver_dir}")
    else:
        print(f"[SPEC] WARNING: playwright/driver NOT found at: {_driver_dir}")
else:
    print("[SPEC] WARNING: playwright package NOT found!")

# pywebview: collect platform resources
try:
    datas += collect_data_files('webview')
except Exception:
    pass

try:
    datas += collect_data_files('pythonnet')
except Exception:
    pass

try:
    datas += collect_data_files('clr_loader')
except Exception:
    pass

# ── Hidden imports ──────────────────────────────────────────────────────────────
hiddenimports = [
    'src',
    'src.ui',
    'src.ui.server',
    'src.setup',
    'src.setup.schema',
    'src.setup.persistence',
    'src.runtime',
    'src.runtime.supervisor',
    'src.runtime.status_registry',
    'src.runtime.log_stream',
    'src.ai',
    'src.ai.controller',
    'workers',
    'workers.mail_worker',
    'workers.ai_worker',
    'scripts',
    'scripts.initial_sync_win',
    # pywebview GUI
    'webview',
    'webview.platforms',
    'webview.platforms.winforms',
    'webview.platforms.edgechromium',
    'webview.platforms.mshtml',
    'webview.util',
    'webview.menu',
    'webview.screen',
    'webview.window',
    'webview.event',
    'webview.errors',
    'webview.dom',
    'webview.js',
    'clr',
    'clr_loader',
    'pythonnet',
    'System',
    'System.Windows.Forms',
    'System.Drawing',
    'System.Threading',
    # Playwright & stealth
    'playwright',
    'playwright_stealth',
    'playwright_stealth.stealth',
    # Windows COM
    'win32com',
    'win32com.client',
    'win32timezone',
    'pythoncom',
    'pywintypes',
]

try:
    hiddenimports += collect_submodules('playwright_stealth')
except Exception:
    pass

try:
    hiddenimports += collect_submodules('webview')
except Exception:
    pass

try:
    hiddenimports += collect_submodules('win32com')
except Exception:
    pass

# ── Analysis ────────────────────────────────────────────────────────────────────
a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MailAgent',
    icon='img/icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MailAgent',
)

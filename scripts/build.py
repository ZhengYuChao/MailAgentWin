"""
MailAgent Windows Build Script (PyInstaller wrapper).
Builds in local %TEMP% to prevent OneDrive sync lock errors,
then copies the final build to the project dist directory.

Post-build verification ensures playwright_stealth JS files
and pywebview platform modules are correctly bundled.

Usage:
    python scripts/build.py
"""
import subprocess
import sys
import os
import shutil
import tempfile
import glob
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    print("=" * 60)
    print("  MailAgent Windows Build (PyInstaller)")
    print("=" * 60)
    print(f"[INFO] Project Root: {root}")
    print(f"[INFO] Python: {sys.executable}")

    # ── Step 1: Kill running MailAgent and clean stale bytecode cache ──────────
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "MailAgent.exe", "/T"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    print("\n[STEP 1] Cleaning stale bytecode cache (__pycache__ and .pyc)...")
    for pyc_dir in root.glob("**/__pycache__"):
        if pyc_dir.is_dir():
            shutil.rmtree(pyc_dir, ignore_errors=True)
    for pyc_file in root.glob("**/*.pyc"):
        try:
            pyc_file.unlink(missing_ok=True)
        except Exception:
            pass
    print("  ✅ Stale cache cleaned.")

    # ── Step 2: Install all build+runtime dependencies ─────────────────────────
    print("\n[STEP 2] Installing build and runtime dependencies...")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        "-r", str(root / "requirements.txt"),
        "--quiet",
    ])
    
    print("  Ensuring Playwright Chromium browser is installed...")
    try:
        subprocess.check_call([
            sys.executable, "-m", "playwright", "install", "chromium"
        ])
    except Exception as e:
        print(f"  ⚠️ Warning: Failed to run 'playwright install chromium': {e}")


    # ── Step 3: Verify critical packages are importable ────────────────────────
    print("\n[STEP 3] Verifying critical packages...")
    errors = []

    # Check playwright_stealth and its JS files
    try:
        import playwright_stealth
        stealth_dir = Path(playwright_stealth.__file__).parent
        js_file = stealth_dir / "js" / "generate.magic.arrays.js"
        if js_file.exists():
            print(f"  ✅ playwright_stealth OK — JS at: {stealth_dir / 'js'}")
        else:
            errors.append(
                f"playwright_stealth installed but JS files missing at: {stealth_dir / 'js'}\n"
                f"  Expected file: {js_file}"
            )
    except ImportError:
        errors.append(
            "playwright_stealth is NOT installed.\n"
            "  Run: pip install playwright-stealth>=2.0.0"
        )

    # Check pywebview
    try:
        import webview
        print(f"  ✅ pywebview OK")
    except ImportError:
        errors.append(
            "pywebview is NOT installed.\n"
            "  Run: pip install pywebview>=5.0"
        )

    # Check pyinstaller
    try:
        import PyInstaller
        print(f"  ✅ PyInstaller OK — version: {PyInstaller.__version__}")
    except ImportError:
        errors.append(
            "PyInstaller is NOT installed.\n"
            "  Run: pip install pyinstaller>=6.0"
        )

    if errors:
        print("\n" + "=" * 60)
        print("  [FATAL] Pre-build verification FAILED:")
        for e in errors:
            print(f"  ❌ {e}")
        print("=" * 60)
        sys.exit(1)

    # ── Step 3b: REMOVED auto-patching of source files ──────────────────────
    # We now handle sys.stderr gracefully in main.py directly.

    # ── Step 4: Run PyInstaller ────────────────────────────────────────────────
    temp_base = Path(tempfile.gettempdir())
    temp_work = temp_base / "mailagent_build_work"
    temp_dist = temp_base / "mailagent_build_dist"
    final_dist = root / "dist"
    spec_path = root / "mailagent.spec"

    # Clean previous temp work and dist
    if temp_work.exists():
        shutil.rmtree(temp_work, ignore_errors=True)
    if temp_dist.exists():
        shutil.rmtree(temp_dist, ignore_errors=True)

    print(f"\n[STEP 4] Building with PyInstaller...")
    print(f"  Spec: {spec_path}")
    print(f"  Work dir: {temp_work}")
    print(f"  Temp dist: {temp_dist}")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(spec_path),
        "--workpath", str(temp_work),
        "--distpath", str(temp_dist),
        "--noconfirm",
    ]

    ret = subprocess.call(cmd)
    if ret != 0:
        print(f"\n[FATAL] PyInstaller build failed with exit code {ret}.")
        sys.exit(ret)

    # ── Step 5: Post-build verification ────────────────────────────────────────
    print("\n[STEP 5] Post-build verification...")
    build_dir = temp_dist / "MailAgent"
    internal_dir = build_dir / "_internal"

    post_errors = []

    # 5a. Check playwright_stealth JS files
    stealth_js = internal_dir / "playwright_stealth" / "js" / "generate.magic.arrays.js"
    if stealth_js.exists():
        print(f"  ✅ playwright_stealth/js bundled correctly")
    else:
        post_errors.append(f"playwright_stealth JS files NOT found in build at: {stealth_js.parent}")
        # Attempt auto-fix: copy from site-packages
        try:
            import playwright_stealth as _ps
            src_js = Path(_ps.__file__).parent / "js"
            dst_js = internal_dir / "playwright_stealth" / "js"
            if src_js.exists():
                dst_js.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src_js, dst_js, dirs_exist_ok=True)
                print(f"  🔧 AUTO-FIX: Copied playwright_stealth/js from {src_js}")
                if (dst_js / "generate.magic.arrays.js").exists():
                    post_errors.pop()  # Remove the error since we fixed it
                    print(f"  ✅ playwright_stealth/js auto-fix successful")
        except Exception as fix_err:
            post_errors.append(f"Auto-fix also failed: {fix_err}")

    # 5b. Check pywebview is bundled
    webview_check = list(internal_dir.glob("webview*")) if internal_dir.exists() else []
    if webview_check:
        print(f"  ✅ pywebview bundled: {[p.name for p in webview_check[:3]]}")
    else:
        post_errors.append("pywebview modules NOT found in _internal/")
        # Attempt auto-fix: copy webview package
        try:
            import webview as _wv
            src_webview = Path(_wv.__file__).parent
            dst_webview = internal_dir / "webview"
            if src_webview.exists():
                shutil.copytree(src_webview, dst_webview, dirs_exist_ok=True)
                print(f"  🔧 AUTO-FIX: Copied webview from {src_webview}")
                post_errors.pop()
                print(f"  ✅ pywebview auto-fix successful")
        except Exception as fix_err:
            post_errors.append(f"webview auto-fix also failed: {fix_err}")

    # 5c. Check pythonnet / clr_loader is bundled (required for pywebview on Windows)
    if sys.platform == "win32":
        pythonnet_dll = list(internal_dir.glob("Python.Runtime.dll"))
        if not pythonnet_dll:
            print("  ⚠️ Python.Runtime.dll not found in _internal/. Attempting auto-fix...")
            try:
                import clr
                src_clr = Path(clr.__file__).parent
                for dll in src_clr.glob("*.dll"):
                    shutil.copy2(dll, internal_dir)
                print("  ✅ pythonnet (clr) DLLs auto-fix successful")
            except Exception as fix_err:
                post_errors.append(f"pythonnet auto-fix failed: {fix_err}")

        # Also ensure clr_loader data is present
        clr_loader_dir = internal_dir / "clr_loader"
        if not clr_loader_dir.exists():
            print("  ⚠️ clr_loader not found in _internal/. Attempting auto-fix...")
            try:
                import clr_loader as _cl
                src_cl = Path(_cl.__file__).parent
                if src_cl.exists():
                    shutil.copytree(src_cl, clr_loader_dir, dirs_exist_ok=True)
                    print(f"  ✅ clr_loader auto-fix successful (from {src_cl})")
            except Exception as fix_err:
                print(f"  ⚠️ clr_loader auto-fix failed: {fix_err}")

    # 5d. Check the EXE exists
    exe_path = build_dir / "MailAgent.exe"
    if exe_path.exists():
        print(f"  ✅ MailAgent.exe exists ({exe_path.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        post_errors.append(f"MailAgent.exe NOT found at: {exe_path}")

    if post_errors:
        print("\n" + "=" * 60)
        print("  [FATAL] Post-build verification FAILED:")
        for e in post_errors:
            print(f"  ❌ {e}")
        print("=" * 60)
        sys.exit(1)

    # ── Step 6: Copy to project dist/ ──────────────────────────────────────────
    print(f"\n[STEP 6] Copying build to {final_dist / 'MailAgent'}...")
    target_dir = final_dist / "MailAgent"
    # Remove old build entirely to avoid stale files
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    final_dist.mkdir(parents=True, exist_ok=True)
    shutil.copytree(build_dir, target_dir, dirs_exist_ok=True)

    final_exe = target_dir / "MailAgent.exe"
    print("\n" + "=" * 60)
    print("  ✅ BUILD SUCCESSFUL!")
    print(f"  Executable: {final_exe}")
    print(f"  Size: {final_exe.stat().st_size / 1024 / 1024:.1f} MB")
    print("=" * 60)


if __name__ == "__main__":
    main()

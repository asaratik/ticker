#!/usr/bin/env python3
"""
Cross-platform build script: installs dependencies, runs the test suite,
and packages the app -- Ticker.exe on Windows, Ticker.app on macOS,
a Ticker binary on Linux. Single source of truth for the packaging
command, used both for local development and by CI
(.github/workflows/ci.yml), so it only lives in one place.

Usage:
    python build.py                # install deps, test, package
    python build.py --skip-tests   # skip straight to packaging
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def run(cmd: list):
    print(f"\n== {' '.join(cmd)} ==", flush=True)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-tests", action="store_true",
                         help="skip the test suite and go straight to packaging")
    args = parser.parse_args()

    run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
         "-r", "requirements-dev.txt"])

    if args.skip_tests:
        print("\n== Skipping tests (--skip-tests) ==")
    else:
        run([sys.executable, "-m", "pytest", "-v"])

    pyinstaller_cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--windowed", "--name", "Ticker", "--noconfirm",
    ]
    if sys.platform == "win32":
        # bleak's Windows BLE backend uses the winrt bindings, which
        # PyInstaller's static analysis doesn't fully trace on its own --
        # without this the packaged exe silently fails to connect.
        pyinstaller_cmd += ["--collect-all", "winrt"]
    pyinstaller_cmd.append("hrm_app.py")

    run(pyinstaller_cmd)

    print("\nBuild complete -- see dist/")


if __name__ == "__main__":
    main()

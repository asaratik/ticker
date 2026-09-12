#!/usr/bin/env python3
"""
Cross-platform build script: installs dependencies, runs the test suite,
and packages the app -- a Ticker folder on Windows and Linux, Ticker.app on
macOS. Single source of truth for the packaging command, used both for local
development and by CI (.github/workflows/ci.yml), so it only lives in one
place.

Packaging options live in packaging/ticker.spec, not here. Passing them on
the PyInstaller command line would make it generate and overwrite a spec of
its own, which is how the migration data files went missing from packaged
builds once already.

Usage:
    python build.py                # install deps, test, package
    python build.py --skip-tests   # skip straight to packaging
    python build.py --installer    # also build the Windows installer
    python build.py --winget-only  # fill in the winget manifests from a build
    python build.py --dmg-only     # macOS: wrap the signed app in a .dmg
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "packaging" / "ticker.spec"
DIST = ROOT / "dist"
WINGET = ROOT / "packaging" / "windows" / "winget"
LOCK = ROOT / "requirements-build.lock"
BUILD_INFO = ROOT / "build" / "build-info.json"

# Only used to build the InstallerUrl. CI overrides it with the repository
# the release is actually being cut from.
DEFAULT_REPO = "ashokaratikatla/ticker"

# Where Inno Setup puts ISCC.exe. Only used with --installer, and only on
# Windows; CI installs it explicitly.
INNO_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
]


def run(cmd: list):
    print("\n== {} ==".format(" ".join(str(c) for c in cmd)), flush=True)
    result = subprocess.run([str(c) for c in cmd])
    if result.returncode != 0:
        sys.exit(result.returncode)


def sha256(path) -> str:
    """Hex digest of a file, read in blocks.

    A onedir zip is tens of megabytes and CI runners are not generous with
    memory, so this never holds the whole file at once.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256sums(paths) -> str:
    """SHA256SUMS in the format sha256sum -c expects.

    Published with every release so anyone can verify a download
    independently of whether it happens to be signed yet (section 12.3).
    """
    lines = ["{}  {}".format(sha256(path), Path(path).name)
             for path in sorted(paths)]
    return "\n".join(lines) + "\n"


def artifacts() -> list:
    """The files a release publishes, per platform."""
    if sys.platform == "darwin":
        found = [p for p in (DIST / "Ticker.zip",) if p.exists()]
        found.extend(DIST.glob("Ticker-*-macos-arm64.dmg"))
        return found
    found = []
    for pattern in ("Ticker.zip", "Ticker-*-windows-x86_64-setup.exe",
                    "Ticker.tar.gz", "Ticker-*-linux-x86_64.AppImage"):
        found.extend(DIST.glob(pattern))
    return found


def build_dmg(version: str) -> int:
    """Wrap Ticker.app in a compressed disk image.

    The macOS counterpart of the Inno Setup installer: the thing people
    actually download, and the thing that gets signed, notarized and
    stapled (section 12.5). Built here rather than in the workflow for the
    same reason the installer is -- one packaging command, used the same
    way locally and in CI.

    Run it *after* the app is signed, notarized and stapled. hdiutil copies
    the bundle as it finds it, so an image built from an unstapled app
    contains an unstapled app no matter what happens to the image
    afterwards.
    """
    app = DIST / "Ticker.app"
    if not app.exists():
        print("no {} to wrap -- run the build first".format(app),
              file=sys.stderr)
        return 1
    dmg = DIST / "Ticker-{}-macos-arm64.dmg".format(version)
    # -ov: a re-run in a dirty dist/ should replace the image rather than
    # fail on it. UDZO is the compressed read-only format every macOS
    # release ships.
    run(["hdiutil", "create", "-volname", "Ticker", "-srcfolder", app,
         "-ov", "-format", "UDZO", dmg])
    print("\n== disk image ==\n{}".format(dmg))
    return 0


def build_appimage(version: str) -> int:
    """Wrap dist/Ticker in an AppImage (section 12.6).

    The Linux counterpart of --installer and --dmg-only. The work itself
    lives in packaging/linux/build-appimage.sh rather than here: it is all
    file shuffling and one appimagetool invocation, and a shell script is
    the honest shape for that. This wrapper exists so every platform is
    packaged through the same command.
    """
    source = DIST / "Ticker"
    if not source.exists():
        print("no {} to wrap -- run the build first".format(source),
              file=sys.stderr)
        return 1
    script = ROOT / "packaging" / "linux" / "build-appimage.sh"
    run(["bash", str(script), version])
    return 0


def find_inno():
    for candidate in INNO_CANDIDATES:
        if candidate.exists():
            return candidate
    found = shutil.which("ISCC")
    return Path(found) if found else None


def build_installer(version: str):
    """Wrap the onedir folder in an Inno Setup installer."""
    iscc = find_inno()
    if iscc is None:
        print("\nInno Setup (ISCC.exe) not found -- skipping the installer.\n"
              "Install it from https://jrsoftware.org/isdl.php, or use the\n"
              "onedir folder in dist/Ticker directly.", file=sys.stderr)
        return False
    run([iscc, "/DAppVersion={}".format(version),
         ROOT / "packaging" / "windows" / "ticker.iss"])
    return True


def write_hashes(required: bool) -> int:
    """Write dist/SHA256SUMS over whatever release artifacts are present.

    `required` makes an empty result an error. CI recomputes these after
    signing and packaging -- both change the bytes -- and a silently empty
    SHA256SUMS published beside real binaries is worse than none at all,
    since anyone checking it would conclude the download was unverifiable
    rather than that the release was built wrong.
    """
    files = artifacts()
    if not files:
        if required:
            print("no release artifacts in {} to hash".format(DIST),
                  file=sys.stderr)
            return 1
        return 0
    sums = DIST / "SHA256SUMS"
    sums.write_text(sha256sums(files), encoding="utf-8")
    print("\n== SHA256SUMS ==")
    print(sums.read_text(encoding="utf-8"), end="")
    return 0


def set_field(text: str, key: str, value: str) -> str:
    """Replace one scalar field in a winget manifest, in place.

    Line substitution rather than a YAML round-trip on purpose: the
    manifests are hand-written and heavily commented, explaining why each
    value is what it is, and every YAML library within reach drops comments
    on the way back out. Missing keys raise -- an unsubstituted placeholder
    would sail through winget's schema and fail validation later, on the
    submission, where the cause is much less obvious.
    """
    pattern = re.compile(r"^([ \t]*)" + re.escape(key) + r":.*$", re.M)
    if not pattern.search(text):
        raise KeyError("no {} field to substitute".format(key))
    return pattern.sub(
        lambda m: "{}{}: {}".format(m.group(1), key, value), text, count=1)


def winget_manifests(version: str, installer: str, digest: str,
                     repo: str, release_date: str) -> dict:
    """The tracked winget manifests with a release's real values filled in.

    Keyed by filename; winget requires the three files to be named after the
    package identifier, so the names carry through unchanged.
    """
    url = "https://github.com/{}/releases/download/v{}/{}".format(
        repo, version, installer)
    filled = {}
    for path in sorted(WINGET.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        text = set_field(text, "PackageVersion", version)
        if "ManifestType: installer" in text:
            text = set_field(text, "InstallerUrl", url)
            # Uppercase: both validate, but it is what the winget tooling
            # emits and what every manifest in winget-pkgs looks like.
            text = set_field(text, "InstallerSha256", digest.upper())
            text = set_field(text, "ReleaseDate", release_date)
        filled[path.name] = text
    return filled


def find_installer(version: str):
    """The built installer for a version, if it is there to be hashed."""
    exact = DIST / "Ticker-{}-windows-x86_64-setup.exe".format(version)
    if exact.exists():
        return exact
    found = sorted(DIST.glob("Ticker-*-windows-x86_64-setup.exe"))
    return found[0] if len(found) == 1 else None


def write_winget(version: str, repo: str) -> int:
    """Write dist/winget/ -- the manifests, ready to submit.

    Run this after the installer is signed: signing changes the bytes, and a
    wrong InstallerSha256 is the most common reason a winget submission
    fails validation. The tracked manifests keep their zeroed placeholder
    hash so nobody is tempted to maintain one by hand.
    """
    if version == "0.0.0":
        # The argparse default, which means the tag never made it in. A
        # manifest pointing at a v0.0.0 release that doesn't exist is worse
        # than no manifest.
        print("refusing to write winget manifests for the placeholder "
              "version 0.0.0 -- pass --version or set TICKER_VERSION",
              file=sys.stderr)
        return 1

    installer = find_installer(version)
    if installer is None:
        print("no Ticker-{}-windows-x86_64-setup.exe in {} to publish a winget manifest "
              "for".format(version, DIST), file=sys.stderr)
        return 1

    manifests = winget_manifests(
        version=version,
        installer=installer.name,
        digest=sha256(installer),
        repo=repo,
        release_date=datetime.now(timezone.utc).date().isoformat(),
    )

    out = DIST / "winget"
    out.mkdir(parents=True, exist_ok=True)
    for name, text in manifests.items():
        (out / name).write_text(text, encoding="utf-8")
    print("\n== winget manifests ==")
    print("{} for {} ({})".format(out, version, installer.name))
    return 0


def write_build_info(version: str, commit: str = "") -> None:
    if not commit:
        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT,
                text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            commit = "unknown"
    BUILD_INFO.parent.mkdir(parents=True, exist_ok=True)
    BUILD_INFO.write_text(json.dumps({"version": version, "commit": commit},
                                     sort_keys=True) + "\n", encoding="utf-8")


def packaged_executable() -> Path:
    if sys.platform == "darwin":
        return DIST / "Ticker.app" / "Contents" / "MacOS" / "Ticker"
    if sys.platform == "win32":
        return DIST / "Ticker" / "Ticker.exe"
    return DIST / "Ticker" / "Ticker"


def smoke_packaged() -> None:
    executable = packaged_executable()
    if not executable.exists():
        raise RuntimeError("packaged executable is missing: {}".format(executable))
    with tempfile.TemporaryDirectory(prefix="ticker-build-smoke-") as folder:
        result = Path(folder) / "result.json"
        completed = subprocess.run(
            [str(executable), "--smoke-test", str(result)], timeout=45)
        if completed.returncode or not result.exists():
            raise RuntimeError("packaged smoke test failed with exit code {}".format(
                completed.returncode))
        payload = json.loads(result.read_text(encoding="utf-8"))
        if payload.get("ok") is not True:
            raise RuntimeError("packaged smoke test did not report success")


def stage_release(platform_name: str, version: str, commit: str,
                  signed: bool) -> int:
    """Create one platform's uniquely named, self-describing release set."""
    if version == "0.0.0" or not commit:
        print("--stage-release needs a real --version and --commit",
              file=sys.stderr)
        return 1
    mapping = {
        "windows": [
            (DIST / "Ticker.zip",
             "Ticker-{}-windows-x86_64.zip".format(version)),
            (find_installer(version),
             "Ticker-{}-windows-x86_64-setup.exe".format(version)),
        ],
        "macos": [
            (DIST / "Ticker.zip", "Ticker-{}-macos-arm64.zip".format(version)),
            (DIST / "Ticker-{}-macos-arm64.dmg".format(version),
             "Ticker-{}-macos-arm64.dmg".format(version)),
        ],
        "linux": [
            (DIST / "Ticker.tar.gz",
             "Ticker-{}-linux-x86_64.tar.gz".format(version)),
            (DIST / "Ticker-{}-linux-x86_64.AppImage".format(version),
             "Ticker-{}-linux-x86_64.AppImage".format(version)),
        ],
    }
    sources = mapping[platform_name]
    missing = [str(path) for path, _ in sources if path is None or not path.exists()]
    if missing:
        print("missing release artifacts: {}".format(", ".join(missing)),
              file=sys.stderr)
        return 1
    out = DIST / "release-{}".format(platform_name)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    files = []
    for source, name in sources:
        target = out / name
        shutil.copy2(source, target)
        files.append(target)
    if platform_name == "windows" and (DIST / "winget").exists():
        for manifest in (DIST / "winget").glob("*.yaml"):
            shutil.copy2(manifest, out / manifest.name)
    sums = out / "SHA256SUMS-{}.txt".format(platform_name)
    sums.write_text(sha256sums(files), encoding="utf-8")
    manifest = {
        "version": version, "commit": commit, "platform": platform_name,
        "signed": bool(signed),
        "files": [{"name": path.name, "sha256": sha256(path),
                   "bytes": path.stat().st_size} for path in files],
    }
    (out / "manifest-{}.json".format(platform_name)).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("staged {} release files in {}".format(platform_name, out))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the test suite and go straight to packaging")
    parser.add_argument("--installer", action="store_true",
                        help="also build the Windows installer (needs Inno Setup)")
    parser.add_argument("--installer-only", action="store_true",
                        help="wrap the existing signed Windows folder; do not rebuild it")
    parser.add_argument("--version", default=os.environ.get("TICKER_VERSION", "0.0.0"),
                        help="version stamped into the installer")
    parser.add_argument("--hashes-only", action="store_true",
                        help="rewrite dist/SHA256SUMS and do nothing else")
    parser.add_argument("--winget-only", action="store_true",
                        help="write dist/winget/ from the built installer "
                             "and do nothing else")
    parser.add_argument("--dmg-only", action="store_true",
                        help="wrap the signed dist/Ticker.app in a .dmg "
                             "and do nothing else (macOS)")
    parser.add_argument("--appimage-only", action="store_true",
                        help="wrap dist/Ticker in an AppImage "
                             "and do nothing else (Linux)")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY",
                                                         DEFAULT_REPO),
                        help="owner/name the winget InstallerUrl points at")
    parser.add_argument("--stage-release", choices=("windows", "macos", "linux"),
                        help="stage uniquely named files, checksums and a manifest")
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--signed", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.installer_only:
        if sys.platform != "win32":
            print("--installer-only is Windows-only", file=sys.stderr)
            return 1
        return 0 if build_installer(args.version) else 1

    if args.stage_release:
        return stage_release(args.stage_release, args.version, args.commit,
                             args.signed)

    if args.hashes_only:
        return write_hashes(required=True)

    if args.winget_only:
        return write_winget(args.version, args.repo)

    if args.dmg_only:
        if sys.platform != "darwin":
            print("--dmg-only is macOS-only (hdiutil)", file=sys.stderr)
            return 1
        return build_dmg(args.version)

    if args.appimage_only:
        if not sys.platform.startswith("linux"):
            print("--appimage-only is Linux-only (appimagetool)",
                  file=sys.stderr)
            return 1
        return build_appimage(args.version)

    run([sys.executable, "-m", "pip", "install", "--quiet",
         "--require-hashes", "--only-binary=:all:", "-r", LOCK])

    if args.skip_tests:
        print("\n== Skipping tests (--skip-tests) ==")
    else:
        run([sys.executable, "-m", "pyflakes", "ticker", "build.py",
             "config.py", "storage.py", "hr_source.py", "ble_source.py",
             "http_source.py"])
        run([sys.executable, "-m", "pytest", "-v"])

    write_build_info(args.version, args.commit)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", SPEC])
    smoke_packaged()

    if args.installer:
        if sys.platform != "win32":
            print("\n--installer is Windows-only; skipping.", file=sys.stderr)
        else:
            build_installer(args.version)

    write_hashes(required=False)
    print("\nBuild complete -- see dist/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

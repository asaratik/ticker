"""Verify every platform artifact before creating and publishing a draft."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

PLATFORM_FILES = {
    "windows": ("Ticker-{version}-windows-x86_64.zip",
                "Ticker-{version}-windows-x86_64-setup.exe"),
    "macos": ("Ticker-{version}-macos-arm64.zip",
              "Ticker-{version}-macos-arm64.dmg"),
    "linux": ("Ticker-{version}-linux-x86_64.tar.gz",
              "Ticker-{version}-linux-x86_64.AppImage"),
}
ROOT = Path(__file__).resolve().parent


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_assets(folder: Path, tag: str, commit: str,
                  require_signing: bool = False):
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.]+)?", tag):
        raise ValueError("invalid release tag")
    folder, version = Path(folder), tag[1:]
    expected = set()
    for platform_name, templates in PLATFORM_FILES.items():
        files = [template.format(version=version) for template in templates]
        expected.update(files)
        expected.add("SHA256SUMS-{}.txt".format(platform_name))
        expected.add("manifest-{}.json".format(platform_name))

        manifest = json.loads((folder / "manifest-{}.json".format(
            platform_name)).read_text(encoding="utf-8"))
        if (manifest.get("version"), manifest.get("commit"),
                manifest.get("platform")) != (version, commit, platform_name):
            raise ValueError("build identity mismatch: {}".format(platform_name))
        if require_signing and platform_name != "linux" and not manifest.get("signed"):
            raise ValueError("missing required signing: {}".format(platform_name))
        declared = {item.get("name"): item for item in manifest.get("files", [])}
        if set(declared) != set(files):
            raise ValueError("manifest file list mismatch: {}".format(platform_name))

        checksum_lines = (folder / "SHA256SUMS-{}.txt".format(
            platform_name)).read_text(encoding="utf-8").splitlines()
        checksums = dict(line.split("  ", 1)[::-1] for line in checksum_lines)
        for name in files:
            path = folder / name
            digest = _sha256(path)
            if checksums.get(name) != digest:
                raise ValueError("checksum mismatch: {}".format(name))
            item = declared[name]
            if item.get("sha256") != digest or item.get("bytes") != path.stat().st_size:
                raise ValueError("manifest metadata mismatch: {}".format(name))

    winget = {path.name for path in
              (ROOT / "packaging" / "windows" / "winget").glob("*.yaml")}
    expected.update(winget)
    actual = {path.name for path in folder.iterdir() if path.is_file()}
    if actual != expected:
        missing, extra = sorted(expected - actual), sorted(actual - expected)
        raise ValueError("incomplete release set; missing={} extra={}".format(
            missing, extra))
    return sorted(folder.iterdir())


def main() -> None:
    tag, commit = os.environ["RELEASE_TAG"], os.environ["RELEASE_COMMIT"]
    files = verify_assets(ROOT / "release-assets", tag, commit,
                          os.environ.get("SIGN_RELEASES") == "true")
    actual_commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", "refs/tags/{}^{{commit}}".format(tag)],
        text=True).strip()
    if actual_commit != commit:
        raise ValueError("tag moved after validation; refusing to publish")

    existing = subprocess.run(["gh", "release", "view", tag,
                               "--json", "isDraft,assets"],
                              capture_output=True, text=True)
    if existing.returncode == 0:
        state = json.loads(existing.stdout)
        if not state["isDraft"]:
            raise ValueError("release is already published; use a new version tag")
        # A previous failed attempt may have uploaded only part of the set.
        # Clear that draft before the verified set is uploaded, so stale
        # assets cannot make a successful retry look complete or ambiguous.
        for asset in state.get("assets", []):
            subprocess.run(["gh", "release", "delete-asset", tag,
                            asset["name"], "--yes"], check=True)
    else:
        subprocess.run(
            ["gh", "release", "create", tag, "--draft", "--verify-tag",
             "--target", commit, "--title", "Ticker {}".format(tag),
             "--notes-file", str(ROOT / "docs" / "release-notes.md")], check=True)
    subprocess.run(["gh", "release", "upload", tag, "--clobber",
                    *map(str, files)], check=True)

    uploaded = json.loads(subprocess.check_output(
        ["gh", "release", "view", tag, "--json", "assets"], text=True))
    expected_sizes = {path.name: path.stat().st_size for path in files}
    actual_sizes = {asset["name"]: asset["size"] for asset in uploaded["assets"]}
    if actual_sizes != expected_sizes:
        raise RuntimeError("uploaded assets are incomplete; release remains a draft")
    command = ["gh", "release", "edit", tag, "--draft=false"]
    if "-" in tag:
        command.append("--prerelease")
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()

import hashlib
import json

import pytest

import release


def assets(folder):
    for platform_name, templates in release.PLATFORM_FILES.items():
        entries, sums = [], []
        for template in templates:
            name = template.format(version="1.2.3")
            payload = (platform_name + name).encode()
            (folder / name).write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            entries.append({"name": name, "sha256": digest,
                            "bytes": len(payload)})
            sums.append("{}  {}".format(digest, name))
        (folder / "SHA256SUMS-{}.txt".format(platform_name)).write_text(
            "\n".join(sums) + "\n", encoding="utf-8")
        (folder / "manifest-{}.json".format(platform_name)).write_text(
            json.dumps({"version": "1.2.3", "commit": "abc",
                        "platform": platform_name, "signed": True,
                        "files": entries}), encoding="utf-8")
    for path in (release.Path("packaging") / "windows" / "winget").glob("*.yaml"):
        (folder / path.name).write_text("manifest", encoding="utf-8")


def test_complete_release_is_required(tmp_path):
    assets(tmp_path)
    assert len(release.verify_assets(tmp_path, "v1.2.3", "abc", True)) == 15
    (tmp_path / "Ticker-1.2.3-linux-x86_64.AppImage").unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        release.verify_assets(tmp_path, "v1.2.3", "abc")


def test_bad_checksum_and_wrong_commit_are_rejected(tmp_path):
    assets(tmp_path)
    with pytest.raises(ValueError, match="identity"):
        release.verify_assets(tmp_path, "v1.2.3", "different")
    (tmp_path / "Ticker-1.2.3-windows-x86_64.zip").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        release.verify_assets(tmp_path, "v1.2.3", "abc")


def test_signing_requirement_cannot_be_silently_skipped(tmp_path):
    assets(tmp_path)
    path = tmp_path / "manifest-macos.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["signed"] = False
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="signing"):
        release.verify_assets(tmp_path, "v1.2.3", "abc", True)


@pytest.mark.parametrize("tag", ["main", "../../tag", "v1.0", "v1.2.3;echo bad"])
def test_invalid_release_tag(tag, tmp_path):
    with pytest.raises(ValueError, match="invalid release tag"):
        release.verify_assets(tmp_path, tag, "abc")

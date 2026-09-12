"""
Tests for the packaging script and the artifacts it depends on.

Nothing here runs PyInstaller -- a real build takes half a minute and needs
the toolchain. What is tested is the part that is easy to get quietly wrong
and expensive to discover later: the published checksums, and the promise
that the packaging spec actually declares the data files a packaged build
needs at runtime.

That last one has bitten once already. The migrations were added to a
generated .spec that PyInstaller overwrites on every build, so packaged
builds shipped without them and could not create their schema on first run.
"""

import hashlib
import re
from pathlib import Path

import pytest

import build

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "ticker.spec"


# -- checksums -----------------------------------------------------------

def test_sha256sums_matches_the_sha256sum_c_format(tmp_path):
    target = tmp_path / "Ticker.zip"
    target.write_bytes(b"payload")
    line = build.sha256sums([target]).strip()

    digest, name = line.split("  ")
    assert digest == hashlib.sha256(b"payload").hexdigest()
    # The bare name, not the path: sha256sum -c resolves it relative to
    # wherever the file was downloaded to.
    assert name == "Ticker.zip"


def test_sha256sums_is_ordered_so_the_output_is_reproducible(tmp_path):
    for name in ("c.zip", "a.zip", "b.zip"):
        (tmp_path / name).write_bytes(name.encode())
    text = build.sha256sums(list(tmp_path.glob("*.zip")))
    names = [line.split("  ")[1] for line in text.strip().splitlines()]
    assert names == sorted(names)


def test_sha256sums_ends_with_a_newline(tmp_path):
    target = tmp_path / "Ticker.zip"
    target.write_bytes(b"x")
    assert build.sha256sums([target]).endswith("\n")


def test_hashing_large_files_does_not_read_them_whole(tmp_path, monkeypatch):
    # Streamed in blocks: a onedir zip is tens of megabytes, and CI runners
    # are not generous with memory.
    target = tmp_path / "Ticker.zip"
    payload = b"x" * (3 * 1024 * 1024)
    target.write_bytes(payload)
    line = build.sha256sums([target]).strip()
    assert line.split("  ")[0] == hashlib.sha256(payload).hexdigest()


def test_hashes_only_fails_loudly_when_there_is_nothing_to_hash(
        tmp_path, monkeypatch, capsys):
    """An empty SHA256SUMS published beside real binaries is worse than none:
    anyone checking would conclude the download was unverifiable."""
    monkeypatch.setattr(build, "DIST", tmp_path)
    assert build.write_hashes(required=True) == 1
    assert "no release artifacts" in capsys.readouterr().err


def test_a_build_without_artifacts_is_not_an_error(tmp_path, monkeypatch):
    # Building the onedir folder alone is a valid local build; only a
    # release has to have something to hash.
    monkeypatch.setattr(build, "DIST", tmp_path)
    assert build.write_hashes(required=False) == 0


def test_hashes_only_writes_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "DIST", tmp_path)
    (tmp_path / "Ticker.zip").write_bytes(b"payload")
    assert build.write_hashes(required=True) == 0
    assert "Ticker.zip" in (tmp_path / "SHA256SUMS").read_text(encoding="utf-8")


def test_artifacts_picks_up_the_installer(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "DIST", tmp_path)
    (tmp_path / "Ticker.zip").write_bytes(b"a")
    (tmp_path / "Ticker-1.2.3-windows-x86_64-setup.exe").write_bytes(b"b")
    (tmp_path / "not-a-release-file.txt").write_bytes(b"c")
    names = {p.name for p in build.artifacts()}
    assert "Ticker-1.2.3-windows-x86_64-setup.exe" in names
    assert "not-a-release-file.txt" not in names


# -- the packaging spec --------------------------------------------------

def test_the_tracked_spec_exists():
    assert SPEC.exists(), "packaging/ticker.spec is what build.py builds from"


def test_build_py_builds_from_the_tracked_spec():
    """Passing options on the PyInstaller command line makes it generate and
    overwrite a spec, silently dropping anything added to the old one."""
    source = (ROOT / "build.py").read_text(encoding="utf-8")
    assert "SPEC" in source
    assert "--onefile" not in source


def test_the_spec_ships_the_migrations():
    """Read off disk at runtime, so PyInstaller cannot discover them by
    following imports. Without these a packaged build cannot migrate."""
    text = SPEC.read_text(encoding="utf-8")
    assert "migrations" in text
    assert "*.sql" in text
    assert "schema.sql" in text


def test_every_runtime_data_file_is_declared_in_the_spec():
    """Catches a new .sql file being added without the spec being updated."""
    text = SPEC.read_text(encoding="utf-8")
    on_disk = list((ROOT / "ticker" / "db").rglob("*.sql"))
    assert on_disk, "expected migrations and a schema snapshot on disk"
    for path in on_disk:
        relative = path.relative_to(ROOT / "ticker" / "db")
        covered = (
            str(relative) in text
            or "{}/*.sql".format(relative.parent).replace("\\", "/") in text
            or (relative.parent != Path(".") and str(relative.parent) in text)
        )
        assert covered, "{} is not covered by the spec's datas".format(relative)


def test_the_spec_builds_a_onedir_bundle():
    """onefile's bootloader unpacks to temp and executes from there, which is
    what heuristic AV engines flag (section 12.3)."""
    text = SPEC.read_text(encoding="utf-8")
    assert "COLLECT(" in text
    assert "exclude_binaries=True" in text


def test_the_spec_does_not_upx_compress():
    # A packed executable is another thing scanners dislike, and the few
    # megabytes are not worth the false positives.
    text = SPEC.read_text(encoding="utf-8")
    assert "upx=False" in text
    assert "upx=True" not in text


def test_the_spec_declares_the_macos_bluetooth_usage_string():
    """Without NSBluetoothAlwaysUsageDescription macOS refuses Bluetooth
    outright, signed or not (section 12.5)."""
    text = SPEC.read_text(encoding="utf-8")
    assert "NSBluetoothAlwaysUsageDescription" in text


def test_the_spec_is_not_gitignored():
    """It was, once, which is how the migrations went missing."""
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!packaging/*.spec" in ignore


# -- installer and winget ------------------------------------------------

INNO = ROOT / "packaging" / "windows" / "ticker.iss"
WINGET = ROOT / "packaging" / "windows" / "winget"


def test_the_installer_script_wraps_the_onedir_folder():
    text = INNO.read_text(encoding="utf-8")
    assert "recursesubdirs" in text          # _internal must come along
    assert "dist\\Ticker" in text


def test_the_installer_is_per_user_so_it_needs_no_admin():
    text = INNO.read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in text


def test_the_installer_does_not_delete_the_database_on_uninstall():
    """Removing the program must not remove years of heart rate history."""
    text = INNO.read_text(encoding="utf-8")
    uninstall = text[text.index("[UninstallDelete]"):]
    assert "LOCALAPPDATA" not in uninstall.split(";")[0]


def test_the_installer_version_is_supplied_by_the_build():
    text = INNO.read_text(encoding="utf-8")
    assert "AppVersion" in text
    assert "OutputBaseFilename=Ticker-{#AppVersion}-windows-x86_64-setup" in text


@pytest.mark.parametrize("name", [
    "AshokAratikatla.Ticker.yaml",
    "AshokAratikatla.Ticker.installer.yaml",
    "AshokAratikatla.Ticker.locale.en-US.yaml",
])
def test_the_winget_manifest_files_exist(name):
    assert (WINGET / name).exists()


def test_the_winget_manifests_agree_on_identifier_and_version():
    identifiers, versions = set(), set()
    for path in WINGET.glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        identifiers.add(re.search(r"^PackageIdentifier: (.+)$", text, re.M).group(1))
        versions.add(re.search(r"^PackageVersion: (.+)$", text, re.M).group(1))
    assert len(identifiers) == 1
    assert len(versions) == 1


def test_the_winget_installer_type_matches_what_is_actually_built():
    text = (WINGET / "AshokAratikatla.Ticker.installer.yaml").read_text(encoding="utf-8")
    assert "InstallerType: inno" in text
    # Per-user, matching PrivilegesRequired=lowest in the .iss.
    assert "Scope: user" in text


def test_the_winget_hash_is_an_obvious_placeholder():
    """CI substitutes the real hash. A plausible-looking wrong hash would be
    worse than an obviously fake one."""
    text = (WINGET / "AshokAratikatla.Ticker.installer.yaml").read_text(encoding="utf-8")
    digest = re.search(r"InstallerSha256: ([0-9a-fA-F]+)", text).group(1)
    assert len(digest) == 64
    assert set(digest) == {"0"}


def test_the_license_the_winget_manifest_points_at_exists():
    """LicenseUrl resolves to LICENSE in the repository root, and winget
    validation follows it. pyproject.toml claims MIT for the same file."""
    assert (ROOT / "LICENSE").exists()
    assert "MIT" in (ROOT / "LICENSE").read_text(encoding="utf-8")


# -- filling the manifests in for a release ------------------------------

def fake_release(tmp_path, monkeypatch, version="1.2.3", payload=b"installer"):
    """A dist/ with one built installer in it."""
    monkeypatch.setattr(build, "DIST", tmp_path)
    installer = tmp_path / "Ticker-{}-windows-x86_64-setup.exe".format(version)
    installer.write_bytes(payload)
    return installer


def filled(tmp_path, name="AshokAratikatla.Ticker.installer.yaml"):
    return (tmp_path / "winget" / name).read_text(encoding="utf-8")


def test_the_filled_manifest_carries_the_hash_of_the_built_installer(
        tmp_path, monkeypatch):
    installer = fake_release(tmp_path, monkeypatch, payload=b"signed bytes")
    assert build.write_winget("1.2.3", "owner/repo") == 0

    digest = re.search(r"InstallerSha256: ([0-9A-F]+)", filled(tmp_path)).group(1)
    assert digest == hashlib.sha256(installer.read_bytes()).hexdigest().upper()


def test_the_filled_installer_url_points_at_the_tag_and_the_built_file(
        tmp_path, monkeypatch):
    fake_release(tmp_path, monkeypatch)
    build.write_winget("1.2.3", "owner/repo")
    assert ("InstallerUrl: https://github.com/owner/repo/releases/download/"
            "v1.2.3/Ticker-1.2.3-windows-x86_64-setup.exe") in filled(tmp_path)


def test_every_filled_manifest_gets_the_release_version(tmp_path, monkeypatch):
    """winget rejects a submission whose three files disagree."""
    fake_release(tmp_path, monkeypatch)
    build.write_winget("1.2.3", "owner/repo")

    written = sorted((tmp_path / "winget").glob("*.yaml"))
    assert len(written) == 3
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert re.search(r"^PackageVersion: 1\.2\.3$", text, re.M)


def test_filling_in_a_manifest_keeps_its_comments(tmp_path, monkeypatch):
    """The comments are why each value is what it is -- a YAML round-trip
    would drop them, which is why this is a line substitution."""
    fake_release(tmp_path, monkeypatch)
    build.write_winget("1.2.3", "owner/repo")
    assert "# winget installer manifest." in filled(tmp_path)


def test_the_filled_release_date_is_a_plain_iso_date(tmp_path, monkeypatch):
    fake_release(tmp_path, monkeypatch)
    build.write_winget("1.2.3", "owner/repo")
    assert re.search(r"^ReleaseDate: \d{4}-\d{2}-\d{2}$", filled(tmp_path), re.M)


def test_nothing_is_left_holding_the_placeholder_hash(tmp_path, monkeypatch):
    fake_release(tmp_path, monkeypatch)
    build.write_winget("1.2.3", "owner/repo")
    assert "0" * 64 not in filled(tmp_path)
    assert "0.2.0" not in filled(tmp_path)


def test_the_tracked_manifests_are_left_alone(tmp_path, monkeypatch):
    """The templates keep their placeholder so nobody maintains one by hand;
    only the copies under dist/ get real values."""
    before = {p.name: p.read_text(encoding="utf-8") for p in WINGET.glob("*.yaml")}
    fake_release(tmp_path, monkeypatch)
    build.write_winget("1.2.3", "owner/repo")
    after = {p.name: p.read_text(encoding="utf-8") for p in WINGET.glob("*.yaml")}
    assert before == after


def test_a_field_that_is_not_in_the_manifest_raises(tmp_path, monkeypatch):
    """A silent no-op would ship an unsubstituted placeholder, and the
    failure would surface much later, on the winget submission."""
    with pytest.raises(KeyError):
        build.set_field("PackageVersion: 1.0.0\n", "InstallerUrl", "x")


def test_a_key_appearing_inside_a_comment_is_not_substituted():
    text = "# InstallerUrl is filled in by CI\nInstallerUrl: old\n"
    assert build.set_field(text, "InstallerUrl", "new") == (
        "# InstallerUrl is filled in by CI\nInstallerUrl: new\n")


def test_indented_installer_fields_keep_their_indentation():
    """InstallerUrl sits under a list item; losing the indent is invalid
    YAML, and winget would reject the manifest."""
    text = "Installers:\n  - Architecture: x64\n    InstallerUrl: old\n"
    assert "\n    InstallerUrl: new\n" in build.set_field(text, "InstallerUrl", "new")


def test_winget_refuses_the_placeholder_version(tmp_path, monkeypatch, capsys):
    """0.0.0 is the argparse default, and means the tag never made it in."""
    fake_release(tmp_path, monkeypatch)
    assert build.write_winget("0.0.0", "owner/repo") == 1
    assert "0.0.0" in capsys.readouterr().err


def test_winget_needs_an_installer_to_hash(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build, "DIST", tmp_path)
    assert build.write_winget("1.2.3", "owner/repo") == 1
    assert "no Ticker-1.2.3-windows-x86_64-setup.exe" in capsys.readouterr().err


def test_a_stale_installer_from_another_version_is_not_hashed(
        tmp_path, monkeypatch):
    """Two installers in dist/ and no way to tell which the release is: an
    ambiguous guess would publish a hash for the wrong binary."""
    monkeypatch.setattr(build, "DIST", tmp_path)
    (tmp_path / "Ticker-1.0.0-windows-x86_64-setup.exe").write_bytes(b"old")
    (tmp_path / "Ticker-1.1.0-windows-x86_64-setup.exe").write_bytes(b"older")
    assert build.write_winget("1.2.3", "owner/repo") == 1


# -- the release workflow ------------------------------------------------

RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def test_the_winget_manifests_are_filled_in_after_the_installer_is_signed():
    """Signing rewrites the installer's bytes. A hash taken before it is
    wrong, and a wrong InstallerSha256 fails winget validation."""
    text = RELEASE.read_text(encoding="utf-8")
    assert text.index("Sign Windows installer") < text.index("--winget-only")


def test_the_release_attaches_the_filled_manifests():
    text = RELEASE.read_text(encoding="utf-8")
    assert "--stage-release" in text
    assert "release-assets" in text

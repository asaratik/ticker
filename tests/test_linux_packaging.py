"""
Tests for the Linux AppImage build.

Nothing here runs appimagetool -- it is an AppImage itself, needs FUSE or an
extraction dance, and produces a 60MB artifact. What is tested is the part
that is cheap to get wrong and only shows up when a user double-clicks the
download: the AppDir layout, the desktop entry matching its icon, and the
onedir folder being copied whole rather than just its executable.

The last one is the interesting failure. `dist/Ticker/Ticker` runs fine on
the machine that built it, because _internal is sitting next to it. Copy
only the executable into the AppImage and it still builds, still passes a
smoke check that the file exists, and dies on the user's machine with an
import error.

Section 12.6 also says signing is not the trust mechanism here, so unlike
the Windows and macOS pipelines there is deliberately nothing about
certificates in this file.
"""

import re
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

import build

ROOT = Path(__file__).resolve().parent.parent
LINUX = ROOT / "packaging" / "linux"
SCRIPT = LINUX / "build-appimage.sh"
DESKTOP = LINUX / "ticker.desktop"
ICON = LINUX / "ticker.png"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"

# Spelled by ordinal so this file can never itself be saved with the
# very bytes it is asserting the absence of.
CRLF = bytes((13, 10))


@pytest.fixture(scope="module")
def script():
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow():
    return RELEASE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def desktop():
    entries = {}
    for line in DESKTOP.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#") and not line.startswith("["):
            key, value = line.split("=", 1)
            entries[key.strip()] = value.strip()
    return entries


# -- The desktop entry ------------------------------------------------------

def test_the_desktop_entry_exists():
    assert DESKTOP.exists()


def test_it_declares_itself_an_application(desktop):
    assert desktop["Type"] == "Application"


def test_it_is_not_a_terminal_program(desktop):
    """A Tk app launched with Terminal=true opens a stray console window."""
    assert desktop["Terminal"] == "false"


def test_the_exec_name_matches_what_pyinstaller_builds(desktop):
    """The spec names the executable Ticker; Exec has to agree."""
    spec = (ROOT / "packaging" / "ticker.spec").read_text(encoding="utf-8")
    assert 'name="Ticker"' in spec
    assert desktop["Exec"].split()[0] == "Ticker"


def test_the_icon_key_matches_the_icon_file(desktop):
    """appimagetool resolves Icon= against the AppDir root, without suffix."""
    assert desktop["Icon"] == ICON.stem


def test_the_categories_are_well_formed(desktop):
    """The desktop spec requires a trailing semicolon on list keys."""
    assert desktop["Categories"].endswith(";")
    assert all(part for part in desktop["Categories"].split(";") if part != "")


@pytest.mark.skipif(not Path("/usr/bin/desktop-file-validate").exists(),
                    reason="desktop-file-validate is not installed")
def test_the_desktop_entry_passes_the_freedesktop_validator():
    result = subprocess.run(
        ["desktop-file-validate", str(DESKTOP)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


# -- The icon ---------------------------------------------------------------

def test_the_icon_exists():
    assert ICON.exists()


def test_the_icon_is_a_real_png():
    raw = ICON.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_icon_is_square_and_a_size_launchers_expect():
    width, height = struct.unpack(">II", ICON.read_bytes()[16:24])
    assert width == height
    # hicolor's standard sizes; the script installs under 256x256.
    assert width in (16, 22, 24, 32, 48, 64, 128, 256, 512)


def test_the_icon_pixels_are_reproducible_from_its_generator(tmp_path):
    """Compression may differ across zlib versions; image data must not."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "make_icon", LINUX / "make_icon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    regenerated = tmp_path / "ticker.png"
    module.write_png(module.render(), regenerated)

    def image_data(path):
        raw = path.read_bytes()
        offset, chunks = 8, []
        while offset < len(raw):
            length = struct.unpack(">I", raw[offset:offset + 4])[0]
            kind = raw[offset + 4:offset + 8]
            payload = raw[offset + 8:offset + 8 + length]
            if kind == b"IDAT":
                chunks.append(payload)
            offset += 12 + length
        return zlib.decompress(b"".join(chunks))

    assert image_data(regenerated) == image_data(ICON)


# -- The build script -------------------------------------------------------

def test_the_script_exists():
    assert SCRIPT.exists()


def test_the_script_is_executable_in_git():
    """Cloned without the bit set, CI cannot run it."""
    listing = subprocess.run(
        ["git", "ls-files", "-s", "packaging/linux/build-appimage.sh"],
        cwd=ROOT, capture_output=True, text=True).stdout
    assert listing.startswith("100755"), listing or "file is not tracked"


def test_the_script_fails_on_error(script):
    """Without -e, a failed copy still produces an AppImage, just a broken one."""
    assert re.search(r"set -e[uo]*", script)


def test_the_whole_onedir_is_copied_not_just_the_executable(script):
    """Ticker cannot start without _internal beside it."""
    assert 'cp -a "$SOURCE/." "$APPDIR/usr/bin/"' in script


def test_the_appdir_is_cleaned_before_staging(script):
    """A stale AppDir would ship files from the previous build."""
    assert 'rm -rf "$APPDIR"' in script


def test_an_apprun_is_written_and_made_executable(script):
    """The runtime executes AppRun; a non-executable one just fails."""
    assert "$APPDIR/AppRun" in script
    assert 'chmod +x "$APPDIR/AppRun"' in script


def test_the_apprun_execs_rather_than_forking(script):
    """exec keeps the process tree flat so signals reach the app."""
    assert re.search(r'exec "\$APPDIR/usr/bin/Ticker"', script)


def test_the_diricon_is_provided(script):
    """appimagetool guesses at the icon without it."""
    assert ".DirIcon" in script


def test_the_icon_is_installed_under_hicolor(script):
    assert "usr/share/icons/hicolor/256x256/apps" in script


def test_the_script_refuses_to_run_without_a_build(script):
    assert 'if [ ! -d "$SOURCE" ]' in script


def test_appimagetool_is_not_downloaded_by_the_script(script):
    """Section 12.3's posture: no fetching and executing binaries mid-build.

    CI installs it as its own visible step. A download buried in a build
    script is invisible in the log and is the shape of supply-chain problem
    this project explicitly tries to avoid.
    """
    body = "\n".join(line for line in script.splitlines()
                     if not line.strip().startswith("#"))
    # The usage message may mention wget; an actual fetch-then-run may not.
    assert "curl " not in body
    fetches = [line for line in body.splitlines() if "wget" in line]
    assert all("APPIMAGETOOL" not in line for line in fetches)


def test_the_script_explains_how_to_get_appimagetool(script):
    assert "APPIMAGETOOL" in script
    assert "appimagetool" in script.lower()


def test_extraction_is_used_where_there_is_no_fuse(script):
    assert "APPIMAGE_EXTRACT_AND_RUN" in script


def test_the_architecture_is_set_explicitly(script):
    """appimagetool cannot infer ARCH from an AppDir with no ELF at its root."""
    assert "ARCH=" in script


# -- build.py integration ---------------------------------------------------

def test_build_py_exposes_an_appimage_mode():
    assert hasattr(build, "build_appimage")


def test_appimage_only_is_refused_off_linux(monkeypatch, capsys):
    monkeypatch.setattr(build.sys, "platform", "darwin")
    assert build.main(["--appimage-only"]) == 1
    assert "Linux-only" in capsys.readouterr().err


def test_building_an_appimage_without_a_build_fails_loudly(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build, "DIST", tmp_path / "dist")
    assert build.build_appimage("1.2.3") == 1
    assert "run the build first" in capsys.readouterr().err


def test_an_appimage_is_a_release_artifact(tmp_path, monkeypatch):
    """Otherwise it is built, never published, and never hashed."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "Ticker-1.2.3-x86_64.AppImage").write_bytes(b"stub")
    monkeypatch.setattr(build, "DIST", dist)
    monkeypatch.setattr(build.sys, "platform", "linux")
    assert any(p.name.endswith(".AppImage") for p in build.artifacts())


def test_the_appimage_is_hashed_with_everything_else(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "Ticker-1.2.3-x86_64.AppImage").write_bytes(b"stub")
    monkeypatch.setattr(build, "DIST", dist)
    monkeypatch.setattr(build.sys, "platform", "linux")
    build.write_hashes(required=True)
    assert "AppImage" in (dist / "SHA256SUMS").read_text()


def test_the_appimage_is_named_after_the_version(script):
    assert "Ticker-${VERSION}-x86_64.AppImage" in script


# -- The release workflow ---------------------------------------------------

def test_the_workflow_installs_appimagetool_before_building(workflow):
    install = workflow.index("Install appimagetool")
    built = workflow.index("Build the AppImage")
    assert install < built


def test_the_workflow_builds_through_build_py(workflow):
    """Same tested code path locally and in CI, as with the dmg."""
    assert "--appimage-only" in workflow


def test_the_workflow_builds_the_appimage_before_hashing(workflow):
    """Hashes taken first would not cover it (section 12.3)."""
    built = workflow.index("Build the AppImage")
    hashed = workflow.index("Recompute SHA256SUMS")
    assert built < hashed


def test_the_appimage_is_attached_to_the_release(workflow):
    assert "dist/Ticker-*.AppImage" in workflow


def test_the_workflow_only_runs_the_linux_steps_on_linux(workflow):
    for step in ("Install appimagetool", "Build the AppImage"):
        after = workflow[workflow.index(step):workflow.index(step) + 400]
        assert "runner.os == 'Linux'" in after, step


def test_the_packaging_notes_document_building_the_appimage():
    """Not merely that the word appears.

    The notes previously said an AppImage "would be the next step", which
    contains the word and describes something that is no longer true.
    """
    notes = (ROOT / "packaging" / "README.md").read_text(encoding="utf-8")
    assert "build-appimage.sh" in notes
    assert "--appimage-only" in notes
    assert "would be the next step" not in notes


def test_shell_scripts_are_pinned_to_lf_endings():
    """CRLF makes the shebang unusable: 'bad interpreter: ...bash^M'."""
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"\*\.sh\s+text\s+eol=lf", attributes)


def test_the_committed_script_has_lf_endings():
    """What is stored is what Linux CI checks out."""
    blob = subprocess.run(
        ["git", "show", ":packaging/linux/build-appimage.sh"],
        cwd=ROOT, capture_output=True).stdout
    assert CRLF not in blob

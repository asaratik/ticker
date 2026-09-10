"""
Tests for the macOS signing and notarization pipeline.

Nothing here runs codesign or talks to Apple -- that needs a Mac, a
Developer Program membership and several minutes per submission. What is
tested is the part that is easy to get quietly wrong and expensive to
discover on a release day: the entitlements the hardened runtime needs, and
the order the workflow does things in.

Order is the whole game on macOS. Every one of these is a mistake that
produces a green build and an app the user cannot open:

* notarizing before signing            -> rejected
* stapling before notarizing           -> nothing to staple
* zipping the app before stapling      -> a .zip whose app has no ticket
* building the .dmg before stapling    -> an image containing an unstapled app
* hashing before stapling              -> published checksums that do not match
"""

import plistlib
import re
from pathlib import Path

import pytest

import build

ROOT = Path(__file__).resolve().parent.parent
MACOS = ROOT / "packaging" / "macos"
ENTITLEMENTS = MACOS / "entitlements.plist"
NOTARIZE = MACOS / "notarize.sh"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


@pytest.fixture(scope="module")
def workflow():
    return RELEASE.read_text(encoding="utf-8")


def step_body(text, name):
    """Just the one step, up to where the next one begins. Windowing by a
    fixed number of characters reads into the following step and makes an
    assertion about the wrong one."""
    start = text.index(name)
    following = text.find("\n      - name:", start)
    return text[start:following if following != -1 else len(text)]


def order(text, *needles):
    """The positions of each needle, asserting every one is present."""
    found = []
    for needle in needles:
        index = text.find(needle)
        assert index != -1, "{!r} is not in the workflow".format(needle)
        found.append(index)
    return found


# -- entitlements ---------------------------------------------------------

def test_the_entitlements_file_exists():
    assert ENTITLEMENTS.exists()


def test_the_entitlements_are_a_valid_plist():
    # codesign rejects a malformed one with a message that does not say the
    # file is malformed, so this is worth catching here.
    assert isinstance(plistlib.loads(ENTITLEMENTS.read_bytes()), dict)


def test_bluetooth_is_entitled():
    """Info.plist's usage string is necessary but not sufficient under the
    hardened runtime: without this the app finds no devices at all."""
    keys = plistlib.loads(ENTITLEMENTS.read_bytes())
    assert keys["com.apple.security.device.bluetooth"] is True


def test_library_validation_is_disabled_for_the_onedir_bundle():
    # A onedir bundle loads dozens of .dylib files from wheels that are not
    # signed by this team; with validation on, the app dies on its first
    # extension-module import.
    keys = plistlib.loads(ENTITLEMENTS.read_bytes())
    assert keys["com.apple.security.cs.disable-library-validation"] is True


def test_ctypes_can_still_build_its_thunks():
    keys = plistlib.loads(ENTITLEMENTS.read_bytes())
    assert keys["com.apple.security.cs.allow-jit"] is True
    assert keys["com.apple.security.cs.allow-unsigned-executable-memory"] is True


def test_the_debugger_entitlement_is_not_requested():
    # get-task-allow lets a debugger attach, and the notary service rejects
    # any submission carrying it.
    keys = plistlib.loads(ENTITLEMENTS.read_bytes())
    assert "com.apple.security.get-task-allow" not in keys


# -- the notarize script --------------------------------------------------

def test_the_notarize_script_exists():
    assert NOTARIZE.exists()


def test_notarization_waits_for_the_verdict():
    # Without --wait the submission returns immediately and the staple that
    # follows has no ticket to attach.
    assert "--wait" in NOTARIZE.read_text(encoding="utf-8")


def test_notarization_retries(workflow):
    """Section 12.7: tolerate a retry rather than failing the whole run."""
    text = NOTARIZE.read_text(encoding="utf-8")
    assert "NOTARIZE_ATTEMPTS" in text
    assert "seq 1" in text


def test_a_rejection_is_not_retried():
    # Apple looked at it and said no; asking again gets the same answer more
    # slowly. The log is fetched instead, because "Invalid" names nothing.
    text = NOTARIZE.read_text(encoding="utf-8")
    assert "retrying will not help" in text
    assert "notarytool log" in text


def test_the_script_checks_its_credentials_up_front():
    text = NOTARIZE.read_text(encoding="utf-8")
    for name in ("APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID"):
        assert name in text


def test_the_script_is_executable_in_git():
    # Checked out without the bit set, the workflow's ./notarize.sh is a
    # permission denied halfway through a release.
    import subprocess
    listed = subprocess.run(
        ["git", "ls-files", "-s", "packaging/macos/notarize.sh"],
        cwd=str(ROOT), capture_output=True, text=True).stdout
    if not listed.strip():
        pytest.skip("not tracked yet")
    assert listed.split()[0] == "100755", listed


# -- workflow order -------------------------------------------------------

def test_the_app_is_signed_before_it_is_notarized(workflow):
    signed, notarized = order(workflow, "Sign the app bundle",
                              "Notarize and staple the app")
    assert signed < notarized


def test_the_certificate_is_imported_before_anything_is_signed(workflow):
    imported, signed = order(workflow, "Import the Developer ID certificate",
                             "Sign the app bundle")
    assert imported < signed


def test_the_hardened_runtime_is_enabled(workflow):
    # Notarization requires it; without --options runtime the submission is
    # rejected regardless of the entitlements.
    assert "--options runtime" in workflow


def test_the_entitlements_are_applied_to_the_bundle(workflow):
    assert "packaging/macos/entitlements.plist" in workflow


def test_nested_binaries_are_signed_before_the_bundle(workflow):
    # Same rule as the Windows job: an unsigned dylib inside a signed
    # bundle fails validation.
    dylibs = workflow.index('-name "*.dylib"')
    bundle = workflow.index('--sign "$IDENTITY" dist/Ticker.app')
    assert dylibs < bundle


def test_codesign_is_never_invoked_with_deep(workflow):
    """--deep is deprecated and applies the bundle's entitlements to every
    nested binary, which is not what any of them should carry."""
    for line in workflow.splitlines():
        if "codesign" in line and not line.lstrip().startswith("#"):
            assert "--deep" not in line, line


def test_the_zip_is_made_after_the_app_is_stapled(workflow):
    # A .zip cannot carry a signature of its own, so the app inside has to
    # hold its ticket or the download is blocked on a machine that is
    # offline.
    stapled, zipped = order(workflow, "xcrun stapler staple dist/Ticker.app",
                            "Zip the macOS app bundle")
    assert stapled < zipped


def test_the_disk_image_is_built_after_the_app_is_stapled(workflow):
    # hdiutil copies the bundle as it finds it: an image built from an
    # unstapled app contains an unstapled app forever after.
    stapled, dmg = order(workflow, "xcrun stapler staple dist/Ticker.app",
                         "Build the disk image")
    assert stapled < dmg


def test_the_disk_image_is_signed_notarized_and_stapled(workflow):
    # Section 12.5: "Ship a signed .dmg, notarized as a whole." Stapling the
    # app inside does not staple the image, and Gatekeeper checks the image
    # on open.
    step = workflow.index("Sign, notarize and staple the disk image")
    tail = workflow[step:]
    codesign = tail.index("codesign --force")
    notarize = tail.index("notarize.sh")
    staple = tail.index("stapler staple")
    assert codesign < notarize < staple


def test_the_hashes_are_taken_after_everything_that_changes_the_bytes(workflow):
    # Signing and stapling both rewrite the file. Checksums taken earlier
    # are published against downloads that will not match them.
    dmg, hashes = order(workflow, "Sign, notarize and staple the disk image",
                        "Recompute SHA256SUMS")
    assert dmg < hashes


def test_the_disk_image_is_attached_to_the_release(workflow):
    assert "dist/Ticker-*.dmg" in workflow


def test_signing_is_skipped_rather_than_failing_without_a_certificate(workflow):
    # A fork has no Developer Program membership; it should still be able to
    # cut an unsigned release rather than fail on a secret it never had.
    assert "MACOS_SIGNING_ENABLED" in workflow
    for step in ("Sign the app bundle", "Notarize and staple the app"):
        assert "MACOS_SIGNING_ENABLED == 'true'" in step_body(workflow, step), step


def test_the_unsigned_path_still_produces_both_artifacts(workflow):
    # The zip and the dmg are built unconditionally; only the signing and
    # notarization steps are gated.
    for step in ("Zip the macOS app bundle", "Build the disk image"):
        body = step_body(workflow, step)
        assert "MACOS_SIGNING_ENABLED" not in body, step
        assert "runner.os == 'macOS'" in body, step


# -- build.py's part ------------------------------------------------------

def test_the_disk_image_is_built_by_build_py(workflow):
    """One packaging command, used the same way locally and in CI -- the
    same reason the Windows installer is built through build.py."""
    assert "build.py --dmg-only" in workflow


def test_a_dmg_is_a_release_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "DIST", tmp_path)
    monkeypatch.setattr(build.sys, "platform", "darwin")
    (tmp_path / "Ticker.zip").write_bytes(b"zip")
    (tmp_path / "Ticker-1.2.3.dmg").write_bytes(b"dmg")
    names = {p.name for p in build.artifacts()}
    assert names == {"Ticker.zip", "Ticker-1.2.3.dmg"}


def test_the_dmg_is_hashed_with_everything_else(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "DIST", tmp_path)
    monkeypatch.setattr(build.sys, "platform", "darwin")
    (tmp_path / "Ticker-1.2.3.dmg").write_bytes(b"dmg")
    assert build.write_hashes(required=True) == 0
    assert "Ticker-1.2.3.dmg" in (tmp_path / "SHA256SUMS").read_text()


def test_building_a_dmg_without_an_app_fails_loudly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build, "DIST", tmp_path)
    assert build.build_dmg("1.2.3") == 1
    assert "run the build first" in capsys.readouterr().err


def test_the_dmg_is_named_after_the_version(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "DIST", tmp_path)
    (tmp_path / "Ticker.app").mkdir()
    issued = []
    monkeypatch.setattr(build, "run", lambda cmd: issued.append(cmd))
    assert build.build_dmg("1.2.3") == 0
    command = [str(c) for c in issued[0]]
    assert command[0] == "hdiutil"
    assert command[-1].endswith("Ticker-1.2.3.dmg")
    # -ov, so a re-run in a dirty dist/ replaces the image rather than
    # failing on it.
    assert "-ov" in command


def test_dmg_only_is_refused_off_macos(monkeypatch, capsys):
    monkeypatch.setattr(build.sys, "platform", "win32")
    assert build.main(["--dmg-only", "--version", "1.2.3"]) == 1
    assert "macOS-only" in capsys.readouterr().err


# -- the Info.plist half, which is not entitlements -----------------------

def test_the_bundle_still_declares_the_bluetooth_usage_string():
    # Independent of signing: without it macOS refuses Bluetooth outright.
    # The entitlement does not replace it; both are required.
    text = (ROOT / "packaging" / "ticker.spec").read_text(encoding="utf-8")
    assert "NSBluetoothAlwaysUsageDescription" in text


def test_the_packaging_notes_no_longer_call_notarization_outstanding():
    # README drift is how "not set up yet" survives three releases past the
    # point it stopped being true.
    text = (ROOT / "packaging" / "README.md").read_text(encoding="utf-8")
    assert not re.search(r"[Ss]till outstanding \(milestone 9\)", text)

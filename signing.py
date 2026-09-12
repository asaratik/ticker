"""Optional release signing. Credentials belong in CI secrets, never files in git."""
from contextlib import contextmanager
import base64
import glob
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile


def required(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Signing requires {name}; see docs/RELEASING.md")
    return value


def run(command):
    # Do not print command arguments: some contain credential material.
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Signing step {Path(command[0]).name} failed (exit {result.returncode}). Check the certificate and signing credentials.")


@contextmanager
def credentials():
    if sys.platform == "darwin":
        # A temporary keychain is added alongside the existing search list.
        original = subprocess.check_output(["security", "list-keychains", "-d", "user"], text=True)
        import shlex
        previous = shlex.split(original)
        with tempfile.TemporaryDirectory() as folder:
            cert = Path(folder) / "certificate.p12"
            cert.write_bytes(base64.b64decode(required("MACOS_CERTIFICATE_P12"), validate=True))
            cert.chmod(0o600)
            keychain = str(Path(folder) / "signing.keychain-db")
            password = secrets.token_urlsafe(32)
            os.environ["TICKER_CODESIGN_IDENTITY"] = required("MACOS_SIGNING_IDENTITY")
            run(["security", "create-keychain", "-p", password, keychain])
            try:
                run(["security", "set-keychain-settings", "-lut", "3600", keychain])
                run(["security", "unlock-keychain", "-p", password, keychain])
                run(["security", "import", str(cert), "-k", keychain, "-P", required("MACOS_CERTIFICATE_PASSWORD"), "-T", "/usr/bin/codesign"])
                run(["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", password, keychain])
                run(["security", "list-keychains", "-d", "user", "-s", keychain, *previous])
                yield
            finally:
                run(["security", "list-keychains", "-d", "user", "-s", *previous])
                run(["security", "delete-keychain", keychain])
                os.environ.pop("TICKER_CODESIGN_IDENTITY", None)
    else:
        yield


def sign(executable):
    if sys.platform == "darwin":
        run(["codesign", "--verify", "--deep", "--strict", "dist/Ticker.app"])
        run(["ditto", "-c", "-k", "--keepParent", "dist/Ticker.app", "dist/notarize.zip"])
        run(["xcrun", "notarytool", "submit", "dist/notarize.zip", "--wait",
             "--apple-id", required("APPLE_ID"), "--password", required("APPLE_APP_PASSWORD"),
             "--team-id", required("APPLE_TEAM_ID")])
        run(["xcrun", "stapler", "staple", "dist/Ticker.app"])
        run(["spctl", "--assess", "--type", "execute", "dist/Ticker.app"])
    elif sys.platform == "win32":
        tool = shutil.which("signtool")
        if not tool:
            matches = glob.glob(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)") + "/Windows Kits/10/bin/*/x64/signtool.exe")
            tool = sorted(matches)[-1] if matches else None
        if not tool:
            raise RuntimeError("Install the Windows SDK signing tools first")
        with tempfile.TemporaryDirectory() as folder:
            cert = Path(folder) / "certificate.pfx"
            cert.write_bytes(base64.b64decode(required("WINDOWS_CERTIFICATE_PFX"), validate=True))
            run([tool, "sign", "/fd", "SHA256", "/tr", "http://timestamp.digicert.com", "/td", "SHA256",
                 "/f", str(cert), "/p", required("WINDOWS_CERTIFICATE_PASSWORD"), str(executable)])
        run([tool, "verify", "/pa", str(executable)])

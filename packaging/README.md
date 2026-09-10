# Packaging

```
python build.py                 # test + build dist/Ticker (a folder)
python build.py --skip-tests    # straight to packaging
python build.py --installer     # Windows: also build the Inno Setup installer
python build.py --hashes-only   # rewrite dist/SHA256SUMS
python build.py --winget-only --version 1.2.3   # fill in dist/winget/
```

`packaging/ticker.spec` is the single source of truth for what goes into a
build, and `build.py` builds *from* it. Don't pass PyInstaller options on the
command line: doing so makes it generate a `.spec` of its own and overwrite
this one. That is not hypothetical — the database migrations were added to a
generated spec once, silently vanished on the next build, and packaged builds
shipped unable to create their schema.

`tests/test_build.py` checks that every `.sql` file on disk is covered by the
spec's `datas`, so adding a migration without updating the spec fails the
suite rather than the user's first run.

## Why a folder instead of one file

PyInstaller's `--onefile` bootloader unpacks an embedded archive into a temp
directory and executes from there, which is behaviourally identical to a
dropper. Heuristic AV engines flag it on that basis alone, and signing does
not fully fix it — plenty of signed onefile binaries still get flagged. UPX
compression is off for the same reason.

The folder is wrapped in an installer, so users never see the difference.

## What is *not* set up here

**Nothing in this repo is signed, and none of the signing configuration has
ever been executed.** The workflow steps are written against Azure Artifact
Signing's documented interface, but they have never run — there is no Azure
account, no certificate profile, and no way to test them short of setting
those up. Treat the release workflow's signing steps as a starting point to
verify, not as working configuration.

The workflow skips signing entirely when `AZURE_CLIENT_ID` is unset, so
releases still build (unsigned) until that changes.

### To actually sign on Windows

1. Set up Azure Artifact Signing (formerly Trusted Signing). As of 2026 it is
   roughly $9.99/month on the Basic tier, and self-employed individuals can
   apply — the three years of business history is no longer required.
   Individual developers are currently limited to the USA and Canada.
2. Create an app registration with a **federated credential** for this
   repository, so CI authenticates by OIDC and there is no long-lived secret
   to store or leak.
3. Add repository secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
   `AZURE_SUBSCRIPTION_ID`, `AZURE_SIGNING_ENDPOINT`,
   `AZURE_SIGNING_ACCOUNT`, `AZURE_CERT_PROFILE`.
4. Tag a release and watch the run.

Set expectations honestly: **this does not buy instant SmartScreen trust.**
New files can still show the warning until they accumulate reputation.
Signing consecutive releases under a consistent publisher identity is what
lets reputation build and carry forward. The same is true of an OV
certificate; there is no purchasable shortcut to a clean first download.

### Reducing false positives beyond signing

- Every binary in the bundle is signed, not just `Ticker.exe` — an unsigned
  DLL beside a signed executable is exactly what a scanner looks for.
- SHA256SUMS is published with every release so anyone can verify a download
  independently of whether it happens to be signed.
- Not yet done: building the PyInstaller bootloader from source. The prebuilt
  one is in many AV signature databases precisely because malware ships the
  stock build, and a locally compiled bootloader has a different hash. Worth
  doing if false positives persist after signing.
- Submit false positives to Microsoft Defender's developer portal (and the
  equivalents) per release until reputation settles. Turnaround is usually
  days.

## winget

`packaging/windows/winget/` holds the three manifest files. They validate
against the real schema:

```
winget validate --manifest packaging/windows/winget
```

`InstallerSha256` is a deliberately obvious placeholder — sixty-four zeroes.
A wrong hash is the most common reason a winget PR fails validation, so it is
never hand-edited: the tracked files stay templates, and the release workflow
writes filled-in copies to `dist/winget/` with the version, the release URL,
the date, and the hash of the **signed** installer. That step runs after
signing on purpose — signing rewrites the installer's bytes, so a hash taken
before it would be wrong.

The filled copies are attached to the GitHub release, so publishing is:
download the three `.yaml` files from the release, drop them under
`manifests/a/AshokAratikatla/Ticker/<version>/` in a fork of
`microsoft/winget-pkgs`, and open a PR. Nothing to edit by hand.

Confirm the publisher identity before the first submission — the manifests
assume `AshokAratikatla.Ticker`, and winget requires it to be consistent
forever after.

## macOS

`packaging/ticker.spec` sets `NSBluetoothAlwaysUsageDescription` in the
bundle's `Info.plist`. Without it macOS refuses Bluetooth access outright,
signed or not, so this is a real fix independent of notarization.

`packaging/macos/entitlements.plist` is the other half. Under the hardened
runtime — which notarization requires — the usage string is necessary but
not sufficient: `com.apple.security.device.bluetooth` is what actually lets
CoreBluetooth answer. The other three keys are there because a
PyInstaller onedir bundle does not run without them (library validation
would reject wheel-shipped dylibs; ctypes builds executable thunks). The
file explains each one.

### The release pipeline

The macOS half of `release.yml` does this, in this order:

```
sign the app  →  notarize it  →  staple the ticket to it
              →  build the .dmg from the stapled app
              →  sign the .dmg  →  notarize it  →  staple it
```

Two notarization submissions, because two things get downloaded. Stapling
the `.dmg` does not staple the app inside it, and a `.zip` is not a
container that can carry a signature at all — so the app has to hold its
own ticket and the disk image has to hold one too, or Gatekeeper blocks it
on open. Every step in that chain rewrites bytes, which is why `SHA256SUMS`
is recomputed at the very end. `tests/test_macos_packaging.py` asserts the
order, because each way of getting it wrong produces a green build and an
app that will not open.

`packaging/macos/notarize.sh` wraps `notarytool submit --wait`. It retries a
submission that fails to complete — the notary service has bad minutes — but
never retries a verdict
of `Invalid`: Apple looked at the artifact and said no, and asking again
gets the same answer more slowly. That case fetches the submission log,
because "Invalid" on its own names nothing and the log names the binary.

Budget a few minutes of wall clock per submission.

### To actually notarize

1. Apple Developer Program membership, $99/year. Not optional for a
   distributable `.app`.
2. Create a **Developer ID Application** certificate, export it as a `.p12`,
   and base64 it: `base64 -i cert.p12 | pbcopy`.
3. Create an **app-specific password** at appleid.apple.com — not the
   account password, which `notarytool` will not accept.
4. Add repository secrets:

   | Secret | What it is |
   |---|---|
   | `MACOS_CERTIFICATE` | the base64 `.p12` |
   | `MACOS_CERTIFICATE_PASSWORD` | its export password |
   | `MACOS_KEYCHAIN_PASSWORD` | any string; unlocks the throwaway CI keychain |
   | `MACOS_SIGNING_IDENTITY` | `Developer ID Application: Name (TEAMID)` |
   | `MACOS_APPLE_ID` | the Apple ID the app-specific password belongs to |
   | `MACOS_APPLE_APP_PASSWORD` | the app-specific password |
   | `MACOS_TEAM_ID` | the ten-character team identifier |

5. Tag a release and watch the run.

As on Windows, the whole macOS signing path is **written but never
executed** — there is no Developer Program membership behind this repo. With
`MACOS_CERTIFICATE` unset every signing and notarization step is skipped and
the job still publishes an unsigned `Ticker.zip` and `.dmg`, which is what a
fork gets. Treat the steps as configuration to verify, not as known-good.

Unlike SmartScreen, notarization has no reputation curve: a notarized app
opens cleanly the first time, and an unnotarized one is blocked outright on
current macOS rather than merely warned about.

## Linux

Signing isn't the trust mechanism on Linux. The release publishes
both a tarball of the onedir folder and an AppImage:

```
python build.py                                  # produce dist/Ticker
packaging/linux/build-appimage.sh 1.2.3          # wrap it
python build.py --appimage-only --version 1.2.3  # same thing, via build.py
```

AppImage because it needs nothing installed on the target -- one executable
file, no package manager, no runtime -- which matches how the other two
platforms ship.

`appimagetool` is not downloaded by the script. Put it on `PATH` or set
`$APPIMAGETOOL`; CI installs it as its own step so that fetching an
executable off the internet is visible in the build log rather than buried
in a script. On a machine without FUSE (GitHub runners included) set
`APPIMAGE_EXTRACT_AND_RUN=1`, which the script already defaults to.

The whole onedir folder goes into the AppDir, not just the executable:
`Ticker` cannot start without `_internal` beside it, and an AppImage built
from the executable alone still builds and still fails on first launch.

`packaging/linux/ticker.png` is generated by `make_icon.py` rather than
committed as an opaque binary -- run it to regenerate and diff. `pipx`
still covers most of the realistic audience.

## pipx

```
pipx install ticker          # from PyPI, once published
pipx install .               # from a checkout
```

Entry points: `ticker` (the app), `ticker-sync`, `ticker-import`, `ticker-setup`,
`ticker-rollup`, `ticker-backfill`, `ticker-server`, `ticker-agent`. This
sidesteps code signing entirely, which makes it the path of least resistance
for anyone technical.

**The packaged builds ship the app and nothing else.** PyInstaller follows
imports from `ticker/ui/app.py`, and the two halves of the agent/server split are
separate entry points that nothing in the GUI imports — so the installer,
the `.dmg` and the tarball all contain `ticker` alone. That is the right
default: the split is opt-in, and the single-machine deployment the packaged
build serves needs neither half.

Anyone running agent and server on separate machines installs from source or
`pipx` today. Shipping them in the bundle would mean additional `EXE`
targets sharing one `COLLECT`, which is worth doing when someone actually
wants a signed agent on a machine that cannot install Python, and not
before.

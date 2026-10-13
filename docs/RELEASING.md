# Releasing and servicing Ticker

## Dependency updates

`requirements-build.lock` contains every runtime and build dependency with
platform markers and hashes. Regenerate it only in a reviewed dependency
change:

```console
uv pip compile requirements-dev.txt --universal --python-version 3.13 --generate-hashes --output-file requirements-build.lock
python build.py
```

`build.py` installs that lock with `--require-hashes`, binary wheels only, and
without an upgrade step. CI and release builds use Python 3.13.12. GitHub
Actions are pinned to immutable commit SHAs. The AppImage tool is pinned to
version 1.9.1 and verified with its SHA-256 digest before execution.

## Publication

1. Update the package version and `docs/release-notes.md`, merge, and create a
   protected tag such as `v0.3.0`.
2. Push the tag, or dispatch **Release** with that existing tag.
3. The resolve job validates the tag once. All platform jobs check out the
   resolved commit and run unit tests plus a launch of the packaged app against
   a temporary database.
4. Windows signs the already-built bundle, builds the installer from those
   exact signed bytes, then signs the installer. macOS signs and notarizes the
   app before creating its zip and disk image. Linux verifies appimagetool
   before creating its AppImage.
5. Each platform stages two uniquely named downloads, a checksum file, and a
   manifest with version, commit, byte sizes, hashes, and signing state. Windows
   also stages the three winget manifests.
6. The publish job downloads all platform sets and verifies completeness,
   identity, hashes, and sizes locally. Only then does it create or reuse a
   draft. It uploads every file, verifies the server-side asset names and sizes,
   and finally publishes the draft. Any failure leaves a draft unpublished.

Only the publish job receives `contents: write`; build jobs have read-only
repository access. Per-tag concurrency prevents simultaneous publication.
Published releases cannot be overwritten by the workflow.

## Signing and notarization

Unsigned builds are marked `signed: false`. Set repository variable
`SIGN_RELEASES=true` to reject a Windows or macOS release when its signing
credentials are unavailable.

| Platform | Required secrets |
|---|---|
| Windows Azure Trusted Signing | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_SIGNING_ENDPOINT`, `AZURE_SIGNING_ACCOUNT`, `AZURE_CERT_PROFILE` |
| macOS | `MACOS_CERTIFICATE` (base64 P12), `MACOS_CERTIFICATE_PASSWORD`, `MACOS_KEYCHAIN_PASSWORD`, `MACOS_SIGNING_IDENTITY`, `MACOS_APPLE_ID`, `MACOS_APPLE_APP_PASSWORD`, `MACOS_TEAM_ID` |

The macOS job uses a temporary keychain. The Windows job uses OIDC, so no
long-lived Azure password is stored. Linux release integrity is represented by
the published checksums and manifest.

## Support and recovery

Ask for the page's diagnostics JSON first. It contains the application build,
operating system, database size/schema/integrity, and server address; it omits
recordings, device identifiers, credential references, and filesystem paths.
Ticker keeps three 2 MiB rotating logs. Logs can contain private import paths,
so users should inspect them before sharing.

Create a snapshot before risky servicing with `ticker backup create`. The
SQLite backup API includes committed WAL data and verifies the result. Restore
with Ticker stopped using `ticker backup restore FILE`; the command verifies the
input and preserves the current database before replacement. Never copy the
main SQLite file alone while Ticker is running.

The page checks for updates only when the user presses **Check for updates**.
It links to the latest GitHub release and does not replace binaries in the
background. Ship corrections under a new tag. An older application rejects a
newer database schema; restoring a compatible backup is required for rollback.

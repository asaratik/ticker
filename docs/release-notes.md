Ticker now distinguishes recording from saving, reports storage failures,
and confirms how many samples reached the database. Reconnects stay with
the selected monitor; stale readings disappear and graphs preserve gaps.

New controls include a remembered device picker, session history, CSV export,
session deletion, retention settings, and privacy-conscious diagnostics.
Existing unversioned databases receive a backup before schema migration.

Downloads are named by version, OS and architecture. Each includes a checksum
and build manifest; inspect the manifest's `signed` field for signing status.
macOS downloads target Apple Silicon. Linux downloads target x86_64 and are
built on Ubuntu 22.04. Data stays in your existing per-user Ticker folder.

"""
v2 configuration.

The v1 modules still import the top-level `config` module, and both versions
have to agree on which file the database is, so this deliberately re-exports
DB_PATH from there rather than re-deriving it. When the v1 modules move into
this package the two merge into this file and the import
below goes away.

Absolute imports mean `import config` here picks up the top-level module,
not this one.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Optional

import config as _v1_config

DB_PATH = _v1_config.DB_PATH

# Display names for the sources the app itself configures. The v1 migration
# creates the 'ble' row and the app looks it up by this name, so they have to
# agree: a mismatch would leave the same strap's history under one source row
# and its new data under another.
SOURCE_DISPLAY_NAMES = {
    "ble": "BLE strap",
    "http": "Watch (HTTP push)",
}


def source_display_name(vendor: str) -> str:
    return SOURCE_DISPLAY_NAMES.get(vendor, vendor)


def _local_zone_name() -> str:
    """The OS zone's IANA name, for defaulting a vendor's profile zone.

    Only a real IANA key is ever returned. `tzname()` looks like an answer
    and is not one -- on Windows it gives "Pacific Daylight Time", which no
    tz database can resolve -- and a zone name that fails to resolve is
    worse here than an obviously-neutral one: it would make the Fitbit
    connector raise on every fetch rather than merely be in the wrong place.

    UTC is the fallback precisely because it is visibly wrong for most
    people, which is the behaviour wanted from a setting that has to be
    configured deliberately.
    """
    try:
        zone = datetime.now(timezone.utc).astimezone().tzinfo
        key = getattr(zone, "key", None)
        if key:
            return key
        # tzlocal, when installed, knows the mapping this cannot do itself.
        try:
            import tzlocal
            return str(tzlocal.get_localzone())
        except Exception:
            return "UTC"
    except Exception:
        return "UTC"


# IANA zone used for local-time bucketing at rollup time, and nowhere else --
# everything is stored in UTC. Unset means the OS zone.
TICKER_TZ = os.environ.get("TICKER_TZ") or None

# Live-stream write coalescing: commit after this many rows
# or this long, whichever comes first. A crash costs at most COALESCE_MS of
# live data, which is an acceptable trade for not fsyncing at 1 Hz.
COALESCE_ROWS = int(os.environ.get("TICKER_COALESCE_ROWS", "200"))
COALESCE_MS = float(os.environ.get("TICKER_COALESCE_MS", "2000"))

# Normalizer batch size handed to the writer.
NORMALIZE_BATCH = int(os.environ.get("TICKER_NORMALIZE_BATCH", "1000"))

# -- Fitbit --------------------------------------------------------------

# Fitbit is a public OAuth client: the client id identifies the application
# and is not a secret, so it lives here rather than in the keyring. The
# tokens it obtains do go to the keyring, like every other credential.
# Register an application at dev.fitbit.com to get one.
FITBIT_CLIENT_ID = os.environ.get("TICKER_FITBIT_CLIENT_ID") or ""

# The IANA zone the Fitbit account reports in. Fitbit answers with local
# wall-clock times carrying no offset, so this is what makes them
# interpretable at all -- see ticker/sources/fitbit.py. Defaults to the same
# zone rollups bucket in, which is right whenever the account and this
# machine are set to the same place.
FITBIT_PROFILE_TZ = (os.environ.get("TICKER_FITBIT_TZ")
                     or TICKER_TZ or _local_zone_name())


# raw_payloads retention in days. 0 disables the sweep and keeps everything.
RAW_RETENTION_DAYS = int(os.environ.get("TICKER_RAW_RETENTION_DAYS", "90"))

# How long a writer waits out another connection's lock before giving up.
BUSY_TIMEOUT_MS = int(os.environ.get("TICKER_BUSY_TIMEOUT_MS", "5000"))


def local_zone() -> tzinfo:
    """The zone rollup days are bucketed in.

    Falls back to the OS zone if TICKER_TZ is unset, and to UTC if the name
    is one the platform's tz database doesn't have -- a bad zone name should
    mis-bucket a dashboard, not stop ingest.
    """
    if TICKER_TZ:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(TICKER_TZ)
        except Exception:
            pass
    return datetime.now(timezone.utc).astimezone().tzinfo or timezone.utc


def local_day(dt: datetime, zone: Optional[tzinfo] = None) -> str:
    """'YYYY-MM-DD' for the rollups_daily key, in local time."""
    return dt.astimezone(zone or local_zone()).strftime("%Y-%m-%d")


# -- read API ------------------------------------------------------------

# Loopback by default. The API can write to the database (POST /api/ingest),
# so unlike the watch's HR endpoint it does not get to listen on every
# interface just because that is convenient -- remote access requires an
# explicit opt-in *and* a token, which api.server enforces together.
API_HOST = os.environ.get("TICKER_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("TICKER_API_PORT", "8477"))

# Shared secret. Required to bind anywhere but loopback; optional on
# loopback, where the OS is already the access control.
API_TOKEN = os.environ.get("TICKER_API_TOKEN") or None

# Binding off-loopback without this explicit flag is
# refused rather than quietly allowed, so the safe configuration is never
# one env var's typo away from being the open one.
API_ALLOW_REMOTE = (os.environ.get("TICKER_API_ALLOW_REMOTE", "")
                    .strip().lower() in {"1", "true", "yes", "on"})

# Extra names a token-less loopback server answers to. Without a token the
# Host header has to name loopback -- that is the DNS-rebinding defence --
# which also turns away a container reaching this machine by name, such as
# Open WebUI in Docker asking for host.docker.internal. List such names
# here, comma-separated.
API_ALLOWED_HOSTS = frozenset(
    name.strip().lower()
    for name in os.environ.get("TICKER_API_ALLOWED_HOSTS", "").split(",")
    if name.strip())

# Most observations one /api/ingest call may carry. An agent draining a long
# spool sends many batches rather than one enormous body.
API_MAX_BATCH = int(os.environ.get("TICKER_API_MAX_BATCH", "5000"))


# -- the Ask box ------------------------------------------------------------

# The model server and model are chosen on the page (see ticker.app.settings)
# unless TICKER_LLM_URL / TICKER_LLM_MODEL pin them. These tune the loop.

# Context window asked of Ollama per request. Its own default is too small
# for tool use; bigger costs memory on the machine running the model.
LLM_CONTEXT = int(os.environ.get("TICKER_LLM_CONTEXT", "8192"))

# How long one model reply may take. Generous: a 14B model on a CPU is slow.
LLM_TIMEOUT_SEC = float(os.environ.get("TICKER_LLM_TIMEOUT_SEC", "300"))


# -- agent ---------------------------------------------------------------

# Where the agent posts. Loopback means agent and server on one machine,
# which is the default deployment and needs no configuration at all.
AGENT_SERVER_URL = os.environ.get(
    "TICKER_SERVER_URL", "http://{}:{}".format(
        "127.0.0.1" if API_HOST in {"0.0.0.0", "::"} else API_HOST, API_PORT))

# The agent's local spool. Its own file, never the main database: the whole
# point of the split is that the machine near the strap doesn't need one.
AGENT_SPOOL_PATH = Path(os.environ.get("TICKER_SPOOL_PATH") or
                        (DB_PATH.parent / "spool.sqlite3"))

# Observations per uplink POST, and how long a post may take. The timeout is
# short on purpose: a stalled uplink should spool and move on, not block the
# stream behind a socket that will time out in thirty seconds anyway.
AGENT_BATCH = int(os.environ.get("TICKER_AGENT_BATCH", "500"))
AGENT_TIMEOUT_SEC = float(os.environ.get("TICKER_AGENT_TIMEOUT_SEC", "10"))

# Spool retention. A machine that cannot reach its server for a month should
# stop growing rather than fill the disk; the oldest rows go first.
AGENT_SPOOL_MAX_ROWS = int(os.environ.get("TICKER_SPOOL_MAX_ROWS", "2000000"))

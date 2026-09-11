"""
Secret storage, via the OS keyring.

Tokens live in the OS keyring --
DPAPI on Windows, Keychain on macOS, Secret Service on Linux -- and
`sources.auth_ref` holds only the *name* of the keyring entry, never the
secret. No secrets in the database, no secrets in environment variables, no
secrets in logs.

The environment-variable exclusion is the one people push back on, so to be
explicit: it is not supported here, on purpose. Environment variables are
inherited by every child process, show up in crash reporters and `ps` output
on some platforms, and end up pasted into issue reports. A health-data token
is worth the extra step.

`keyring` is an optional dependency. Without it, or without a working
backend (a headless Linux box with no Secret Service, typically), reads
return None and writes raise KeyringUnavailable -- a source that cannot
authenticate reports itself unhealthy rather than bringing anything down.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

SERVICE = "ticker"


class KeyringUnavailable(RuntimeError):
    """No keyring package, or no backend that can actually store anything."""


def _backend():
    """The keyring module, or None if it can't store secrets here.

    keyring installs a 'fail' backend when it finds nothing usable, which
    raises only when you try to use it -- so checking the backend's class is
    the difference between failing at startup and failing at 3am mid-sync.
    """
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    try:
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
    except Exception:
        return None
    return keyring


def available() -> bool:
    return _backend() is not None


def get_secret(ref: str, service: str = SERVICE) -> Optional[str]:
    """Read a secret by its keyring entry name. None if absent or unreadable.

    Never raises: a missing token is a health-status problem for one source,
    not an error for the process.
    """
    backend = _backend()
    if backend is None:
        log.warning("no keyring backend; cannot read %r", ref)
        return None
    try:
        return backend.get_password(service, ref)
    except Exception as exc:
        # Locked keychain, D-Bus gone, user denied the prompt.
        log.warning("could not read secret %r: %s", ref, exc)
        return None


def set_secret(ref: str, secret: str, service: str = SERVICE) -> None:
    """Store a secret. Raises if there's nowhere safe to put it.

    This one does raise: silently not storing a token the user just typed
    would leave them thinking they were set up when they weren't.
    """
    backend = _backend()
    if backend is None:
        raise KeyringUnavailable(
            "no OS keyring available. Install the 'keyring' package, or on a "
            "headless Linux box configure a Secret Service backend.")
    backend.set_password(service, ref, secret)


def delete_secret(ref: str, service: str = SERVICE) -> bool:
    """Remove a secret. True if something was removed."""
    backend = _backend()
    if backend is None:
        return False
    try:
        backend.delete_password(service, ref)
        return True
    except Exception:
        return False


# Windows' credential store holds at most 2,560 bytes per entry, and some
# sessions -- Garmin's tokens -- are bigger than that. A large secret is
# split across numbered entries, under a small index entry that says how
# many; a secret stored whole reads back through the same call.
CHUNK = 1000
_INDEX = "chunks:"


def set_large_secret(ref: str, secret: str, service: str = SERVICE) -> None:
    old = _chunk_count(ref, service)
    parts = [secret[i:i + CHUNK] for i in range(0, len(secret), CHUNK)] or [""]
    for index, part in enumerate(parts):
        set_secret("{}#{}".format(ref, index), part, service)
    # The index last, so a reader never sees a count its parts don't match.
    set_secret(ref, _INDEX + str(len(parts)), service)
    for index in range(len(parts), old):
        delete_secret("{}#{}".format(ref, index), service)


def get_large_secret(ref: str, service: str = SERVICE) -> Optional[str]:
    head = get_secret(ref, service)
    if head is None or not head.startswith(_INDEX):
        return head
    parts = [get_secret("{}#{}".format(ref, index), service)
             for index in range(_chunk_count(ref, service))]
    if any(part is None for part in parts):
        return None
    return "".join(parts)


def delete_large_secret(ref: str, service: str = SERVICE) -> bool:
    """Remove a secret however it was stored. True if something was removed."""
    for index in range(_chunk_count(ref, service)):
        delete_secret("{}#{}".format(ref, index), service)
    return delete_secret(ref, service)


def _chunk_count(ref: str, service: str) -> int:
    head = get_secret(ref, service)
    if head is None or not head.startswith(_INDEX):
        return 0
    try:
        return int(head[len(_INDEX):])
    except ValueError:
        return 0


def auth_ref(vendor: str, display_name: str) -> str:
    """The keyring entry name for a configured source.

    Derived from the source's natural key, so the same connector always
    looks in the same place and two Oura accounts don't collide.
    """
    return "{}:{}".format(vendor, display_name)

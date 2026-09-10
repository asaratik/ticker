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


def auth_ref(vendor: str, display_name: str) -> str:
    """The keyring entry name for a configured source.

    Derived from the source's natural key, so the same connector always
    looks in the same place and two Oura accounts don't collide.
    """
    return "{}:{}".format(vendor, display_name)

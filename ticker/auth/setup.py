"""
Configure a pull source: store its token, register it in the database.

    python -m ticker.auth.setup add oura --name "Ring"
    python -m ticker.auth.setup add fitbit --name "Watch"
    python -m ticker.auth.setup list
    python -m ticker.auth.setup remove oura --name "Ring"

Oura and Fitbit use OAuth flows that open a browser and wait for a loopback
redirect. Both end
in the same place: the secret goes to the OS keyring and the database
stores only the name of the entry.

Legacy token-based integrations (kept for existing installations) read a
pasted token from a prompt, never from an argument. A token on the command
line ends up in shell history, in `ps` output, and in any crash report that
captures argv, so environment variables are ruled out too. It goes straight
into the OS keyring; the database stores only the name of the entry.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Optional

from ticker import config as tconfig
from ticker.auth import secrets
from ticker.db import store
from ticker.sources.base import SourceError

# Vendors this can configure: kind, default display name, auth style.
VENDORS = {
    "oura": ("pull", "Ring", "oauth"),
    "fitbit": ("pull", "Fitbit", "oauth"),
    "garmin": ("pull", "Garmin", "password"),
}


def add_password(conn, vendor: str, display_name: str, email: str,
                 password: str, prompt_mfa, library=None) -> int:
    """Sign in with an account's email and password -- once -- and keep the
    session it yields. The password itself is never stored."""
    from ticker.sources import garmin

    if vendor != "garmin":
        raise ValueError("no password sign-in for {!r}".format(vendor))
    kind, _default, _style = VENDORS[vendor]
    ref = secrets.auth_ref(vendor, display_name)
    garmin.sign_in(email, password, ref, prompt_mfa, library=library)
    source_id = store.ensure_source(conn, kind, vendor, display_name, auth_ref=ref)
    conn.execute("UPDATE sources SET auth_ref = ?, enabled = 1 WHERE id = ?",
                 (ref, source_id))
    return source_id


def add(conn, vendor: str, display_name: str,
        token: Optional[str] = None) -> int:
    """Register a source and store its token. Returns the source id."""
    kind, _default, _style = VENDORS[vendor]
    ref = secrets.auth_ref(vendor, display_name)
    if token is None:
        token = getpass.getpass(
            "{} personal access token (input hidden): ".format(vendor))
    token = token.strip()
    if not token:
        raise ValueError("no token given")

    # Keyring first: a source row whose token failed to store would look
    # configured and never work.
    secrets.set_secret(ref, token)
    source_id = store.ensure_source(conn, kind, vendor, display_name,
                                    auth_ref=ref)
    conn.execute("UPDATE sources SET auth_ref = ?, enabled = 1 WHERE id = ?",
                 (ref, source_id))
    return source_id


def add_oauth(conn, vendor: str, display_name: str,
              open_browser=None, client_id: Optional[str] = None,
              client_secret: Optional[str] = None) -> int:
    """Register a source via the OAuth flow. Returns the source id.

    The browser half is deliberately the caller's: this opens a tab, prints
    the URL as a fallback for anyone on a headless box or behind a browser
    that will not launch, and blocks on the loopback listener until the
    redirect lands.
    """
    from ticker import config as tconfig
    from ticker.auth import oauth
    from ticker.sources import fitbit, oura

    if vendor == "oura":
        client_id = (client_id or "").strip()
        client_secret = client_secret or ""
        if not client_id or not client_secret:
            raise ValueError("Oura needs your OAuth client id and client secret")
        kind, _default, _style = VENDORS[vendor]
        ref = secrets.auth_ref(vendor, display_name)
        # Oura requires an exact registered redirect and a confidential
        # client secret. Users register this fixed loopback URI in their
        # Oura application before connecting.
        receiver = oauth.LoopbackReceiver(port=tconfig.OURA_REDIRECT_PORT)
        state = oauth.generate_state()
        url = oauth.build_authorization_url(
            oura.AUTHORIZE_URL, client_id, receiver.redirect_uri,
            oura.SCOPES, state, None)
        try:
            receiver.start()
            (open_browser or _open_browser)(url)
            code = receiver.wait(state)
            tokens = oauth.exchange_code(
                oura.TOKEN_URL, client_id, code, None, receiver.redirect_uri,
                client_secret=client_secret, secret_in_body=True)
            tokens.extra.update(oauth_client_id=client_id,
                                oauth_client_secret=client_secret)
            oauth.save_tokens(ref, tokens)
        finally:
            receiver.close()
        source_id = store.ensure_source(conn, kind, vendor, display_name,
                                        auth_ref=ref)
        conn.execute("UPDATE sources SET auth_ref = ?, enabled = 1 WHERE id = ?",
                     (ref, source_id))
        return source_id

    if vendor != "fitbit":
        raise ValueError("no OAuth flow for {!r}".format(vendor))
    if not tconfig.FITBIT_CLIENT_ID:
        raise ValueError(
            "TICKER_FITBIT_CLIENT_ID is unset. Register an application at "
            "dev.fitbit.com and set it; the client id is not a secret.")

    kind, _default, _style = VENDORS[vendor]
    ref = secrets.auth_ref(vendor, display_name)

    receiver, url, state, verifier = fitbit.begin_authorization(
        tconfig.FITBIT_CLIENT_ID)
    try:
        print("Opening your browser to authorize {}.".format(vendor))
        print("If it does not open, visit:\n\n    {}\n".format(url))
        opener = open_browser or _open_browser
        opener(url)
        # Stores the tokens in the keyring on success.
        fitbit.complete_authorization(
            receiver, state, verifier, tconfig.FITBIT_CLIENT_ID, ref)
    finally:
        # Section 8: the listener goes down the moment it is done, on every
        # path out -- including the user closing the tab.
        receiver.close()

    source_id = store.ensure_source(conn, kind, vendor, display_name,
                                    auth_ref=ref)
    conn.execute("UPDATE sources SET auth_ref = ?, enabled = 1 WHERE id = ?",
                 (ref, source_id))
    return source_id


def _open_browser(url: str) -> None:
    """Best effort. A failure here is not fatal: the URL was printed too."""
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


def remove(conn, vendor: str, display_name: str) -> bool:
    """Forget a source's token and disable it.

    The source row and its observations stay: deleting the row would cascade
    and take the data with it, which is not what 'remove this connector'
    should mean.
    """
    ref = secrets.auth_ref(vendor, display_name)
    secrets.delete_large_secret(ref)          # however it was stored
    cur = conn.execute(
        "UPDATE sources SET enabled = 0 WHERE vendor = ? AND display_name = ?",
        (vendor, display_name))
    return cur.rowcount > 0


def listing(conn):
    return conn.execute(
        "SELECT s.id, s.kind, s.vendor, s.display_name, s.enabled, s.auth_ref, "
        "       (SELECT COUNT(*) FROM observations o WHERE o.source_id = s.id) "
        "FROM sources s ORDER BY s.id").fetchall()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    add_parser = sub.add_parser("add", help="store a token and register a source")
    add_parser.add_argument("vendor", choices=sorted(VENDORS))
    add_parser.add_argument("--name", default=None,
                            help="display name, if you have more than one")

    remove_parser = sub.add_parser("remove", help="forget a token, disable a source")
    remove_parser.add_argument("vendor", choices=sorted(VENDORS))
    remove_parser.add_argument("--name", default=None)

    sub.add_parser("list", help="show configured sources")

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    conn = store.connect(args.db or tconfig.DB_PATH)
    try:
        return _dispatch(conn, args)
    finally:
        conn.close()


def _dispatch(conn, args) -> int:
    if args.command == "list":
        rows = listing(conn)
        if not rows:
            print("no sources configured")
            return 0
        for sid, kind, vendor, name, enabled, ref, count in rows:
            print("#{}  {:7s} {:12s} {:22s} {:9s} {:>8} rows  {}".format(
                sid, kind, vendor, name,
                "enabled" if enabled else "disabled", count, ref or "-"))
        return 0

    name = args.name or VENDORS[args.vendor][1]
    style = VENDORS[args.vendor][2]

    if args.command == "add":
        if not secrets.available():
            print("No OS keyring available. Install the 'keyring' package "
                  "(pip install keyring); on a headless Linux box you also "
                  "need a Secret Service backend.", file=sys.stderr)
            return 1
        try:
            if style == "oauth":
                if args.vendor == "oura":
                    source_id = add_oauth(
                        conn, args.vendor, name,
                        client_id=input("Oura OAuth client id: "),
                        client_secret=getpass.getpass(
                            "Oura OAuth client secret (input hidden): "))
                else:
                    source_id = add_oauth(conn, args.vendor, name)
            elif style == "password":
                # For a box with no browser: the same sign-in as the page.
                source_id = add_password(
                    conn, args.vendor, name, input("{} email: ".format(args.vendor)),
                    getpass.getpass("password (input hidden): "),
                    prompt_mfa=lambda: input("code {} sent you: ".format(args.vendor)))
            else:
                source_id = add(conn, args.vendor, name)
        except (ValueError, secrets.KeyringUnavailable, SourceError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except RuntimeError as exc:
            # CallbackError and friends: the user closed the tab, denied
            # access, or the redirect never arrived.
            print("authorization failed: {}".format(exc), file=sys.stderr)
            return 1
        print("configured {} as source #{} ({!r})".format(
            args.vendor, source_id, name))
        print("start Ticker (`ticker`) to begin syncing; if it is already "
              "running it picks this up within a minute")
        return 0

    if remove(conn, args.vendor, name):
        print("removed the token for {!r}; its data is untouched".format(name))
        return 0
    print("no source named {!r}".format(name), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""
The Ticker runtime: one process, everything linked into it.

    strap or watch (live) ──────┐
    Oura, Fitbit (cloud sync) ──┼─► one writer ─► one SQLite file ─┬─► the page       /
    Apple Health (file import) ─┘                                  ├─► read API       /api
                                                                   └─► agents (MCP)   /mcp, `ticker mcp`

Each of those arrows used to be its own program -- the Tk window,
ticker-sync, ticker-import, ticker-setup, ticker-server, ticker-mcp -- and
knowing which to run, how, and when was left to you. Now `ticker` starts
this, and the page is where you connect things and watch them work.

It is assembled from the parts those programs were already made of -- the
scheduler, the store, the API server, the MCP tools -- so none of that
behaviour changed. What changed is that one process owns them all, which is
what makes a single writer, "Sync now", and connecting an account while
everything runs possible at all.
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from ticker import config as tconfig
from ticker.api import server as apiserver
from ticker.app import assistant as ask
from ticker.app.live import LiveMonitor
from ticker.app.pulls import PullSupervisor
from ticker.app.settings import Settings
from ticker.app import support
from ticker.app.web import AppError, Ui
from ticker.auth import secrets, setup
from ticker.db import store
from ticker.ingest import importer
from ticker.mcp.protocol import package_version
from ticker.mcp.server import build as build_mcp
from ticker.mcp.tools import ToolError, Tools
from ticker.model import now_iso
from ticker.sources import garmin
from ticker.sources.base import PermanentError

log = logging.getLogger("ticker")

# How many finished jobs the page is shown.
MAX_JOBS = 10

# How long "Connect Fitbit" waits for the sign-in URL before giving up.
OAUTH_START_TIMEOUT_SEC = 15.0

# How long "Sign in to Garmin" waits for Garmin to either finish or ask for
# a code before answering the page, and how long a code may take to arrive.
GARMIN_START_TIMEOUT_SEC = 30.0
GARMIN_CODE_TIMEOUT_SEC = 300.0


@dataclasses.dataclass
class Job:
    """Something slow the page started and wants to follow."""

    id: str
    kind: str                      # 'import' | 'fitbit'
    title: str
    status: str = "running"        # running | done | failed
    detail: Optional[str] = None
    progress: Optional[int] = None
    started: str = dataclasses.field(default_factory=now_iso)
    finished: Optional[str] = None

    def finish(self, status: str, detail: Optional[str] = None) -> None:
        self.status, self.detail, self.finished = status, detail, now_iso()


# How many answered questions are kept for the page to fetch.
MAX_ASKS = 20


@dataclasses.dataclass
class AskJob:
    """One question to the Ask box, answered in the background."""

    id: str
    question: str
    model: str
    status: str = "running"        # running | done | failed
    steps: List[dict] = dataclasses.field(default_factory=list)
    answer: Optional[str] = None
    error: Optional[str] = None
    started: str = dataclasses.field(default_factory=now_iso)
    finished: Optional[str] = None


class Runtime:
    """Start it, wait on it, stop it. Everything else hangs off this."""

    def __init__(self, db_path: Optional[Path] = None, *,
                 host: Optional[str] = None, port: Optional[int] = None,
                 token: Optional[str] = None,
                 allow_remote: Optional[bool] = None,
                 builders=None, pull_interval: Optional[float] = None,
                 make_source: Optional[Callable] = None):
        self.db_path = Path(db_path) if db_path else tconfig.DB_PATH
        self.settings = Settings.beside(self.db_path)
        self._server_options = {"host": host, "port": port, "token": token,
                                "allow_remote": allow_remote}
        self._builders = builders
        self._pull_interval = pull_interval
        self._make_source = make_source
        self._stopping = threading.Event()
        self._jobs: List[Job] = []
        self._jobs_lock = threading.Lock()
        self._job_ids = itertools.count(1)
        self._asks: Dict[str, AskJob] = {}
        self._ask_lock = threading.Lock()
        self._ask_ids = itertools.count(1)
        self._garmin_codes: Dict[str, Tuple[Dict[str, str], threading.Event]] = {}
        self._garmin_library = None        # tests hand in a stand-in
        self.writer = None
        self.pulls: Optional[PullSupervisor] = None
        self.live: Optional[LiveMonitor] = None
        self.server = None
        self._readonly = None
        self._overview: Optional[Tools] = None
        self.started_at: Optional[str] = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> "Runtime":
        # Migrate once, before any reader or writer touches the file.
        store.connect(self.db_path).close()
        self.writer = store.AsyncStore(self.db_path, migrate_first=False)
        try:
            pull_options: Dict[str, Any] = {"builders": self._builders}
            if self._pull_interval is not None:
                pull_options["interval"] = self._pull_interval
            self.pulls = PullSupervisor(self.db_path, self.writer, **pull_options)
            self.pulls.start()

            live_options = ({"make_source": self._make_source}
                            if self._make_source else {})
            self.live = LiveMonitor(self.settings, self.writer,
                                    db_path=self.db_path, **live_options)
            self.live.start()

            mcp, self._readonly = build_mcp(self.db_path)
            self._overview = Tools(self._readonly)
            self.server = apiserver.build(
                self.db_path, writer=self.writer,
                on_sync=self.pulls.request_sync, mcp=mcp, ui=Ui(self),
                **self._server_options)
            self.server.start()
        except Exception:
            self.stop()
            raise
        self.started_at = now_iso()
        return self

    @property
    def url(self) -> Optional[str]:
        return self.server.url if self.server is not None else None

    def request_stop(self) -> None:
        """Ask the waiting thread to shut down. Safe from any thread."""
        self._stopping.set()

    @property
    def stop_requested(self) -> bool:
        return self._stopping.is_set()

    def wait(self, poll: float = 0.5) -> None:
        """Block until request_stop(). Short timed waits rather than one
        long one: an untimed wait on the main thread is where Ctrl+C goes to
        be ignored on Windows."""
        while not self._stopping.wait(poll):
            pass

    def stop(self) -> None:
        """Take everything down in the order that loses nothing: no new
        requests, then end the session, then stop syncing, then commit and
        bring the rollups up to date."""
        if self.server is not None:
            self.server.stop()
            self.server = None
        if self.live is not None:
            self.live.close()
            self.live = None
        if self.pulls is not None:
            self.pulls.stop()
            self.pulls = None
        if self.writer is not None:
            self.writer.rebuild_rollups()
            self.writer.close()
            self.writer = None
        if self._readonly is not None:
            self._readonly.close()
            self._readonly = None

    # -- what the page shows ---------------------------------------------

    def state(self) -> Dict[str, Any]:
        try:
            overview = self._overview.call("get_overview", {})
            problem = None
        except ToolError as exc:
            overview, problem = {}, str(exc)
        syncing = self.pulls.status() if self.pulls is not None else {}
        sources = []
        for source in overview.get("sources", []):
            entry = dict(source)
            status = syncing.get(source["id"])
            if status is not None:
                entry["syncing"] = status["syncing"]
                entry["problem"] = status["problem"]
            sources.append(entry)
        with self._jobs_lock:
            jobs = [dataclasses.asdict(job) for job in reversed(self._jobs)]
        url = self.url or ""
        return {
            "app": {"version": package_version(), "db": str(self.db_path),
                    "started": self.started_at, "url": url,
                    "timezone": overview.get("timezone"),
                    "problem": problem},
            "sources": sources,
            "metrics": overview.get("metrics", []),
            "sessions": overview.get("sessions", {}),
            "notes": overview.get("notes", []),
            "jobs": jobs,
            "connect": {"keyring": secrets.available(),
                        "oura": bool(secrets.available()),
                        "oura_redirect": "http://127.0.0.1:{}{}".format(
                            tconfig.OURA_REDIRECT_PORT, "/callback"),
                        "fitbit": bool(tconfig.FITBIT_CLIENT_ID),
                        "garmin": garmin.available(self._garmin_library) is None,
                        "garmin_note": garmin.available(self._garmin_library)},
            "agents": {
                "claude_code": "claude mcp add ticker -- ticker mcp",
                "codex": '[mcp_servers.ticker]\ncommand = "ticker"\nargs = ["mcp"]',
                "claude_desktop": '{ "mcpServers": { "ticker": '
                                  '{ "command": "ticker", "args": ["mcp"] } } }',
                "http": "claude mcp add --transport http ticker {}/mcp".format(url),
                "local": "{}/mcp?profile=compact".format(url),
            },
        }

    # -- what the page can do --------------------------------------------

    def sync_now(self, source_id: int) -> Dict[str, Any]:
        queued = self.pulls.request_sync(source_id)
        return {"source_id": source_id, "queued": queued,
                "message": "syncing" if queued else
                           "already syncing, or not a connected cloud account"}

    def connect_token(self, vendor: str, token: Optional[str],
                      name: Optional[str]) -> Dict[str, Any]:
        """Compatibility path for an existing token-based integration."""
        if setup.VENDORS.get(vendor, (None, None, None))[2] != "token":
            raise AppError(400, "{} doesn't connect with a token".format(vendor))
        token = (token or "").strip()
        if not token:
            raise AppError(400, "paste the access token first")
        self._require_keyring()
        name = (name or "").strip() or setup.VENDORS[vendor][1]
        conn = store.connect(self.db_path, migrate_first=False)
        try:
            source_id = setup.add(conn, vendor, name, token=token)
        except (ValueError, secrets.KeyringUnavailable) as exc:
            raise AppError(400, str(exc))
        finally:
            conn.close()
        # Rebuilt rather than woken: a connector may hold the old token.
        self.pulls.restart(source_id)
        return {"source_id": source_id, "name": name}

    def connect_oura(self, client_id: Optional[str],
                     client_secret: Optional[str],
                     name: Optional[str]) -> Dict[str, Any]:
        """Start Oura OAuth and return its authorization URL to the page."""
        client_id = (client_id or "").strip()
        client_secret = client_secret or ""
        if not client_id or not client_secret:
            raise AppError(400, "enter the client id and client secret from "
                                   "your Oura application")
        self._require_keyring()
        name = (name or "").strip() or setup.VENDORS["oura"][1]
        job = self._job("oura", "Connecting Oura ({})".format(name))
        opened = threading.Event()
        found: Dict[str, str] = {}

        def open_browser(url: str) -> None:
            found["url"] = url
            opened.set()

        def work() -> None:
            conn = store.connect(self.db_path, migrate_first=False)
            try:
                source_id = setup.add_oauth(
                    conn, "oura", name, open_browser=open_browser,
                    client_id=client_id, client_secret=client_secret)
                self.pulls.restart(source_id)
                job.finish("done", "connected as source #{}".format(source_id))
            except Exception as exc:
                job.finish("failed", str(exc))
            finally:
                conn.close()
                opened.set()

        threading.Thread(target=work, name="ticker-oura-signin",
                         daemon=True).start()
        opened.wait(OAUTH_START_TIMEOUT_SEC)
        if "url" not in found:
            raise AppError(502, job.detail or "Oura sign-in didn't start")
        return {"job_id": job.id, "url": found["url"]}

    def connect_fitbit(self, name: Optional[str]) -> Dict[str, Any]:
        """Start Fitbit's sign-in and hand back the URL for the page to open.

        setup.add_oauth runs unchanged on a worker thread: its open_browser
        hook is where the URL comes back out, and it then blocks on the
        loopback redirect while the page watches the job.
        """
        if not tconfig.FITBIT_CLIENT_ID:
            raise AppError(400, "Fitbit needs an application client id first: "
                                "register one at dev.fitbit.com, set "
                                "TICKER_FITBIT_CLIENT_ID and restart Ticker")
        self._require_keyring()
        name = (name or "").strip() or setup.VENDORS["fitbit"][1]
        job = self._job("fitbit", "Connecting Fitbit ({})".format(name))
        opened = threading.Event()
        found: Dict[str, str] = {}

        def open_browser(url: str) -> None:
            found["url"] = url
            opened.set()

        def work() -> None:
            conn = store.connect(self.db_path, migrate_first=False)
            try:
                source_id = setup.add_oauth(conn, "fitbit", name,
                                            open_browser=open_browser)
                self.pulls.restart(source_id)
                job.finish("done", "connected as source #{}".format(source_id))
            except Exception as exc:
                job.finish("failed", str(exc))
            finally:
                conn.close()
                opened.set()

        threading.Thread(target=work, name="ticker-fitbit-signin",
                         daemon=True).start()
        opened.wait(OAUTH_START_TIMEOUT_SEC)
        if "url" not in found:
            raise AppError(502, job.detail or "Fitbit sign-in didn't start")
        return {"job_id": job.id, "url": found["url"]}

    def connect_garmin(self, email: Optional[str], password: Optional[str],
                       name: Optional[str]) -> Dict[str, Any]:
        """Sign in to Garmin Connect in the background.

        Answers once Garmin has either finished or asked for a code; in the
        second case the job waits in 'needs_code' until garmin_code() hands
        it one. The password lives only in the worker's frame.
        """
        why = garmin.available(self._garmin_library)
        if why:
            raise AppError(501, why)
        self._require_keyring()
        email = (email or "").strip()
        if not email or not password:
            raise AppError(400, "enter your Garmin email and password")
        name = (name or "").strip() or setup.VENDORS["garmin"][1]
        job = self._job("garmin", "Signing in to Garmin Connect ({})".format(name))
        code: Dict[str, str] = {}
        answered, asked, settled = threading.Event(), threading.Event(), threading.Event()
        self._garmin_codes[job.id] = (code, answered)

        def prompt_mfa() -> str:
            job.status, job.detail = "needs_code", "Enter the code Garmin sent you."
            asked.set()
            if not answered.wait(GARMIN_CODE_TIMEOUT_SEC):
                raise PermanentError("no code was entered in time")
            job.status, job.detail = "running", None
            return code["code"]

        def work() -> None:
            conn = store.connect(self.db_path, migrate_first=False)
            try:
                source_id = setup.add_password(conn, "garmin", name, email, password,
                                               prompt_mfa, library=self._garmin_library)
                self.pulls.restart(source_id)
                job.finish("done", "connected as source #{}".format(source_id))
            except Exception as exc:
                job.finish("failed", str(exc))
            finally:
                conn.close()
                self._garmin_codes.pop(job.id, None)
                settled.set()

        threading.Thread(target=work, name="ticker-garmin-signin", daemon=True).start()
        deadline = GARMIN_START_TIMEOUT_SEC
        while deadline > 0 and not (asked.is_set() or settled.is_set()):
            settled.wait(0.1)
            deadline -= 0.1
        return {"job_id": job.id, "status": job.status, "detail": job.detail}

    def garmin_code(self, job_id: Optional[str], code: Optional[str]) -> Dict[str, Any]:
        entry = self._garmin_codes.get(job_id or "")
        if entry is None:
            raise AppError(404, "that sign-in isn't waiting for a code")
        code = (code or "").strip()
        if not code:
            raise AppError(400, "enter the code")
        entry[0]["code"] = code
        entry[1].set()
        return {"job_id": job_id}

    def start_import(self, path: Optional[str],
                     name: Optional[str]) -> Dict[str, Any]:
        """Import an Apple Health export in the background."""
        text = (path or "").strip().strip('"').strip("'")
        if not text:
            raise AppError(400, "give the path to the export (export.zip)")
        file = Path(os.path.expanduser(text))
        if not file.is_file():
            raise AppError(400, "no file at {}".format(file))
        try:
            vendor = importer.detect(file)
        except PermanentError as exc:
            raise AppError(400, str(exc))
        job = self._job("import", "Importing {}".format(file.name))

        def progress(count: int) -> None:
            job.progress = count

        def work() -> None:
            conn = store.connect(self.db_path, migrate_first=False)
            try:
                summary = importer.run_import(conn, self.writer, vendor,
                                              file, display_name=name or "",
                                              progress=progress)
                self.writer.rebuild_rollups()
                job.finish("done", summary["detail"])
            except Exception as exc:
                log.exception("import of %s failed", file)
                job.finish("failed", str(exc))
            finally:
                conn.close()

        threading.Thread(target=work, name="ticker-import", daemon=True).start()
        return {"job_id": job.id}

    def pick_import(self) -> Dict[str, Any]:
        try:
            return {"path": support.pick_import_file()}
        except Exception as exc:
            raise AppError(501, "file picker unavailable: {}".format(exc))

    def create_backup(self) -> Dict[str, Any]:
        from ticker.db import backup
        if self.writer is not None and not self.writer.flush(timeout=15):
            raise AppError(503, "could not finish pending database writes")
        try:
            made = backup.create(self.db_path)
        except (OSError, ValueError) as exc:
            raise AppError(500, str(exc))
        return {"file": str(made), "name": made.name}

    def diagnostics(self) -> Dict[str, Any]:
        return support.diagnostics(self.db_path, self.url or "")

    def check_update(self) -> Dict[str, Any]:
        try:
            return support.check_update()
        except RuntimeError as exc:
            raise AppError(502, str(exc))

    def disconnect(self, source_id: int) -> Dict[str, Any]:
        """Forget a cloud account's credentials and stop syncing it. Its
        data stays -- deleting the row would cascade and take it."""
        conn = store.connect(self.db_path, migrate_first=False)
        try:
            row = conn.execute(
                "SELECT kind, vendor, display_name FROM sources WHERE id = ?",
                (source_id,)).fetchone()
            if row is None:
                raise AppError(404, "no source {}".format(source_id))
            if row[0] != "pull":
                raise AppError(400, "only cloud accounts are disconnected here")
            setup.remove(conn, row[1], row[2])
        finally:
            conn.close()
        self.pulls.restart(source_id)       # which now finds it disabled
        return {"source_id": source_id}

    # -- the Ask box -----------------------------------------------------

    def ask_config(self) -> Dict[str, Any]:
        """Where the Ask box's model lives, and whether it answers."""
        url, model = self.settings.llm_url, self.settings.llm_model
        info: Dict[str, Any] = {
            "url": url, "model": model, "api": ask.detect_api(url),
            "local": ask.is_local(url), "pinned": self.settings.llm_pinned,
            "reachable": False, "models": [], "error": None}
        try:
            info["models"] = ask.make_client(url, model).models()
            info["reachable"] = True
        except ask.LlmError as exc:
            info["error"] = ask.friendly(exc)
        return info

    def set_ask_config(self, url: Optional[str],
                       model: Optional[str]) -> Dict[str, Any]:
        if self.settings.llm_pinned:
            raise AppError(409, "TICKER_LLM_URL or TICKER_LLM_MODEL is set in "
                                "the environment, so the model can't be changed "
                                "from here")
        changes: Dict[str, Any] = {}
        if url is not None:
            url = url.strip().rstrip("/")
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise AppError(400, "the model server is a URL such as "
                                    "http://127.0.0.1:11434")
            changes["llm_url"] = url
        if model is not None:
            changes["llm_model"] = model.strip()
        if changes:
            self.settings.update(**changes)
        return self.ask_config()

    def ask(self, question: Optional[str],
            history: Sequence[Tuple[str, str]] = ()) -> Dict[str, Any]:
        """Start answering a question; the page polls ask_status for it."""
        question = (question or "").strip()
        if not question:
            raise AppError(400, "ask a question")
        if len(question) > 1000:
            raise AppError(400, "that question is too long")
        url, model = self.settings.llm_url, self.settings.llm_model
        if not model:
            raise AppError(400, "pick a model first, under Model settings")
        with self._ask_lock:
            # One at a time: a local model answers one question at a time
            # anyway, and a second would only queue behind the first.
            if any(job.status == "running" for job in self._asks.values()):
                raise AppError(409, "still answering the last question")
            job = AskJob(id=str(next(self._ask_ids)), question=question,
                         model=model)
            self._asks[job.id] = job
            while len(self._asks) > MAX_ASKS:
                self._asks.pop(next(iter(self._asks)))

        client = ask.make_client(url, model, context=tconfig.LLM_CONTEXT,
                                 timeout=tconfig.LLM_TIMEOUT_SEC)
        assistant = ask.Assistant(Tools(self._readonly, profile="compact"), client)

        def work() -> None:
            try:
                result = assistant.ask(question, history, on_step=job.steps.append)
                job.answer, job.status = result["answer"], "done"
            except ask.LlmError as exc:
                job.error, job.status = ask.friendly(exc), "failed"
            except Exception as exc:
                log.exception("answering %r failed", question)
                job.error = "{}: {}".format(type(exc).__name__, exc)
                job.status = "failed"
            finally:
                job.finished = now_iso()

        threading.Thread(target=work, name="ticker-ask", daemon=True).start()
        return {"id": job.id}

    def ask_status(self, ask_id: str) -> Dict[str, Any]:
        with self._ask_lock:
            job = self._asks.get(ask_id)
            if job is None:
                raise AppError(404, "no question {}".format(ask_id))
            snapshot = dataclasses.asdict(job)
        return snapshot

    # -- plumbing --------------------------------------------------------

    @staticmethod
    def _require_keyring() -> None:
        if not secrets.available():
            raise AppError(503, "there's no OS keyring to keep the credentials "
                                "in: install the 'keyring' package (and, on a "
                                "headless Linux box, a Secret Service backend)")

    def _job(self, kind: str, title: str) -> Job:
        job = Job(id=str(next(self._job_ids)), kind=kind, title=title)
        with self._jobs_lock:
            self._jobs.append(job)
            finished = [j for j in self._jobs if j.status != "running"]
            while len(self._jobs) > MAX_JOBS and finished:
                self._jobs.remove(finished.pop(0))
        return job

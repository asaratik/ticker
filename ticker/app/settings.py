"""
Choices made in the app, kept beside the database.

Environment variables are still how Ticker is *configured*: they are what
a service manager, a container or a shell profile sets. But a choice made
on the page -- "record from my strap" -- has to survive a restart without
anyone editing an environment, so it lands here, in a small JSON file next
to the database.

Precedence, highest first: an environment variable that is actually set,
then this file, then the default. The environment wins so that a
deployment can pin something the page then can't change by accident, and
the page says so rather than pretending to.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

LIVE_SOURCES = ("off", "ble", "http")

# Off by default. The app runs all day now, and a Bluetooth scan running all
# day on a laptop that has no strap in range is a battery drain for nothing;
# turning it on is one click.
#
# The Ask box defaults to Ollama on this machine, with no model chosen: the
# page lists what that server has and asks.
DEFAULT_LLM_URL = "http://127.0.0.1:11434"
DEFAULTS: Dict[str, Any] = {"live_source": "off", "ble_address": "",
                            "llm_url": DEFAULT_LLM_URL, "llm_model": ""}


class Settings:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._values = dict(DEFAULTS)
        self._values.update(self._load())

    @classmethod
    def beside(cls, db_path: Path) -> "Settings":
        return cls(Path(db_path).parent / "settings.json")

    def _load(self) -> Dict[str, Any]:
        """What's on disk, keeping only keys this version knows. A missing
        or mangled file is the defaults, not an error: settings are a
        convenience, and losing one is better than refusing to start."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {key: value for key, value in data.items() if key in DEFAULTS}

    @property
    def live_source_pinned(self) -> bool:
        return bool(os.environ.get("HRM_SOURCE", "").strip())

    @property
    def live_source(self) -> str:
        pinned = os.environ.get("HRM_SOURCE", "").strip().lower()
        if pinned:
            return pinned
        value = self._values.get("live_source")
        return value if value in LIVE_SOURCES else "off"

    @property
    def ble_address(self) -> Optional[str]:
        return (os.environ.get("HRM_DEVICE_ADDRESS")
                or self._values.get("ble_address") or None)

    @property
    def llm_pinned(self) -> bool:
        return bool(os.environ.get("TICKER_LLM_URL", "").strip()
                    or os.environ.get("TICKER_LLM_MODEL", "").strip())

    @property
    def llm_url(self) -> str:
        return (os.environ.get("TICKER_LLM_URL", "").strip()
                or self._values.get("llm_url") or DEFAULT_LLM_URL)

    @property
    def llm_model(self) -> str:
        return (os.environ.get("TICKER_LLM_MODEL", "").strip()
                or self._values.get("llm_model") or "")

    def update(self, **changes: Any) -> None:
        unknown = sorted(set(changes) - set(DEFAULTS))
        if unknown:
            raise KeyError("unknown setting(s): {}".format(", ".join(unknown)))
        with self._lock:
            self._values.update(changes)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Written aside and renamed over, so a crash mid-write leaves the
            # old file rather than half of a new one.
            scratch = self.path.with_name(self.path.name + ".tmp")
            scratch.write_text(json.dumps(self._values, indent=2),
                               encoding="utf-8")
            os.replace(scratch, self.path)

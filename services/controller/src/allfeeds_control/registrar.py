from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .config import load_sources
from .settings import Settings
from .store import ControlStore

logger = logging.getLogger(__name__)


class Registrar:
    def __init__(self, settings: Settings, store: ControlStore):
        self.settings = settings
        self.store = store
        self.config_version: str | None = None
        self.last_error: str | None = None

    def tick(self) -> dict[str, Any]:
        loaded = load_sources(self.settings.config_path)
        if loaded.version_hash != self.config_version:
            self.store.reconcile_sources(loaded)
            self.config_version = loaded.version_hash
        registered = self.store.register_due_schedules()
        maintained = self.store.maintenance()
        self.last_error = None
        return {
            "config_version": self.config_version,
            "registered": len(registered),
            **maintained,
        }


def run_registrar(
    stop: threading.Event,
    settings: Settings,
    store: ControlStore,
    runtime_state: dict[str, Any],
    notify_local,
) -> None:
    registrar = Registrar(settings, store)
    runtime_state["registrar"] = registrar
    while not stop.is_set():
        started = time.monotonic()
        try:
            result = registrar.tick()
            runtime_state["registrar_result"] = result
            if result["registered"]:
                notify_local(result["registered"])
        except Exception as exc:
            registrar.last_error = f"{type(exc).__name__}: {exc}"
            runtime_state["registrar_error"] = registrar.last_error
            logger.exception("registrar tick failed")
        elapsed = time.monotonic() - started
        stop.wait(max(0.2, settings.registrar_tick_seconds - elapsed))

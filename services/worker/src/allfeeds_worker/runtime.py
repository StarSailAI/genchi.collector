from __future__ import annotations

import logging
import multiprocessing
import os
import queue
import signal
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
from allfeeds_contracts import FetchReport, PluginInventory, TaskEnvelope, WorkerDescriptor
from allfeeds_sdk import PluginRegistry, RateLimitError

from .assets import LocalAssetStore
from .client import ControlClient, LeaseLost
from .executor import TaskExecutor
from .settings import WorkerSettings
from .sink import PostgresSink

logger = logging.getLogger(__name__)


def _execute_child(task: TaskEnvelope, output, asset_path: str) -> None:
    try:
        report = TaskExecutor(asset_path=Path(asset_path)).execute(task)
        output.put({"ok": True, "report": report.model_dump(mode="json")})
    except BaseException as exc:
        error_class = getattr(exc, "error_class", "unknown")
        retry_after = exc.retry_after_seconds if isinstance(exc, RateLimitError) else None
        output.put(
            {
                "ok": False,
                "error_class": error_class,
                "error_message": f"{type(exc).__name__}: {exc}",
                "retry_after_seconds": retry_after,
                "traceback": traceback.format_exc(),
            }
        )


@dataclass
class RunningTask:
    task: TaskEnvelope
    process: multiprocessing.Process
    output: Any
    started: float


def discover_plugins() -> tuple[PluginRegistry, tuple[PluginInventory, ...]]:
    registry = PluginRegistry.discover()
    if not os.environ.get("S3_BUCKET", "").strip():
        registry.asset_stores.pop("s3", None)
    if "postgres" not in registry.sinks:
        registry.add_sink(PostgresSink())
    if "local" not in registry.asset_stores:
        registry.add_asset_store(LocalAssetStore())
    return registry, tuple(registry.inventories())


def descriptor(
    settings: WorkerSettings,
    *,
    instance_id: str,
    mode: str,
    inventories: tuple[PluginInventory, ...],
    metadata: dict[str, Any] | None = None,
) -> WorkerDescriptor:
    capabilities = {item.capability for item in inventories}
    for item in inventories:
        capabilities.update(item.capabilities)
    queues = {"default"}
    for item in inventories:
        if item.kind == "fetcher" and item.default_queue:
            queues.add(item.default_queue)
    return WorkerDescriptor(
        node_id=settings.node_id,
        instance_id=instance_id,
        hostname=settings.hostname,
        mode=mode,
        max_concurrency=settings.concurrency,
        software_version=settings.software_version,
        capabilities=tuple(sorted(capabilities)),
        queues=tuple(sorted(queues)),
        plugins=inventories,
        metadata=metadata or {},
    )


class WorkerRuntime:
    def __init__(
        self,
        settings: WorkerSettings,
        client: ControlClient,
        *,
        instance_id: str,
        mode: str,
        inventories: tuple[PluginInventory, ...],
    ):
        self.settings = settings
        self.client = client
        self.instance_id = instance_id
        self.mode = mode
        self.inventories = inventories
        self.mp = multiprocessing.get_context("spawn")
        self.running: dict[int, RunningTask] = {}
        self.stop = threading.Event()
        self.desired_state = "online"
        self.next_heartbeat = 0.0
        self.process = psutil.Process()
        psutil.cpu_percent(interval=None)

    def request_stop(self, *_args) -> None:
        self.stop.set()
        self.desired_state = "draining"

    def metadata(self) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        try:
            load = psutil.getloadavg()[0]
        except (AttributeError, OSError):
            load = None
        return {
            "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
            "memory_percent": round(memory.percent, 1),
            "process_rss_mb": round(self.process.memory_info().rss / 1024 / 1024, 1),
            "load_1m": round(load, 2) if load is not None else None,
            "slot_label": "slot usage",
        }

    def used_slots(self) -> int:
        return sum(item.task.slot_cost for item in self.running.values())

    def start_task(self, task: TaskEnvelope) -> None:
        output = self.mp.Queue(maxsize=1)
        process = self.mp.Process(
            target=_execute_child,
            args=(task, output, str(self.settings.asset_path)),
            name=f"allfeeds-task-{task.id}",
            daemon=False,
        )
        process.start()
        self.running[task.id] = RunningTask(task, process, output, time.monotonic())
        logger.info("task started id=%s source=%s pid=%s", task.id, task.source_id, process.pid)

    def poll_children(self) -> None:
        for task_id, child in list(self.running.items()):
            timeout = child.task.execution_timeout_seconds
            if child.process.is_alive() and time.monotonic() - child.started <= timeout:
                continue
            if child.process.is_alive():
                child.process.terminate()
                child.process.join(timeout=5)
                if child.process.is_alive():
                    child.process.kill()
                result = {
                    "ok": False,
                    "error_class": "timeout",
                    "error_message": f"task exceeded hard timeout of {timeout}s",
                }
            else:
                child.process.join(timeout=0.1)
                try:
                    result = child.output.get(timeout=1)
                except queue.Empty:
                    result = {
                        "ok": False,
                        "error_class": "unknown",
                        "error_message": f"child exited {child.process.exitcode} without result",
                    }
            try:
                if result["ok"]:
                    self.client.complete(child.task, FetchReport.model_validate(result["report"]))
                    logger.info("task completed id=%s", task_id)
                else:
                    self.client.fail(
                        child.task,
                        error_class=result.get("error_class", "unknown"),
                        error_message=result.get("error_message", "task failed"),
                        retry_after_seconds=result.get("retry_after_seconds"),
                    )
                    logger.warning(
                        "task failed id=%s error=%s", task_id, result.get("error_message")
                    )
            except LeaseLost:
                logger.warning("task result ignored after lease loss id=%s", task_id)
            except Exception:
                logger.exception("could not report task result id=%s", task_id)
            finally:
                child.output.close()
                self.running.pop(task_id, None)

    def send_heartbeat(self) -> None:
        running = [
            {"task_id": task_id, "lease_token": item.task.lease_token}
            for task_id, item in self.running.items()
        ]
        self.desired_state = self.client.heartbeat(self.instance_id, running, self.metadata())
        self.next_heartbeat = time.monotonic() + self.settings.heartbeat_seconds

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        self.client.start(
            descriptor(
                self.settings,
                instance_id=self.instance_id,
                mode=self.mode,
                inventories=self.inventories,
                metadata=self.metadata(),
            )
        )
        logger.info(
            "worker online node=%s mode=%s concurrency=%s",
            self.settings.node_id,
            self.mode,
            self.settings.concurrency,
        )
        while not self.stop.is_set() or self.running:
            self.poll_children()
            if time.monotonic() >= self.next_heartbeat:
                try:
                    self.send_heartbeat()
                except Exception:
                    logger.exception("heartbeat failed")
                    time.sleep(2)
            free = self.settings.concurrency - self.used_slots()
            if not self.stop.is_set() and self.desired_state == "online" and free > 0:
                try:
                    tasks, self.desired_state = self.client.claim(self.instance_id, free)
                    for task in tasks:
                        self.start_task(task)
                except Exception:
                    logger.exception("task claim failed")
                    time.sleep(2)
            else:
                time.sleep(0.2)
            if self.desired_state in {"draining", "disabled", "replaced"} and not self.running:
                break

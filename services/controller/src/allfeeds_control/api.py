from __future__ import annotations

import asyncio
import hmac
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from allfeeds_contracts import PROTOCOL_VERSION, FetchReport, WorkerDescriptor
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import PlainTextResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import __version__
from .db import close_pool
from .metrics import render_metrics
from .registrar import run_registrar
from .settings import Settings
from .signals import TaskSignal
from .store import ControlStore

logger = logging.getLogger(__name__)
settings: Settings | None = None
store: ControlStore | None = None
runtime_state: dict[str, Any] = {}
signal = TaskSignal()
api_key = APIKeyHeader(name="X-API-Key", auto_error=False)
worker_bearer = HTTPBearer(auto_error=False)


def get_settings() -> Settings:
    global settings
    if settings is None:
        settings = Settings.from_env()
    return settings


def get_store() -> ControlStore:
    global store
    if store is None:
        store = ControlStore(get_settings())
    return store


def require_admin(value: str | None = Security(api_key)) -> None:
    cfg = get_settings()
    if cfg.control_token and (not value or not hmac.compare_digest(value, cfg.control_token)):
        raise HTTPException(status_code=401, detail="invalid API key")


def require_worker(
    value: HTTPAuthorizationCredentials | None = Security(worker_bearer),
) -> dict[str, Any]:
    if not value or value.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="worker credential required")
    worker = get_store().authenticate_worker(value.credentials)
    if not worker:
        raise HTTPException(status_code=401, detail="invalid or revoked worker credential")
    return worker


class EnrollmentRequest(BaseModel):
    mode: Literal["resident", "burst"] = "burst"
    ttl_seconds: int = Field(default=1800, ge=60, le=86_400)
    max_uses: int = Field(default=1, ge=1, le=100)


class EnrollRequest(BaseModel):
    enrollment_token: str = Field(min_length=20)
    descriptor: WorkerDescriptor
    protocol_version: str = PROTOCOL_VERSION


class StartRequest(BaseModel):
    descriptor: WorkerDescriptor
    protocol_version: str = PROTOCOL_VERSION


class ClaimRequest(BaseModel):
    instance_id: str
    available_slots: int = Field(ge=0, le=128)
    wait_seconds: float = Field(default=20, ge=0, le=25)


class HeartbeatRequest(BaseModel):
    instance_id: str
    running: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompleteRequest(BaseModel):
    lease_token: str
    report: FetchReport


class FailRequest(BaseModel):
    lease_token: str
    error_class: str = Field(min_length=1, max_length=128)
    error_message: str = Field(min_length=1, max_length=16_000)
    retry_after_seconds: float | None = Field(default=None, ge=0, le=604_800)


class ManualTaskRequest(BaseModel):
    source_id: str
    operation: Literal["fetch"] = "fetch"
    dedupe_key: str | None = None


class BackfillRequest(BaseModel):
    source_id: str
    start: datetime
    end: datetime
    window_seconds: int | None = Field(default=None, ge=60)


class WorkerStateRequest(BaseModel):
    state: Literal["online", "draining", "disabled"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cfg = get_settings()
    control_store = get_store()
    signal.start(asyncio.get_running_loop(), cfg)
    stop = threading.Event()
    thread = threading.Thread(
        target=run_registrar,
        args=(stop, cfg, control_store, runtime_state, signal.notify_local),
        name="allfeeds-registrar",
        daemon=True,
    )
    thread.start()
    yield
    stop.set()
    signal.stop()
    thread.join(timeout=5)
    close_pool()


app = FastAPI(
    title="AllFeeds Controller",
    version=__version__,
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, Any]:
    registrar = runtime_state.get("registrar")
    error = getattr(registrar, "last_error", None)
    if error:
        raise HTTPException(status_code=503, detail={"status": "degraded", "error": error})
    return {
        "status": "ok",
        "service": "allfeeds-control",
        "version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "registrar": runtime_state.get("registrar_result"),
    }


@app.get("/metrics", response_class=PlainTextResponse, dependencies=[Depends(require_admin)])
def metrics() -> PlainTextResponse:
    return PlainTextResponse(render_metrics(get_store()), media_type="text/plain; version=0.0.4")


@app.post("/v1/enrollments", status_code=201, dependencies=[Depends(require_admin)])
def create_enrollment(request: EnrollmentRequest) -> dict[str, Any]:
    return get_store().create_enrollment(
        mode=request.mode,
        ttl_seconds=request.ttl_seconds,
        max_uses=request.max_uses,
        created_by="control-api",
    )


@app.post("/v1/workers/enroll", status_code=201)
def enroll(request: EnrollRequest) -> dict[str, Any]:
    if request.protocol_version != PROTOCOL_VERSION:
        raise HTTPException(status_code=409, detail="protocol version mismatch")
    result = get_store().enroll_worker(request.enrollment_token, request.descriptor)
    if not result:
        raise HTTPException(status_code=401, detail="invalid, expired or used enrollment token")
    return {**result, "protocol_version": PROTOCOL_VERSION}


@app.post("/v1/workers/start")
def start(
    request: StartRequest, worker: dict[str, Any] = Depends(require_worker)
) -> dict[str, Any]:
    if request.protocol_version != PROTOCOL_VERSION:
        raise HTTPException(status_code=409, detail="protocol version mismatch")
    if request.descriptor.node_id != worker["node_id"]:
        raise HTTPException(status_code=409, detail="worker identity mismatch")
    if not get_store().start_worker(request.descriptor):
        raise HTTPException(status_code=409, detail="worker is disabled")
    return {"node_id": worker["node_id"], "mode": worker["mode"]}


@app.get("/v1/workers/bootstrap")
def bootstrap(worker: dict[str, Any] = Depends(require_worker)) -> dict[str, Any]:
    registrar = runtime_state.get("registrar")
    return {
        "node_id": worker["node_id"],
        "mode": worker["mode"],
        "protocol_version": PROTOCOL_VERSION,
        "config_version": getattr(registrar, "config_version", None),
    }


@app.post("/v1/workers/claim")
async def claim(
    request: ClaimRequest,
    worker: dict[str, Any] = Depends(require_worker),
) -> dict[str, Any]:
    if request.instance_id != worker["instance_id"]:
        raise HTTPException(status_code=409, detail="worker instance was replaced")
    deadline = time.monotonic() + request.wait_seconds
    tasks = []
    state = worker["desired_state"]
    while request.available_slots and state == "online":
        tasks, state = await asyncio.to_thread(
            get_store().claim_tasks,
            node_id=worker["node_id"],
            instance_id=request.instance_id,
            available_slots=request.available_slots,
        )
        if tasks or time.monotonic() >= deadline:
            break
        await signal.wait(min(25, max(0.05, deadline - time.monotonic())))
    return {
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "desired_state": state,
    }


@app.post("/v1/workers/heartbeat")
def heartbeat(
    request: HeartbeatRequest,
    worker: dict[str, Any] = Depends(require_worker),
) -> dict[str, Any]:
    state = get_store().heartbeat(
        node_id=worker["node_id"],
        instance_id=request.instance_id,
        running=request.running,
        metadata=request.metadata,
    )
    return {"desired_state": state}


@app.post("/v1/tasks/{task_id}/complete")
def complete(
    task_id: int,
    request: CompleteRequest,
    _worker: dict[str, Any] = Depends(require_worker),
) -> dict[str, Any]:
    accepted = get_store().complete_task(
        task_id=task_id,
        lease_token=request.lease_token,
        status=request.report.status,
        report=request.report.model_dump(mode="json"),
        error_message=request.report.error_message,
    )
    if not accepted:
        raise HTTPException(status_code=409, detail="task lease was lost")
    return {"accepted": True}


@app.post("/v1/tasks/{task_id}/fail")
def fail(
    task_id: int,
    request: FailRequest,
    _worker: dict[str, Any] = Depends(require_worker),
) -> dict[str, Any]:
    result = get_store().fail_task(task_id=task_id, **request.model_dump())
    if not result["accepted"]:
        raise HTTPException(status_code=409, detail="task lease was lost")
    return result


@app.post("/v1/tasks", status_code=201, dependencies=[Depends(require_admin)])
def manual_task(request: ManualTaskRequest) -> dict[str, Any]:
    try:
        task_id = get_store().register_manual(**request.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail="source not found") from None
    signal.notify_local()
    return {"task_id": task_id, "registered": task_id is not None}


@app.post("/v1/backfills", status_code=201, dependencies=[Depends(require_admin)])
def backfill(request: BackfillRequest) -> dict[str, Any]:
    try:
        result = get_store().create_backfill(**request.model_dump(), created_by="control-api")
    except KeyError:
        raise HTTPException(status_code=404, detail="source not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    signal.notify_local(result["task_count"])
    return result


@app.get("/v1/backfills/{batch_id}", dependencies=[Depends(require_admin)])
def backfill_status(batch_id: int) -> dict[str, Any]:
    result = get_store().backfill(batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="backfill not found")
    return result


@app.post("/v1/backfills/{batch_id}/{action}", dependencies=[Depends(require_admin)])
def backfill_action(batch_id: int, action: Literal["pause", "resume", "cancel"]) -> dict[str, Any]:
    result = get_store().set_backfill_state(batch_id, action)
    if result is None:
        raise HTTPException(status_code=409, detail="backfill is not active")
    if action == "resume":
        signal.notify_local()
    return result


@app.get("/v1/overview", dependencies=[Depends(require_admin)])
def overview() -> dict[str, Any]:
    return get_store().overview()


@app.get("/v1/sources", dependencies=[Depends(require_admin)])
def sources() -> dict[str, Any]:
    return {"sources": get_store().sources()}


@app.get("/v1/tasks/stuck", dependencies=[Depends(require_admin)])
def stuck() -> dict[str, Any]:
    return {"tasks": get_store().stuck_tasks()}


@app.put("/v1/workers/{node_id}/state", dependencies=[Depends(require_admin)])
def worker_state(node_id: str, request: WorkerStateRequest) -> dict[str, Any]:
    if not get_store().set_worker_state(node_id, request.state):
        raise HTTPException(status_code=404, detail="worker not found")
    return {"node_id": node_id, "desired_state": request.state}


@app.delete("/v1/workers/{node_id}/credential", dependencies=[Depends(require_admin)])
def revoke_worker(node_id: str) -> dict[str, Any]:
    if not get_store().revoke_worker(node_id):
        raise HTTPException(status_code=404, detail="active worker credential not found")
    return {"node_id": node_id, "revoked": True}

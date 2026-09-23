from __future__ import annotations

import json
import logging
import os
import uuid

from .client import ControlClient
from .runtime import WorkerRuntime, descriptor, discover_plugins
from .settings import WorkerSettings


def _identity(settings: WorkerSettings, inventories) -> tuple[str, str, str]:
    path = settings.state_path
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        return str(value["credential"]), str(value["instance_id"]), str(value["mode"])
    if not settings.enrollment_token:
        raise RuntimeError("ENROLLMENT_TOKEN is required for first worker start")
    instance_id = uuid.uuid4().hex
    enrolling = ControlClient(
        settings.control_url,
        None,
        verify=settings.tls_verify,
        timeout=settings.request_timeout_seconds,
    )
    result = enrolling.enroll(
        settings.enrollment_token,
        descriptor(
            settings,
            instance_id=instance_id,
            mode="resident",
            inventories=inventories,
        ),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "credential": result["credential"],
                "instance_id": instance_id,
                "mode": result["mode"],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return str(result["credential"]), instance_id, str(result["mode"])


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = WorkerSettings.from_env()
    _registry, inventories = discover_plugins()
    credential, instance_id, mode = _identity(settings, inventories)
    client = ControlClient(
        settings.control_url,
        credential,
        verify=settings.tls_verify,
        timeout=settings.request_timeout_seconds,
    )
    WorkerRuntime(
        settings,
        client,
        instance_id=instance_id,
        mode=mode,
        inventories=inventories,
    ).run()


if __name__ == "__main__":
    main()

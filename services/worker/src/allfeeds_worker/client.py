from __future__ import annotations

from typing import Any

import requests
from allfeeds_contracts import PROTOCOL_VERSION, FetchReport, TaskEnvelope, WorkerDescriptor


class ControlError(RuntimeError):
    pass


class LeaseLost(ControlError):
    pass


class ControlClient:
    def __init__(
        self, base_url: str, credential: str | None, *, verify: bool | str, timeout: float
    ):
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self.verify = verify
        self.timeout = timeout
        self.session = requests.Session()

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}))
        if self.credential:
            headers["Authorization"] = f"Bearer {self.credential}"
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            timeout=self.timeout,
            verify=self.verify,
            **kwargs,
        )
        if response.status_code == 409 and "/tasks/" in path:
            raise LeaseLost(response.text)
        if response.status_code >= 400:
            raise ControlError(f"controller {response.status_code}: {response.text[:500]}")
        return response.json() if response.content else {}

    def enroll(self, token: str, descriptor: WorkerDescriptor) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/workers/enroll",
            json={
                "enrollment_token": token,
                "descriptor": descriptor.model_dump(mode="json"),
                "protocol_version": PROTOCOL_VERSION,
            },
        )

    def start(self, descriptor: WorkerDescriptor) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/workers/start",
            json={
                "descriptor": descriptor.model_dump(mode="json"),
                "protocol_version": PROTOCOL_VERSION,
            },
        )

    def bootstrap(self) -> dict[str, Any]:
        return self._request("GET", "/v1/workers/bootstrap")

    def claim(self, instance_id: str, slots: int) -> tuple[list[TaskEnvelope], str]:
        value = self._request(
            "POST",
            "/v1/workers/claim",
            json={"instance_id": instance_id, "available_slots": slots, "wait_seconds": 20},
        )
        return [TaskEnvelope.model_validate(item) for item in value["tasks"]], value[
            "desired_state"
        ]

    def heartbeat(
        self,
        instance_id: str,
        running: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> str:
        value = self._request(
            "POST",
            "/v1/workers/heartbeat",
            json={"instance_id": instance_id, "running": running, "metadata": metadata},
        )
        return str(value["desired_state"])

    def complete(self, task: TaskEnvelope, report: FetchReport) -> None:
        self._request(
            "POST",
            f"/v1/tasks/{task.id}/complete",
            json={"lease_token": task.lease_token, "report": report.model_dump(mode="json")},
        )

    def fail(
        self,
        task: TaskEnvelope,
        *,
        error_class: str,
        error_message: str,
        retry_after_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/tasks/{task.id}/fail",
            json={
                "lease_token": task.lease_token,
                "error_class": error_class,
                "error_message": error_message,
                "retry_after_seconds": retry_after_seconds,
            },
        )

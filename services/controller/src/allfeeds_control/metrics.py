from __future__ import annotations

from .store import ControlStore


def render_metrics(store: ControlStore) -> str:
    data = store.overview()
    lines = [
        "# HELP allfeeds_tasks Current tasks by state.",
        "# TYPE allfeeds_tasks gauge",
    ]
    for status, count in data["task_counts"].items():
        lines.append(f'allfeeds_tasks{{status="{status}"}} {int(count)}')
    lines.extend(
        [
            "# HELP allfeeds_workers Worker count by effective state.",
            "# TYPE allfeeds_workers gauge",
        ]
    )
    worker_states: dict[str, int] = {}
    for worker in data["workers"]:
        state = worker["effective_state"]
        worker_states[state] = worker_states.get(state, 0) + 1
    for state, count in worker_states.items():
        lines.append(f'allfeeds_workers{{state="{state}"}} {count}')
    recent = data["last_24h"]
    lines.extend(
        [
            "# HELP allfeeds_task_runs_24h Completed task runs in the last 24 hours.",
            "# TYPE allfeeds_task_runs_24h gauge",
            f'allfeeds_task_runs_24h{{status="completed"}} {int(recent["completed"])}',
            f'allfeeds_task_runs_24h{{status="succeeded"}} {int(recent["succeeded"])}',
            f'allfeeds_task_runs_24h{{status="issues"}} {int(recent["issues"])}',
            "# HELP allfeeds_resources_total Stored current resources.",
            "# TYPE allfeeds_resources_total gauge",
            f"allfeeds_resources_total {int(data['resources']['total'])}",
        ]
    )
    return "\n".join(lines) + "\n"

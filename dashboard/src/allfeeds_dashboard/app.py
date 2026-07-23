from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any

import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(
    page_title="AllFeeds Operations",
    page_icon=Path(__file__).with_name("favicon.png"),
    layout="wide",
)
st.markdown(
    """
<style>
:root{--bg:#0b0f14;--panel:#111821;--line:#263548;--muted:#8494aa;--text:#d9e2ec;
--blue:#58a6ff;--green:#3fb950;--amber:#d29922;--red:#f85149;--purple:#bc8cff}
html,body,[data-testid="stAppViewContainer"]{background:var(--bg)!important;color:var(--text)!important}
#MainMenu,footer,header[data-testid="stHeader"],[data-testid="stToolbar"]{display:none!important}
.block-container{max-width:1600px;padding:1.2rem 1.5rem 3rem!important}
*{font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.brand{display:flex;align-items:center;gap:13px}.mark{width:36px;height:36px;display:grid;place-items:center;
border-radius:9px;background:#1f6feb22;border:1px solid #1f6feb88;color:var(--blue);font-weight:800}
.title{font-size:18px;font-weight:750;color:#f0f6fc}.sub{font-size:12px;color:var(--muted)}
.health{display:inline-block;border-radius:999px;padding:5px 10px;font:700 12px monospace}
.healthy{color:var(--green);border:1px solid #23863688;background:#23863622}
.attention{color:var(--amber);border:1px solid #9e6a0388;background:#9e6a0322}
.error{color:var(--red);border:1px solid #f85149aa;background:#f8514922}
.metrics{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:10px;margin:16px 0 20px}
.metric,.worker{background:linear-gradient(145deg,var(--panel),#0f151d);border:1px solid var(--line);border-radius:9px;padding:13px}
.label{font:650 10px monospace;color:var(--muted);letter-spacing:.5px;text-transform:uppercase}
.value{font-size:27px;font-weight:750;margin-top:7px}.note{font-size:10px;color:var(--muted)}
.blue{color:var(--blue)}.green{color:var(--green)}.amber{color:var(--amber)}.red{color:var(--red)}.purple{color:var(--purple)}
.section{font-size:14px;font-weight:700;color:#f0f6fc;border-bottom:1px solid var(--line);padding:20px 0 8px;margin-bottom:9px}
.workers{display:grid;grid-template-columns:repeat(3,minmax(280px,1fr));gap:10px}.worker-top{display:flex;justify-content:space-between}.worker-name{font:700 13px monospace}.online{color:var(--green)}.offline{color:var(--red)}.draining,.disabled{color:var(--amber)}
.bar{height:6px;background:#253142;border-radius:99px;margin:10px 0 7px;overflow:hidden}.bar span{display:block;height:100%;background:var(--blue)}
.meta{color:var(--muted);font:11px/1.55 monospace;overflow-wrap:anywhere}
div[data-testid="stDataFrame"]{border:1px solid var(--line);border-radius:8px}
@media(max-width:1100px){.metrics{grid-template-columns:repeat(4,1fr)}.workers{grid-template-columns:repeat(2,1fr)}}
@media(max-width:700px){.metrics{grid-template-columns:repeat(2,1fr)}.workers{grid-template-columns:1fr}}
</style>
""",
    unsafe_allow_html=True,
)


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


@st.cache_data(ttl=5, show_spinner=False)
def load_data() -> dict[str, Any]:
    base = os.environ.get("CONTROL_URL", "http://control:8060").rstrip("/")
    token = os.environ.get("CONTROL_API_TOKEN", "")
    response = requests.get(
        f"{base}/v1/overview",
        headers={"X-API-Key": token},
        timeout=10,
        verify=os.environ.get("CONTROL_TLS_VERIFY", "1") not in {"0", "false"},
    )
    response.raise_for_status()
    return response.json()


def health(data: dict[str, Any]) -> str:
    counts = data["task_counts"]
    workers = data["workers"]
    online = sum(1 for worker in workers if worker["effective_state"] == "online")
    pending = int(counts.get("pending", 0)) + int(counts.get("retry", 0))
    issues = int(data["last_24h"]["issues"])
    completed = int(data["last_24h"]["completed"])
    if (pending and not online) or issues >= 10 or (completed >= 20 and issues / completed >= 0.25):
        return "error"
    if counts.get("retry", 0) or any(worker["effective_state"] != "online" for worker in workers):
        return "attention"
    return "healthy"


def metric(label: str, value: Any, note: str, color: str) -> str:
    return (
        f'<div class="metric"><div class="label">{esc(label)}</div>'
        f'<div class="value {color}">{esc(value)}</div><div class="note">{esc(note)}</div></div>'
    )


header_left, header_actions, header_state = st.columns([6, 2.2, 2.2], vertical_alignment="center")
with header_left:
    st.markdown(
        '<div class="brand"><div class="mark">AF</div><div><div class="title">AllFeeds Operations</div><div class="sub">sources · task pool · workers · execution history · resources</div></div></div>',
        unsafe_allow_html=True,
    )
with header_actions:
    auto_col, refresh_col = st.columns([1.3, 1])
    with auto_col:
        auto = st.toggle("Auto refresh", value=True)
    with refresh_col:
        if st.button("↻ Refresh", width="stretch"):
            load_data.clear()
            st.rerun()
if auto:
    st_autorefresh(interval=5000, key="allfeeds-auto-refresh")

try:
    data = load_data()
except Exception as exc:
    st.error(f"Unable to load Controller state: {exc}")
    st.stop()

state = health(data)
with header_state:
    st.markdown(
        f'<div style="text-align:right"><span class="health {state}">● {state}</span>'
        f'<div class="sub">{esc(data["generated_at"])}</div></div>',
        unsafe_allow_html=True,
    )

counts = data["task_counts"]
recent = data["last_24h"]
resources = data["resources"]
genchi = data.get("genchi") or {}
normalization = genchi.get("normalization") or {}
cards = [
    metric("Pending", counts.get("pending", 0), "ready tasks", "amber"),
    metric("Running", counts.get("running", 0), "leased tasks", "green"),
    metric("Retry", counts.get("retry", 0), "delayed retry", "red"),
    metric("24h Success", recent["succeeded"], f"{recent['completed']} completed", "blue"),
    metric("24h Issues", recent["issues"], "partial + dead", "red"),
    metric("Rows Added", recent["rows_added"], "last 24 hours", "purple"),
    metric("Resources", resources["total"], f"{resources['recent_24h']} new in 24h", "blue"),
    metric("Searchable", genchi.get("content_total", 0), "Genchi content", "purple"),
    metric("Normalize Queue", int(normalization.get("PENDING", 0)) + int(normalization.get("RETRY", 0)), "pending + retry", "amber"),
    metric("Review", genchi.get("candidates_pending", 0), "fact candidates", "amber"),
]
st.markdown('<div class="metrics">' + "".join(cards) + "</div>", unsafe_allow_html=True)

st.markdown('<div class="section">Workers</div>', unsafe_allow_html=True)
worker_cards = []
for worker in data["workers"]:
    maximum = max(1, int(worker["max_concurrency"]))
    running = int(worker["running_slots"])
    percent = min(100, running / maximum * 100)
    metadata = worker.get("metadata") or {}
    plugins = (
        ", ".join(f"{plugin['kind']}:{plugin['name']}" for plugin in (worker.get("plugins") or []))
        or "none"
    )
    effective = worker["effective_state"]
    worker_cards.append(
        '<div class="worker"><div class="worker-top">'
        f'<div><div class="worker-name">{esc(worker["node_id"])}</div><div class="sub">{esc(worker["hostname"])}</div></div>'
        f'<b class="{esc(effective)}">● {esc(effective.upper())}</b></div>'
        f'<div class="bar" title="slot usage {running}/{maximum}"><span style="width:{percent:.1f}%"></span></div>'
        f'<div class="meta">slot usage {running}/{maximum} · mode {esc(worker["mode"])}<br>'
        f"CPU {esc(metadata.get('cpu_percent', '—'))}% · MEM {esc(metadata.get('memory_percent', '—'))}% · RSS {esc(metadata.get('process_rss_mb', '—'))}MB<br>"
        f"queues {esc(', '.join(worker.get('queues') or []))}<br>plugins {esc(plugins)}<br>"
        f"version {esc(worker['software_version'])}</div></div>"
    )
st.markdown(
    '<div class="workers">' + "".join(worker_cards) + "</div>"
    if worker_cards
    else "No workers registered.",
    unsafe_allow_html=True,
)

left, right = st.columns(2)
with left:
    st.markdown('<div class="section">Running</div>', unsafe_allow_html=True)
    st.dataframe(data["running"], width="stretch", height=260, hide_index=True)
with right:
    st.markdown('<div class="section">Queue</div>', unsafe_allow_html=True)
    st.dataframe(data["queue"], width="stretch", height=260, hide_index=True)

st.markdown('<div class="section">Errors</div>', unsafe_allow_html=True)
error_page_size = 10
error_pages = max(1, (len(data["errors"]) + error_page_size - 1) // error_page_size)
error_page = st.number_input("Error page", min_value=1, max_value=error_pages, value=1)
start = (int(error_page) - 1) * error_page_size
st.dataframe(
    data["errors"][start : start + error_page_size], width="stretch", hide_index=True
)

st.markdown('<div class="section">Recent runs</div>', unsafe_allow_html=True)
run_page_size = 20
run_pages = max(1, (len(data["recent_runs"]) + run_page_size - 1) // run_page_size)
run_page = st.number_input("Run page", min_value=1, max_value=run_pages, value=1)
start = (int(run_page) - 1) * run_page_size
st.dataframe(
    data["recent_runs"][start : start + run_page_size], width="stretch", hide_index=True
)

st.markdown('<div class="section">Schedules</div>', unsafe_allow_html=True)
st.dataframe(data["schedules"], width="stretch", height=360, hide_index=True)

st.markdown('<div class="section">Resource kinds</div>', unsafe_allow_html=True)
st.dataframe(data["resource_kinds"], width="stretch", hide_index=True)

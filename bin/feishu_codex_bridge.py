#!/usr/bin/env python3
"""
Feishu/Lark CLI <-> Codex bridge.

This is intentionally a user-started process, not a launchd/service install.
It keeps state under ./var and can run in foreground or as a pidfile-backed
background process.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import errno
import gzip
import json
import os
import pty
import queue
import re
import select
import shlex
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is available on macOS/Linux.
    fcntl = None


ROOT = Path(__file__).resolve().parents[1]
VAR_DIR = ROOT / "var"
DB_PATH = VAR_DIR / "bridge.sqlite3"
PID_PATH = VAR_DIR / "bridge.pid"
LOG_PATH = VAR_DIR / "bridge.log"
HEARTBEAT_PID_PATH = VAR_DIR / "heartbeat.pid"
HEARTBEAT_LOG_PATH = VAR_DIR / "heartbeat.log"
TIMINGS_PATH = ROOT / "logs" / "timings.ndjson"
WATCH_PATH = ROOT / "logs" / "watch.ndjson"
RAW_DIFY_AUDIT_RESULTS_PATH = ROOT / "inbox" / "raw-dify-audit-results.ndjson"
AI_SEMANTIC_RESULTS_PATH = ROOT / "inbox" / "ai-semantic-results.ndjson"
WATCH_HEARTBEAT_STATE_PATH = ROOT / "state" / "watch-heartbeat.json"
EXECUTOR_STATE_PATH = ROOT / "state" / "executor-state.json"
DEFAULT_AGENT_BRIDGE_ROOT = Path.home() / "Desktop" / "agent-bridge"
DEFAULT_LARK_RUN_JS = (
    Path.home()
    / ".npm/_npx/8f08eae71a6e4041/node_modules/@larksuite/cli/scripts/run.js"
)
FEISHU_TEXT_CHUNK_SIZE = 12000
AI_SEMANTIC_TASK_TYPE = "AI_SEMANTIC_GENERATE"
AI_SEMANTIC_RESULT_TOP_LEVEL_KEYS = (
    "tableSemantic",
    "columnSemantics",
    "entitySemantics",
    "dimensionSemantics",
    "relationSemantics",
    "metricSemantics",
    "glossarySemantics",
    "aiUsageSemantic",
    "pendingQuestions",
)
CODEX_FAILURE_TAIL_LINES = 12
CODEX_FAILURE_TAIL_CHARS = 2000
HEALTHCHECK_LOG_TAIL_BYTES = 512 * 1024
DEFAULT_HEALTHCHECK_ERROR_WINDOW_SECONDS = 180
DEFAULT_HEALTHCHECK_ERROR_THRESHOLD = 3
LOCAL_BRIDGE_LABEL = "【本地桥接模式】"
LLM_EXECUTOR_CODEX = "codex"
LLM_EXECUTOR_CLAUDE = "claude"
LLM_EXECUTOR_ROUND_ROBIN = "round_robin"
LLM_EXECUTOR_WORKER_POOL = "worker_pool"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_utc_from_ms(value: int) -> str:
    return (
        dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def default_agent_bridge_root() -> Path:
    override = os.environ.get("AGENT_BRIDGE_DIR") or os.environ.get("AGENT_BRIDGE_ROOT")
    return Path(override).expanduser() if override else DEFAULT_AGENT_BRIDGE_ROOT


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)


def compact_codex_failure_tail(lines: list[str]) -> str:
    if not lines:
        return ""
    tail = "\n".join(lines[-CODEX_FAILURE_TAIL_LINES:])
    if len(tail) <= CODEX_FAILURE_TAIL_CHARS:
        return tail
    return tail[-CODEX_FAILURE_TAIL_CHARS:]


def local_bridge_text(text: str) -> str:
    value = str(text or "")
    if value.startswith(LOCAL_BRIDGE_LABEL):
        return value
    return f"{LOCAL_BRIDGE_LABEL}\n{value}"


def local_bridge_title(title: str) -> str:
    value = str(title or "").strip()
    if LOCAL_BRIDGE_LABEL in value:
        return value
    return f"{LOCAL_BRIDGE_LABEL}{value}"


def ensure_var_dir() -> None:
    VAR_DIR.mkdir(parents=True, exist_ok=True)


def append_json_line(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return json.load(handle)
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, json.JSONDecodeError):
        return default


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def resolve_lark_cli() -> list[str]:
    override = os.environ.get("LARK_CLI")
    if override:
        return shlex.split(override)

    # Prefer a working PATH command, but the Homebrew symlink can be stale.
    try:
        completed = subprocess.run(
            ["lark-cli", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if completed.returncode == 0:
            return ["lark-cli"]
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    if DEFAULT_LARK_RUN_JS.exists():
        return ["node", str(DEFAULT_LARK_RUN_JS)]

    return ["lark-cli"]


def run_json(cmd: list[str], *, check: bool = True) -> dict[str, Any]:
    log(f"$ {' '.join(cmd)}")
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0 and check:
        raise RuntimeError(
            "command failed\n"
            f"cmd: {' '.join(cmd)}\n"
            f"exit: {completed.returncode}\n"
            f"stdout: {completed.stdout}\n"
            f"stderr: {completed.stderr}"
        )
    payload = completed.stdout.strip()
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"expected JSON from {' '.join(cmd)}: {payload}") from exc


def parse_event_status(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "bus_running": False,
        "bus_pid": None,
        "active_consumers": 0,
        "consumers": [],
        "raw": text,
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("Bus:"):
            result["bus_running"] = bool(re.search(r"\brunning\b", line)) and "not running" not in line
            marker = "PID "
            if marker in line:
                tail = line.split(marker, 1)[1]
                pid_text = tail.split(",", 1)[0].strip().strip(")")
                if pid_text.isdigit():
                    result["bus_pid"] = int(pid_text)
        elif line.startswith("Active consumers:"):
            value = line.split(":", 1)[1].strip()
            if value.isdigit():
                result["active_consumers"] = int(value)
        elif line.startswith("pid="):
            parts = line.split()
            consumer: dict[str, Any] = {}
            for part in parts:
                if part.startswith("pid="):
                    pid_text = part.split("=", 1)[1]
                    if pid_text.isdigit():
                        consumer["pid"] = int(pid_text)
                elif part.isdigit():
                    if "received" not in consumer:
                        consumer["received"] = int(part)
                    else:
                        consumer["dropped"] = int(part)
                elif part.startswith("im."):
                    consumer["event_key"] = part
            if consumer:
                result["consumers"].append(consumer)
    result["received"] = sum(int(c.get("received", 0)) for c in result["consumers"])
    result["dropped"] = sum(int(c.get("dropped", 0)) for c in result["consumers"])
    return result


def bridge_pid_status() -> dict[str, Any]:
    status: dict[str, Any] = {"pid": None, "pid_file_exists": PID_PATH.exists(), "running": False}
    if not PID_PATH.exists():
        return status
    try:
        pid = int(PID_PATH.read_text().strip())
    except OSError as exc:
        status["error"] = f"read pid file failed: {exc}"
        return status
    except ValueError:
        status["error"] = "invalid pid file"
        return status
    status["pid"] = pid
    status["running"] = is_bridge_pid(pid)
    if is_pid_alive(pid) and not status["running"]:
        status["error"] = "pid belongs to another process"
    return status


def recent_bridge_error_counts(window_seconds: int) -> dict[str, Any]:
    result = {
        "window_seconds": window_seconds,
        "eperm": 0,
        "operation_not_permitted": 0,
        "lines": 0,
        "last_error": "",
    }
    if window_seconds <= 0 or not LOG_PATH.exists():
        return result
    cutoff = time.time() - window_seconds
    try:
        if PID_PATH.exists():
            cutoff = max(cutoff, PID_PATH.stat().st_mtime)
    except OSError:
        pass
    try:
        with LOG_PATH.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - HEALTHCHECK_LOG_TAIL_BYTES))
            data = handle.read().decode("utf-8", errors="ignore")
    except OSError as exc:
        result["last_error"] = f"read bridge log failed: {exc}"
        return result
    for line in data.splitlines():
        if len(line) < 19:
            continue
        try:
            line_time = dt.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if line_time < cutoff:
            continue
        result["lines"] += 1
        if "EPERM" in line:
            result["eperm"] += 1
            result["last_error"] = line[-500:]
        if "Operation not permitted" in line:
            result["operation_not_permitted"] += 1
            result["last_error"] = line[-500:]
    return result


def build_healthcheck(
    feishu: "FeishuClient",
    *,
    error_window_seconds: int = DEFAULT_HEALTHCHECK_ERROR_WINDOW_SECONDS,
    error_threshold: int = DEFAULT_HEALTHCHECK_ERROR_THRESHOLD,
) -> dict[str, Any]:
    auth = feishu.auth_probe()
    pid = bridge_pid_status()
    event_status = parse_event_status(feishu.event_status())
    issues: list[str] = []
    if not auth.get("ok"):
        message = auth.get("message") or "lark-cli auth status failed"
        issues.append(f"lark-cli auth failed: {message}")
    elif auth.get("identity") != "bot":
        issues.append(f"lark-cli identity expected bot, got {auth.get('identity')}")
    if not pid["running"]:
        issues.append("bridge process is not running")
    if not event_status["bus_running"]:
        issues.append("lark event bus is not running")
    if event_status["active_consumers"] != 1:
        issues.append(f"active consumers expected 1, got {event_status['active_consumers']}")
    if event_status.get("dropped", 0) > 0:
        issues.append(f"dropped events is {event_status['dropped']}")
    recent_errors = recent_bridge_error_counts(error_window_seconds)
    recent_error_count = int(recent_errors.get("eperm", 0)) + int(recent_errors.get("operation_not_permitted", 0))
    if error_threshold > 0 and recent_error_count >= error_threshold:
        issues.append(
            "recent bridge EPERM/Operation not permitted errors "
            f"is {recent_error_count} in {error_window_seconds}s"
        )
    return {
        "schema": "feishu-codex.health.v1",
        "ts": iso_utc(),
        "root": str(ROOT),
        "healthy": not issues,
        "issues": issues,
        "auth": auth,
        "bridge": pid,
        "event_bus": event_status,
        "recent_errors": recent_errors,
    }


def record_watch(action: str, health: dict[str, Any], *, detail: str = "") -> None:
    record = {
        "schema": "feishu-codex.watch.v1",
        "ts": iso_utc(),
        "action": action,
        "healthy": health.get("healthy"),
        "issues": health.get("issues", []),
        "detail": detail,
        "auth": {
            key: health.get("auth", {}).get(key)
            for key in ("ok", "identity", "appId", "error_type", "message")
        },
        "bridge": health.get("bridge"),
        "event_bus": {
            key: health.get("event_bus", {}).get(key)
            for key in ("bus_running", "bus_pid", "active_consumers", "received", "dropped")
        },
    }
    append_json_line(WATCH_PATH, record)
    log(f"watch action={action} healthy={record['healthy']} issues={record['issues']} {detail}".strip())


def running_responses() -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
              r.event_id,
              r.incoming_message_id,
              r.reply_message_id,
              r.chat_id,
              r.updated_at,
              e.received_at,
              e.content
            FROM responses r
            LEFT JOIN events e ON e.event_id = r.event_id
            WHERE r.status = 'running'
            ORDER BY COALESCE(e.received_at, r.updated_at) ASC
            """
        ).fetchall()
    finally:
        conn.close()
    now = now_ms()
    result: list[dict[str, Any]] = []
    for row in rows:
        started_at = int(row["received_at"] or row["updated_at"] or now)
        result.append(
            {
                "event_id": row["event_id"],
                "incoming_message_id": row["incoming_message_id"],
                "reply_message_id": row["reply_message_id"],
                "chat_id": row["chat_id"],
                "content": row["content"] or "",
                "started_at": started_at,
                "age_seconds": max(0, int((now - started_at) / 1000)),
            }
        )
    return result


def notify_long_running_responses(
    args: argparse.Namespace,
    feishu: "FeishuClient",
    health: dict[str, Any],
) -> None:
    if args.running_heartbeat_after <= 0 or args.running_heartbeat_interval <= 0:
        return
    running = running_responses()
    if not running:
        return
    state = read_json_file(WATCH_HEARTBEAT_STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    now = now_ms()
    changed = False
    for item in running:
        event_id = str(item.get("event_id") or "")
        chat_id = str(item.get("chat_id") or "")
        if not event_id or not chat_id:
            continue
        age_seconds = int(item.get("age_seconds") or 0)
        if age_seconds < args.running_heartbeat_after:
            continue
        last_sent = int(state.get(event_id, 0) or 0)
        if now - last_sent < int(args.running_heartbeat_interval * 1000):
            continue
        minutes = max(1, int(round(age_seconds / 60)))
        text = (
            f"Codex 仍在处理上一条请求，已运行约 {minutes} 分钟。"
            "如果原回复超过飞书可编辑窗口，最终结果会自动用新消息补发。"
        )
        try:
            message_id = feishu.send_text(chat_id, text)
            state[event_id] = now
            changed = True
            record_watch(
                "running_heartbeat",
                health,
                detail=f"event={event_id} age_seconds={age_seconds} message={message_id}",
            )
        except Exception as exc:
            record_watch(
                "running_heartbeat_failed",
                health,
                detail=f"event={event_id} age_seconds={age_seconds} error={exc}",
            )
    if changed:
        write_json_file(WATCH_HEARTBEAT_STATE_PATH, state)


class State:
    def __init__(self, path: Path = DB_PATH) -> None:
        ensure_var_dir()
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                message_id TEXT,
                chat_id TEXT,
                sender_id TEXT,
                content TEXT,
                raw_json TEXT NOT NULL,
                received_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                feishu_message_id TEXT,
                event_id TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS responses (
                event_id TEXT PRIMARY KEY,
                incoming_message_id TEXT,
                reply_message_id TEXT,
                chat_id TEXT,
                status TEXT NOT NULL,
                output TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );
            """
        )
        self.conn.commit()

    def insert_event_once(self, event: dict[str, Any]) -> bool:
        try:
            self.conn.execute(
                """
                INSERT INTO events (
                    event_id, message_id, chat_id, sender_id, content, raw_json, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.get("event_id"),
                    event.get("message_id") or event.get("id"),
                    event.get("chat_id"),
                    event.get("sender_id"),
                    event.get("content", ""),
                    json.dumps(event, ensure_ascii=False),
                    now_ms(),
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def add_message(
        self,
        *,
        chat_id: str,
        role: str,
        content: str,
        feishu_message_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO messages (
                chat_id, role, content, feishu_message_id, event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, role, content, feishu_message_id, event_id, now_ms()),
        )
        self.conn.commit()

    def mark_response(
        self,
        *,
        event_id: str,
        incoming_message_id: str,
        reply_message_id: Optional[str],
        chat_id: str,
        status: str,
        output: str = "",
        error: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO responses (
                event_id, incoming_message_id, reply_message_id, chat_id,
                status, output, error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                reply_message_id=excluded.reply_message_id,
                status=excluded.status,
                output=excluded.output,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (
                event_id,
                incoming_message_id,
                reply_message_id,
                chat_id,
                status,
                output,
                error,
                now_ms(),
            ),
        )
        self.conn.commit()

    def recent_history(self, chat_id: str, limit: int) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            """
            SELECT role, content
            FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        return list(reversed(rows))

    def all_events(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT event_id, message_id, chat_id, sender_id, content, raw_json, received_at
            FROM events
            ORDER BY received_at ASC
            """
        ).fetchall()

    def all_responses(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT event_id, incoming_message_id, reply_message_id, chat_id,
                   status, output, error, updated_at
            FROM responses
            ORDER BY updated_at ASC
            """
        ).fetchall()


@dataclass
class BridgeConfig:
    lark_cli: list[str]
    codex_cmd: list[str]
    claude_cmd: list[str]
    history_limit: int = 12
    update_interval: float = 0.6
    simulated_chunk_size: int = 80
    placeholder_text: str = "Codex Thinking\n已收到，准备处理。"
    output_mode: str = "edit_append"
    direct_chunk_min_chars: int = 80
    agent_bridge_root: Path = DEFAULT_AGENT_BRIDGE_ROOT
    progress_interval: float = 30.0
    progress_enabled: bool = True
    fallback_message_interval: float = 60.0
    llm_executor: str = "codex"
    claude_model: str = "claude-opus-4-8"
    claude_effort: str = "high"
    codex_timeout: float = 900.0
    codex_retries: int = 1
    raw_dify_audit_callback_enabled: bool = False
    enable_worker_pool: bool = False
    executor_pool: str = "codex:1,claude:1"
    worker_queue_size: int = 100


@dataclass
class BridgeTask:
    event: dict[str, Any]
    event_id: str
    incoming_message_id: str
    reply_message_id: str
    chat_id: str
    content: str
    ai_semantic_task: dict[str, Any]
    created_at: float
    timing: Any


class TimingTrace:
    def __init__(self, event: dict[str, Any], *, output_mode: str) -> None:
        self.started_monotonic = time.monotonic()
        self.event_id = str(event.get("event_id") or "")
        self.request_id = AgentBridgeMirror._request_record_id_static(self.event_id)
        self.incoming_message_id = str(event.get("message_id") or event.get("id") or "")
        self.chat_id = str(event.get("chat_id") or "")
        self.sender_id = str(event.get("sender_id") or "")
        self.output_mode = output_mode
        self.started_at = iso_utc()
        self.marks_ms: dict[str, int] = {}
        self.counters: dict[str, int] = {
            "codex_json_events": 0,
            "codex_delta_chunks": 0,
            "codex_completed_messages": 0,
            "codex_simulated_chunks": 0,
            "feishu_updates": 0,
            "feishu_progress_updates": 0,
        }
        self.meta: dict[str, Any] = {}

    def mark(self, name: str) -> None:
        if name not in self.marks_ms:
            self.marks_ms[name] = int((time.monotonic() - self.started_monotonic) * 1000)

    def bump(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def set_meta(self, key: str, value: Any) -> None:
        self.meta[key] = value

    def write(
        self,
        *,
        status: str,
        reply_message_id: Optional[str],
        output: str = "",
        error: str = "",
    ) -> None:
        self.mark("total")
        record = {
            "schema": "feishu-codex.timing.v1",
            "ts": iso_utc(),
            "started_at": self.started_at,
            "status": status,
            "event": {
                "event_id": self.event_id,
                "request_id": self.request_id,
                "incoming_message_id": self.incoming_message_id,
                "reply_message_id": reply_message_id,
                "chat_id": self.chat_id,
                "sender_id": self.sender_id,
            },
            "output_mode": self.output_mode,
            "timings_ms": self.marks_ms,
            "counters": self.counters,
            "output_chars": len(output),
            "error": error,
            "meta": self.meta,
        }
        try:
            append_json_line(TIMINGS_PATH, record)
            log(
                "timing "
                f"event={self.event_id} status={status} total_ms={self.marks_ms.get('total')} "
                f"placeholder_ms={self.marks_ms.get('placeholder_sent')} "
                f"codex_first_output_ms={self.marks_ms.get('codex_first_output')} "
                f"first_update_ms={self.marks_ms.get('feishu_first_update')} "
                f"updates={self.counters.get('feishu_updates')}"
            )
        except Exception as exc:
            log(f"timing write failed for {self.event_id}: {exc}")


class AgentBridgeMirror:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()
        self.base = self.root / "feishu-codex"
        self.request_file = self.base / "inbox" / "feishu-to-codex.ndjson"
        self.result_file = self.base / "inbox" / "codex-to-feishu.ndjson"
        self.events_file = self.base / "logs" / "events.ndjson"
        self.state_file = self.base / "state" / "session-map.json"

    def ensure(self) -> None:
        for directory in (
            self.base / "inbox",
            self.base / "logs",
            self.base / "state",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        for path in (
            self.request_file,
            self.result_file,
            self.events_file,
        ):
            path.touch(exist_ok=True)
        if not self.state_file.exists():
            self._write_state(self._empty_state())
        else:
            self._write_state(self._read_state())

    def record_request(self, event: dict[str, Any], *, ts: Optional[str] = None) -> None:
        self.ensure()
        event_id = str(event.get("event_id"))
        chat_id = str(event.get("chat_id") or "")
        sender_id = str(event.get("sender_id") or "")
        content = str(event.get("content") or "")
        message_id = str(event.get("message_id") or event.get("id") or "")
        request_id = self._request_record_id(event_id)
        record = {
            "id": request_id,
            "ts": ts or iso_utc(),
            "from": "feishu",
            "to": "codex",
            "kind": "request",
            "type": "request",
            "request_id": request_id,
            "project": str(ROOT),
            "session": {
                "feishu": chat_id,
                "codex": "codex-cli",
            },
            "prompt": content,
            "text": content,
            "meta": {
                "protocol_version": "agent-bridge.v1",
                "source": "feishu-codex",
                "feishu": {
                    "event_id": event_id,
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "sender_id": sender_id,
                    "message_type": event.get("message_type"),
                    "chat_type": event.get("chat_type"),
                },
            },
        }
        self._append(self.request_file, record)
        self._append_events(record)
        self._update_session(chat_id, sender_id, event_id, message_id, None)

    def record_result(
        self,
        *,
        event_id: str,
        chat_id: str,
        incoming_message_id: str,
        reply_message_id: Optional[str],
        output: str,
        status: str,
        error: str = "",
        ts: Optional[str] = None,
    ) -> None:
        self.ensure()
        request_id = self._request_record_id(event_id)
        record = {
            "id": self._event_record_id("codex", event_id, ts),
            "ts": ts or iso_utc(),
            "from": "codex",
            "to": "feishu",
            "kind": "result",
            "type": "result",
            "request_id": request_id,
            "project": str(ROOT),
            "session": {
                "feishu": chat_id,
                "codex": "codex-cli",
            },
            "result": output,
            "text": output,
            "meta": {
                "protocol_version": "agent-bridge.v1",
                "source": "feishu-codex",
                "status": status,
                "error": error,
                "feishu": {
                    "event_id": event_id,
                    "incoming_message_id": incoming_message_id,
                    "reply_message_id": reply_message_id,
                    "chat_id": chat_id,
                },
            },
        }
        self._append(self.result_file, record)
        self._append_events(record)
        self._update_session(chat_id, "", event_id, incoming_message_id, reply_message_id)

    def existing_request_ids(self) -> set[str]:
        return self._existing_request_ids(self.request_file)

    def existing_result_request_ids(self) -> set[str]:
        return self._existing_request_ids(self.result_file)

    def _append_events(self, record: dict[str, Any]) -> None:
        self._append(self.events_file, record)

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            self._lock(handle)
            try:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                self._unlock(handle)

    def _event_record_id(self, source: str, event_id: str, ts: Optional[str]) -> str:
        stamp = (
            (ts or iso_utc())
            .replace("-", "")
            .replace(":", "")
            .replace("Z", "")
            .replace("T", "_")
            .replace(".", "")
        )
        safe_event_id = "".join(ch if ch.isalnum() else "_" for ch in event_id)[:16] or "unknown"
        return f"evt_{stamp}_{source}_{safe_event_id}"

    def _request_record_id(self, event_id: str) -> str:
        return self._request_record_id_static(event_id)

    @staticmethod
    def _request_record_id_static(event_id: str) -> str:
        safe_event_id = "".join(ch if ch.isalnum() else "_" for ch in event_id) or "unknown"
        return f"req_feishu_{safe_event_id}"

    def _read_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return self._empty_state()
        try:
            state = self._read_json(self.state_file)
        except json.JSONDecodeError:
            return self._empty_state()
        return self._normalize_state(state)

    def _empty_state(self) -> dict[str, Any]:
        return {"version": 1, "updated_at": None, "projects": {}}

    def _normalize_state(self, state: dict[str, Any]) -> dict[str, Any]:
        normalized = self._empty_state()
        if isinstance(state, dict):
            normalized["version"] = state.get("version", 1)
            normalized["updated_at"] = state.get("updated_at")
            if isinstance(state.get("projects"), dict):
                normalized["projects"] = state["projects"]

            # Migrate the earlier Feishu-only sessions shape into the same
            # projects -> agents schema used by the root agent-bridge.
            legacy_sessions = state.get("sessions")
            if isinstance(legacy_sessions, dict):
                for chat_id, legacy in legacy_sessions.items():
                    if not isinstance(legacy, dict):
                        continue
                    project = self._project_entry(normalized)
                    agents = project.setdefault("agents", {})
                    updated_at = legacy.get("updated_at") or normalized["updated_at"] or iso_utc()
                    agents["feishu"] = {
                        "session": chat_id,
                        "identity": legacy.get("feishu_sender_id"),
                        "updated_at": updated_at,
                        "meta": {
                            "bridge": "feishu-codex",
                            "feishu_chat_id": chat_id,
                            "last_event_id": legacy.get("last_event_id"),
                            "last_incoming_message_id": legacy.get("last_incoming_message_id"),
                            "last_reply_message_id": legacy.get("last_reply_message_id"),
                        },
                    }
                    agents.setdefault(
                        "codex",
                        {
                            "session": legacy.get("codex") or "codex-cli",
                            "identity": "codex-cli",
                            "updated_at": updated_at,
                            "meta": {"bridge": "feishu-codex"},
                        },
                    )
        return normalized

    def _write_state(self, state: dict[str, Any]) -> None:
        self._write_json(self.state_file, state)

    def _update_session(
        self,
        chat_id: str,
        sender_id: str,
        event_id: str,
        incoming_message_id: Optional[str],
        reply_message_id: Optional[str],
    ) -> None:
        if not chat_id:
            return
        state = self._read_state()
        project = self._project_entry(state)
        agents = project.setdefault("agents", {})
        updated_at = iso_utc()
        previous_feishu = agents.get("feishu", {})
        agents["feishu"] = {
            "session": chat_id,
            "identity": sender_id or previous_feishu.get("identity"),
            "updated_at": updated_at,
            "meta": {
                "bridge": "feishu-codex",
                "feishu_chat_id": chat_id,
                "last_event_id": event_id,
                "last_incoming_message_id": incoming_message_id,
                "last_reply_message_id": reply_message_id
                or (previous_feishu.get("meta") or {}).get("last_reply_message_id"),
            },
        }
        agents["codex"] = {
            "session": "codex-cli",
            "identity": "codex-cli",
            "updated_at": updated_at,
            "meta": {"bridge": "feishu-codex"},
        }
        state["updated_at"] = updated_at
        self._write_state(state)

    def _project_entry(self, state: dict[str, Any]) -> dict[str, Any]:
        projects = state.setdefault("projects", {})
        project_key = str(ROOT)
        project = projects.setdefault(project_key, {"path": project_key, "agents": {}})
        project["path"] = project_key
        project.setdefault("agents", {})
        return project

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            self._lock(handle)
            try:
                raw = handle.read().strip()
            finally:
                self._unlock(handle)
        if not raw:
            return {}
        return json.loads(raw)

    def _existing_request_ids(self, path: Path) -> set[str]:
        if not path.exists():
            return set()
        ids: set[str] = set()
        with path.open("r", encoding="utf-8") as handle:
            self._lock(handle)
            try:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    request_id = event.get("request_id")
                    if isinstance(request_id, str):
                        ids.add(request_id)
                    meta = event.get("meta")
                    if isinstance(meta, dict):
                        feishu_meta = meta.get("feishu")
                        if isinstance(feishu_meta, dict) and isinstance(feishu_meta.get("event_id"), str):
                            ids.add(feishu_meta["event_id"])
            finally:
                self._unlock(handle)
        return ids

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            self._lock(handle)
            try:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                self._unlock(handle)

    def _lock(self, handle: Any) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock(self, handle: Any) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class FeishuClient:
    def __init__(self, lark_cli: list[str]) -> None:
        self.lark_cli = lark_cli

    def auth_status(self) -> dict[str, Any]:
        return run_json([*self.lark_cli, "auth", "status"])

    def auth_probe(self) -> dict[str, Any]:
        completed = subprocess.run(
            [*self.lark_cli, "auth", "status"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        raw = completed.stdout.strip()
        result: dict[str, Any] = {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "identity": None,
            "appId": None,
            "raw": raw,
        }
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                result["identity"] = payload.get("identity")
                result["appId"] = payload.get("appId")
                error = payload.get("error")
                if isinstance(error, dict):
                    result["error_type"] = error.get("type")
                    result["message"] = error.get("message")
                    result["hint"] = error.get("hint")
        return result

    def event_status(self) -> str:
        completed = subprocess.run(
            [*self.lark_cli, "event", "status"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        return completed.stdout.strip()

    def stop_event_bus(self, *, force: bool = False) -> None:
        cmd = [*self.lark_cli, "event", "stop"]
        if force:
            cmd.append("--force")
        subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def start_event_consumer(self) -> "EventConsumer":
        cmd = [
            *self.lark_cli,
            "event",
            "consume",
            "im.message.receive_v1",
            "--as",
            "bot",
            "--quiet",
        ]
        log(f"$ {' '.join(cmd)}")
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        stream = os.fdopen(master_fd, "r", encoding="utf-8", errors="replace", buffering=1)
        return EventConsumer(process=process, stream=stream)

    def reply_placeholder(self, incoming_message_id: str, text: str) -> str:
        content = json.dumps({"text": text}, ensure_ascii=False)
        data = json.dumps(
            {"msg_type": "text", "content": content},
            ensure_ascii=False,
        )
        result = run_json(
            [
                *self.lark_cli,
                "api",
                "POST",
                f"/open-apis/im/v1/messages/{incoming_message_id}/reply",
                "--data",
                data,
                "--as",
                "bot",
            ]
        )
        return result["data"]["message_id"]

    def send_placeholder(self, chat_id: str, text: str) -> str:
        content = json.dumps({"text": text}, ensure_ascii=False)
        data = json.dumps(
            {"receive_id": chat_id, "msg_type": "text", "content": content},
            ensure_ascii=False,
        )
        result = run_json(
            [
                *self.lark_cli,
                "api",
                "POST",
                "/open-apis/im/v1/messages",
                "--params",
                '{"receive_id_type":"chat_id"}',
                "--data",
                data,
                "--as",
                "bot",
            ]
        )
        return result["data"]["message_id"]

    def send_text(self, chat_id: str, text: str) -> str:
        content = json.dumps({"text": text}, ensure_ascii=False)
        data = json.dumps(
            {"receive_id": chat_id, "msg_type": "text", "content": content},
            ensure_ascii=False,
        )
        result = run_json(
            [
                *self.lark_cli,
                "api",
                "POST",
                "/open-apis/im/v1/messages",
                "--params",
                '{"receive_id_type":"chat_id"}',
                "--data",
                data,
                "--as",
                "bot",
            ]
        )
        return result["data"]["message_id"]

    def send_long_text(self, chat_id: str, text: str, *, title: str = "") -> list[str]:
        if len(text) <= FEISHU_TEXT_CHUNK_SIZE:
            return [self.send_text(chat_id, text)]

        chunks = [
            text[index : index + FEISHU_TEXT_CHUNK_SIZE]
            for index in range(0, len(text), FEISHU_TEXT_CHUNK_SIZE)
        ]
        message_ids: list[str] = []
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            prefix = f"{title}\n" if title else ""
            message_ids.append(self.send_text(chat_id, f"{prefix}[{index}/{total}]\n{chunk}"))
        return message_ids

    def update_text_message(self, message_id: str, text: str) -> None:
        content = json.dumps({"text": text}, ensure_ascii=False)
        data = json.dumps(
            {"msg_type": "text", "content": content},
            ensure_ascii=False,
        )
        run_json(
            [
                *self.lark_cli,
                "api",
                "PUT",
                f"/open-apis/im/v1/messages/{message_id}",
                "--data",
                data,
                "--as",
                "bot",
            ]
        )

    def get_message_text(self, message_id: str) -> str:
        result = run_json(
            [
                *self.lark_cli,
                "api",
                "GET",
                f"/open-apis/im/v1/messages/{message_id}",
                "--as",
                "bot",
            ]
        )
        data = result.get("data") or {}
        if isinstance(data.get("items"), list) and data["items"]:
            data = data["items"][0]
        body = data.get("body") or {}
        content = body.get("content", "")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                text = parsed.get("text")
                if isinstance(text, str):
                    return text
            except json.JSONDecodeError:
                return content
        return ""


@dataclass
class EventConsumer:
    process: subprocess.Popen[Any]
    stream: Any

    def readline(self) -> str:
        try:
            return self.stream.readline()
        except OSError as exc:
            if exc.errno == errno.EIO:
                return ""
            raise

    def poll(self) -> Optional[int]:
        return self.process.poll()

    @property
    def returncode(self) -> Optional[int]:
        return self.process.returncode

    def terminate(self) -> None:
        self.process.terminate()

    def wait(self, timeout: Optional[float] = None) -> int:
        return self.process.wait(timeout=timeout)

    def kill(self) -> None:
        self.process.kill()

    def close(self) -> None:
        try:
            self.stream.close()
        except Exception:
            pass


class FeishuAppender:
    def __init__(
        self,
        client: FeishuClient,
        message_id: str,
        *,
        chat_id: str,
        fallback_interval: float,
        interval: float,
        on_flush: Optional[Any] = None,
    ) -> None:
        self.client = client
        self.message_id = message_id
        self.chat_id = chat_id
        self.fallback_interval = fallback_interval
        self.interval = interval
        self.on_flush = on_flush
        self.buffer = ""
        self.last_flushed = ""
        self.last_flush = 0.0
        self.last_progress = ""
        self.last_fallback_send = time.monotonic()
        self.fallback_count = 0
        self.edit_failed = False
        self.final_sent = False

    def append(self, text: str, *, force: bool = False) -> None:
        if not text:
            return
        self.buffer += text
        if force and not self.edit_failed and not self.final_sent:
            self.flush()

    def flush(self) -> None:
        if self.edit_failed or self.final_sent or not self.buffer:
            return
        display_text = local_bridge_text(self.buffer)
        if display_text == self.last_flushed:
            return
        try:
            self.client.update_text_message(self.message_id, display_text)
        except Exception as exc:
            self.edit_failed = True
            log(f"message edit failed, stop streaming partial output until final result: {exc}")
            return
        self.last_flushed = display_text
        self.last_flush = time.monotonic()
        if self.on_flush:
            self.on_flush(self.buffer)

    def progress(self, text: str) -> None:
        if self.final_sent or not text or text == self.last_progress:
            return
        display_text = local_bridge_text(text)
        if not self.edit_failed:
            try:
                self.client.update_text_message(self.message_id, display_text)
            except Exception as exc:
                self.edit_failed = True
                log(f"progress edit failed, switch to concise progress messages: {exc}")
        if self.edit_failed:
            self._send_progress_message(text)
        self.last_progress = text

    def finalize(self, final_text: str) -> None:
        if self.final_sent:
            return
        final_text = (final_text or self.buffer).strip()
        if not final_text:
            return
        self.final_sent = True
        self.buffer = final_text
        display_text = local_bridge_text(final_text)
        if not self.edit_failed:
            try:
                self.client.update_text_message(self.message_id, display_text)
                self.last_flushed = display_text
                self.last_flush = time.monotonic()
                if self.on_flush:
                    self.on_flush(final_text)
                return
            except Exception as exc:
                self.edit_failed = True
                log(f"final edit failed, sending complete final result as new message: {exc}")
        message_ids = self.client.send_long_text(
            self.chat_id,
            display_text,
            title=local_bridge_title("[Codex Bridge 最终结果]"),
        )
        self.last_fallback_send = time.monotonic()
        if self.on_flush:
            self.on_flush(final_text)
        log(f"sent final complete result message(s) count={len(message_ids)}")

    def _should_flush(self, text: str) -> bool:
        if time.monotonic() - self.last_flush >= self.interval:
            return True
        return text.endswith(("\n", "。", "！", "？", ".", "!", "?"))

    def _send_progress_message(self, text: str) -> None:
        if self.fallback_interval <= 0:
            return
        if self.fallback_count >= 1:
            return
        if time.monotonic() - self.last_fallback_send < self.fallback_interval:
            return
        self.fallback_count += 1
        message_ids = self.client.send_long_text(
            self.chat_id,
            local_bridge_text(
                "[Codex Bridge 进度提示]\n"
                "飞书消息已超过可编辑时间，任务仍在执行；后续不再发送进度消息，完成后会发送最终结果并回调。"
            ),
            title=local_bridge_title("[Feishu Codex Bridge]"),
        )
        self.last_fallback_send = time.monotonic()
        log(f"sent single fallback progress notice count={len(message_ids)} progress_no={self.fallback_count}")


class FeishuDirectSender:
    def __init__(
        self,
        client: FeishuClient,
        chat_id: str,
        *,
        min_chars: int,
        on_flush: Optional[Any] = None,
    ) -> None:
        self.client = client
        self.chat_id = chat_id
        self.min_chars = min_chars
        self.on_flush = on_flush
        self.buffer = ""
        self.sent_text = ""
        self.last_message_id: Optional[str] = None

    def append(self, text: str, *, force: bool = False) -> None:
        if not text:
            return
        self.buffer += text
        if force or self._should_flush(text):
            self.flush()

    def flush(self) -> None:
        if not self.buffer.strip():
            return
        chunk = self.buffer
        self.buffer = ""
        self.last_message_id = self.client.send_text(self.chat_id, local_bridge_text(chunk))
        self.sent_text += chunk
        if self.on_flush:
            self.on_flush(self.sent_text)

    def progress(self, _text: str) -> None:
        return

    def _should_flush(self, text: str) -> bool:
        if len(self.buffer) >= self.min_chars:
            return True
        return text.endswith(("\n\n", "。", "！", "？", ".", "!", "?"))


def build_prompt(content: str, history: Iterable[sqlite3.Row]) -> str:
    history_lines = []
    for row in history:
        role = "用户" if row["role"] == "user" else "Codex"
        history_lines.append(f"{role}: {row['content']}")
    history_text = "\n".join(history_lines[-12:])
    return textwrap.dedent(
        f"""
        你是 Codex，正在通过 Feishu/Lark bot 和用户对话。
        请直接回答用户问题，保持简洁、准确、自然。不要提及内部桥接实现，除非用户正在询问 bridge 本身。

        最近对话：
        {history_text}

        当前用户消息：
        {content}
        """
    ).strip()


def is_codex_session_list_query(content: str) -> bool:
    text = content.strip()
    lowered = text.lower()
    if "会话" not in text and "session" not in lowered:
        return False
    query_markers = ("有哪些", "都有哪些", "所有", "列表", "列一下", "看下", "查看", "现在都")
    if any(marker in text for marker in query_markers):
        return True
    return "codex" in lowered and ("session" in lowered or "会话" in text)


def format_codex_session_time(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "unknown"
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        local = parsed.astimezone(dt.timezone(dt.timedelta(hours=8)))
        return local.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text[:16]


def build_codex_session_summary(limit: int = 12) -> str:
    index_path = Path.home() / ".codex" / "session_index.jsonl"
    archived_dir = Path.home() / ".codex" / "archived_sessions"
    if not index_path.exists():
        return (
            "我在本机没有找到 Codex 会话索引 `~/.codex/session_index.jsonl`。"
            "如果要查历史会话，需要先确认 Codex App 的会话索引位置。"
        )

    latest_by_id: dict[str, dict[str, Any]] = {}
    line_count = 0
    try:
        with index_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                line_count += 1
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                session_id = record.get("id")
                if not isinstance(session_id, str) or not session_id:
                    continue
                previous = latest_by_id.get(session_id)
                if previous is None or str(record.get("updated_at", "")) > str(previous.get("updated_at", "")):
                    latest_by_id[session_id] = record
    except OSError as exc:
        return f"读取 Codex 会话索引失败：{exc}"

    rows = sorted(latest_by_id.values(), key=lambda item: str(item.get("updated_at", "")), reverse=True)
    archived_count = 0
    if archived_dir.exists():
        try:
            archived_count = sum(1 for _ in archived_dir.glob("*.jsonl"))
        except OSError:
            archived_count = 0

    lines = [
        "我从本机 Codex 索引查到了这些会话：",
        "",
        f"- 索引记录：{line_count} 条",
        f"- 去重会话：{len(rows)} 个",
        f"- 归档会话文件：{archived_count} 个",
        "",
        "最近会话：",
    ]
    for index, record in enumerate(rows[:limit], start=1):
        session_id = str(record.get("id") or "")
        session_name = str(record.get("thread_name") or "(未命名)")
        lines.append(
            f"{index}. {format_codex_session_time(record.get('updated_at'))} | "
            f"{session_name} | {session_id[:8]}..."
        )
    if len(rows) > limit:
        lines.append("")
        lines.append(f"还有 {len(rows) - limit} 个更早的会话，可以继续让我按名称或时间筛。")
    lines.append("")
    lines.append("当前会话索引主要来自 `~/.codex/session_index.jsonl`，归档内容在 `~/.codex/archived_sessions`。")
    return "\n".join(lines)


def local_fast_response(content: str, timing: Optional[TimingTrace] = None) -> Optional[str]:
    if is_codex_session_list_query(content):
        if timing:
            timing.set_meta("response_source", "local_codex_session_index")
            timing.mark("local_fast_response_start")
        output = build_codex_session_summary()
        if timing:
            timing.mark("local_fast_response_done")
        return output
    return None


def finish_appender(appender: Any, text: str) -> None:
    if hasattr(appender, "finalize"):
        appender.finalize(text)
    else:
        appender.append(text, force=True)
        appender.flush()


def extract_delta(event: dict[str, Any]) -> str:
    event_type = str(event.get("type", ""))
    if "delta" not in event_type.lower():
        return ""
    for key in ("delta", "text", "content"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    item = event.get("item")
    if isinstance(item, dict):
        for key in ("delta", "text", "content"):
            value = item.get(key)
            if isinstance(value, str):
                return value
    return ""


def extract_completed_text(event: dict[str, Any]) -> str:
    if event.get("type") != "item.completed":
        return ""
    item = event.get("item")
    if not isinstance(item, dict):
        return ""
    if item.get("type") != "agent_message":
        return ""
    text = item.get("text")
    return text if isinstance(text, str) else ""


def extract_claude_delta(event: dict[str, Any]) -> str:
    if event.get("type") != "stream_event":
        return ""
    stream_event = event.get("event")
    if not isinstance(stream_event, dict):
        return ""
    if stream_event.get("type") != "content_block_delta":
        return ""
    delta = stream_event.get("delta")
    if not isinstance(delta, dict):
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


def extract_claude_completed_text(event: dict[str, Any]) -> str:
    if event.get("type") != "result":
        return ""
    text = event.get("result")
    return text if isinstance(text, str) else ""


def resolve_llm_executor(config: BridgeConfig, timing: Optional[TimingTrace] = None) -> str:
    mode = (config.llm_executor or LLM_EXECUTOR_CODEX).strip().lower()
    if mode in (LLM_EXECUTOR_CODEX, LLM_EXECUTOR_CLAUDE):
        if timing:
            timing.set_meta("llm_executor", mode)
        return mode
    if mode != LLM_EXECUTOR_ROUND_ROBIN:
        log(f"unknown llm_executor={mode}, fallback to codex")
        if timing:
            timing.set_meta("llm_executor_config_error", mode)
            timing.set_meta("llm_executor", LLM_EXECUTOR_CODEX)
        return LLM_EXECUTOR_CODEX

    state = read_json_file(EXECUTOR_STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    next_executor = str(state.get("next") or LLM_EXECUTOR_CODEX).strip().lower()
    if next_executor not in (LLM_EXECUTOR_CODEX, LLM_EXECUTOR_CLAUDE):
        next_executor = LLM_EXECUTOR_CODEX
    write_json_file(
        EXECUTOR_STATE_PATH,
        {
            "next": LLM_EXECUTOR_CLAUDE if next_executor == LLM_EXECUTOR_CODEX else LLM_EXECUTOR_CODEX,
            "last": next_executor,
            "updatedAt": iso_utc(),
        },
    )
    if timing:
        timing.set_meta("llm_executor_mode", mode)
        timing.set_meta("llm_executor", next_executor)
    return next_executor


def parse_executor_pool(pool: str) -> list[str]:
    workers: list[str] = []
    for raw_item in (pool or "").split(","):
        item = raw_item.strip().lower()
        if not item:
            continue
        if ":" in item:
            executor, raw_count = item.split(":", 1)
        else:
            executor, raw_count = item, "1"
        executor = executor.strip()
        if executor not in (LLM_EXECUTOR_CODEX, LLM_EXECUTOR_CLAUDE):
            raise ValueError(f"unsupported executor in pool: {executor}")
        try:
            count = int(raw_count.strip())
        except ValueError as exc:
            raise ValueError(f"invalid executor count in pool item: {item}") from exc
        if count < 0:
            raise ValueError(f"executor count must be >= 0 in pool item: {item}")
        workers.extend([executor] * count)
    if not workers:
        raise ValueError("executor pool must contain at least one worker")
    return workers


def config_for_executor(config: BridgeConfig, executor: str) -> BridgeConfig:
    return BridgeConfig(
        lark_cli=config.lark_cli,
        codex_cmd=config.codex_cmd,
        claude_cmd=config.claude_cmd,
        history_limit=config.history_limit,
        update_interval=config.update_interval,
        simulated_chunk_size=config.simulated_chunk_size,
        placeholder_text=config.placeholder_text,
        output_mode=config.output_mode,
        direct_chunk_min_chars=config.direct_chunk_min_chars,
        agent_bridge_root=config.agent_bridge_root,
        progress_interval=config.progress_interval,
        progress_enabled=config.progress_enabled,
        fallback_message_interval=config.fallback_message_interval,
        llm_executor=executor,
        claude_model=config.claude_model,
        claude_effort=config.claude_effort,
        codex_timeout=config.codex_timeout,
        codex_retries=config.codex_retries,
        raw_dify_audit_callback_enabled=config.raw_dify_audit_callback_enabled,
        enable_worker_pool=config.enable_worker_pool,
        executor_pool=config.executor_pool,
        worker_queue_size=config.worker_queue_size,
    )


def chunk_text(text: str, chunk_size: int) -> Iterable[str]:
    pending = ""
    for char in text:
        pending += char
        if char in "\n。！？.!?" or len(pending) >= chunk_size:
            yield pending
            pending = ""
    if pending:
        yield pending


def extract_first_json_object(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return None


class AiSemanticOutputError(ValueError):
    pass


def extract_strict_top_level_json_object(text: str) -> tuple[Optional[dict[str, Any]], str]:
    if not text or not text.strip():
        return None, "LLM输出为空"
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.S)
    if fenced:
        candidate = fenced.group(1).strip()
    if candidate.startswith(LOCAL_BRIDGE_LABEL):
        candidate = candidate[len(LOCAL_BRIDGE_LABEL):].strip()
    start = candidate.find("{")
    if start < 0:
        return None, "LLM输出不包含JSON对象"
    if candidate[:start].strip():
        return None, "LLM输出JSON对象前存在非JSON文本"
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(candidate[start:])
    except json.JSONDecodeError as exc:
        return None, f"LLM输出不完整或不是合法JSON: {exc.msg} at char {exc.pos}"
    if candidate[start + end:].strip():
        return None, "LLM输出JSON对象后存在非JSON文本"
    if not isinstance(value, dict):
        return None, "LLM输出顶层不是JSON对象"
    return value, ""


def validate_ai_semantic_output(output: str) -> tuple[Optional[dict[str, Any]], str]:
    semantic_result, error = extract_strict_top_level_json_object(output)
    if error:
        return None, error
    assert semantic_result is not None
    if not any(key in semantic_result for key in AI_SEMANTIC_RESULT_TOP_LEVEL_KEYS):
        return None, "LLM输出顶层JSON未包含任何可识别的语义结果字段"
    return semantic_result, ""


def require_ai_semantic_output(output: str) -> None:
    _, error = validate_ai_semantic_output(output)
    if error:
        raise AiSemanticOutputError(error)


def mark_ai_semantic_retry_compact_output(output: str, attempt: int, reason: Optional[Exception]) -> str:
    semantic_result, error = validate_ai_semantic_output(output)
    if error or semantic_result is None:
        return output
    reason_text = str(reason) if reason else ""
    semantic_result["_bridgeMeta"] = {
        "retryCompactOutput": True,
        "retryAttempt": attempt,
        "retryReason": reason_text,
        "note": "上一次未拿到完整最终结果，本次已压缩长证据/长示例以保证完整JSON回调",
    }
    table_semantic = semantic_result.get("tableSemantic")
    if isinstance(table_semantic, dict):
        evidence = table_semantic.get("evidence")
        if not isinstance(evidence, list):
            evidence = []
        marker = (
            "BRIDGE_RETRY_COMPACT_OUTPUT: 上一次未拿到完整最终结果，"
            f"本次第{attempt}次尝试已压缩长证据/长示例以保证完整JSON"
        )
        if marker not in evidence:
            evidence.append(marker)
        table_semantic["evidence"] = evidence
    return json.dumps(semantic_result, ensure_ascii=False, separators=(",", ":"))


def reset_appender_for_retry(appender: Any) -> None:
    for attr in ("buffer", "last_flushed"):
        if hasattr(appender, attr):
            setattr(appender, attr, "")


def extract_audit_meta(prompt: str, output: str) -> dict[str, Any]:
    parsed = extract_first_json_object(output) or {}
    callback_meta = parsed.get("callback") or parsed.get("callbackParams") or parsed.get("callbackPayload") or {}
    if not isinstance(callback_meta, dict):
        callback_meta = {}
    raw_table = (
        callback_meta.get("rawTableName")
        or callback_meta.get("raw_table")
        or parsed.get("raw_table")
        or parsed.get("rawTableName")
        or parsed.get("source_table")
        or parsed.get("sourceTable")
    )
    model_table = (
        callback_meta.get("modelTableName")
        or callback_meta.get("model_table")
        or parsed.get("model_table")
        or parsed.get("modelTableName")
        or parsed.get("target_table")
        or parsed.get("targetTable")
    )
    replaced_sql = parsed.get("replaced_sql") or parsed.get("replacedSql")

    table_mapping = re.search(
        r"\|\s*([a-zA-Z0-9_]+\.[a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)?)\s*\|\s*"
        r"([a-zA-Z0-9_]+\.[a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)?)\s*\|",
        prompt,
    )
    if table_mapping:
        raw_table = raw_table or table_mapping.group(1)
        model_table = model_table or table_mapping.group(2)

    def find_int(patterns: Iterable[str]) -> Optional[int]:
        for pattern in patterns:
            match = re.search(pattern, prompt, re.I)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    return None
        return None

    def find_text(patterns: Iterable[str]) -> Optional[str]:
        for pattern in patterns:
            match = re.search(pattern, prompt, re.I)
            if match:
                return match.group(1).strip().strip('"')
        return None

    def callback_int(name: str) -> Optional[int]:
        value = callback_meta.get(name)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def callback_text(*names: str) -> Optional[str]:
        for name in names:
            value = callback_meta.get(name)
            if value is not None:
                return str(value).strip()
        return None

    return {
        "auditLogId": callback_int("auditLogId") or find_int((r"审计ID\s*[:：]\s*(\d+)", r"auditLogId[\"'\s:：]+(\d+)")),
        "replaceAssetId": callback_int("replaceAssetId") or find_int((r"资产记录ID\s*[:：]\s*(\d+)", r"replaceAssetId[\"'\s:：]+(\d+)")),
        "docId": callback_int("docId") or find_int((r"docId[\"'\s:：]+(\d+)", r"文档ID\s*[:：]\s*(\d+)")),
        "sceneType": callback_int("sceneType") or find_int((r"sceneType\s*[:：]\s*(\d+)", r"sceneType[\"'\s:：]+(\d+)")),
        "rawTableName": raw_table,
        "modelTableName": model_table,
        "replacedSql": replaced_sql,
        "callbackUrl": callback_text("callbackUrl") or find_text((r"回调接口\s*:\s*POST\s+(\S+)", r"callbackUrl[\"'\s:：]+(https?://[^\"'\s,}]+)")),
        "tableOwnerName": callback_text("tableOwnerName", "ownerName") or find_text((r"tableOwnerName[\"'\s:：]+([^\"'\s,}]+)",)),
        "parsedJson": parsed or None,
    }


def persist_raw_dify_audit_result(
    *,
    event_id: str,
    chat_id: str,
    incoming_message_id: str,
    reply_message_id: Optional[str],
    prompt: str,
    output: str,
    status: str,
    callback_enabled: bool,
    error: str = "",
) -> None:
    try:
        meta = extract_audit_meta(prompt, output)
        callback = post_raw_dify_audit_callback(meta, event_id) if callback_enabled else {
            "status": "disabled",
            "reason": "raw dify audit callback switch is off",
        }
        record = {
            "schema": "feishu-codex.raw-dify-audit-result.v1",
            "ts": iso_utc(),
            "eventId": event_id,
            "requestId": AgentBridgeMirror._request_record_id_static(event_id),
            "chatId": chat_id,
            "incomingMessageId": incoming_message_id,
            "replyMessageId": reply_message_id,
            "status": status,
            "error": error,
            "auditLogId": meta.get("auditLogId"),
            "docId": meta.get("docId"),
            "rawTableName": meta.get("rawTableName"),
            "modelTableName": meta.get("modelTableName"),
            "replacedSql": meta.get("replacedSql"),
            "parsedJson": meta.get("parsedJson"),
            "callback": callback,
            "output": output,
        }
        append_json_line(RAW_DIFY_AUDIT_RESULTS_PATH, record)
        log(
            "raw dify audit result persisted "
            f"event={event_id} auditLogId={record.get('auditLogId')} "
            f"hasReplacedSql={bool(record.get('replacedSql'))}"
        )
    except Exception as exc:
        log(f"raw dify audit result persist failed for {event_id}: {exc}")


def post_raw_dify_audit_callback(meta: dict[str, Any], event_id: str) -> Optional[dict[str, Any]]:
    callback_url = meta.get("callbackUrl")
    if not callback_url:
        return None
    if not meta.get("auditLogId"):
        return {"status": "skipped", "reason": "missing auditLogId"}
    parsed_json = meta.get("parsedJson") if isinstance(meta.get("parsedJson"), dict) else {}
    replaced_sql = meta.get("replacedSql") or ""
    analysis = (
        parsed_json.get("analysis")
        or parsed_json.get("reason")
        or "Feishu bridge parsed Codex output and submitted callback."
    )

    payload = {
        "auditLogId": meta.get("auditLogId"),
        "replaceAssetId": meta.get("replaceAssetId"),
        "docId": meta.get("docId"),
        "rawTableName": meta.get("rawTableName"),
        "sceneType": meta.get("sceneType") or 1,
        "replacedSql": replaced_sql,
        "analysis": analysis,
        "source": "FEISHU_LARK_CLI",
        "bridgeRequestId": AgentBridgeMirror._request_record_id_static(event_id),
        "tableOwnerName": meta.get("tableOwnerName"),
        "errorMsg": "" if replaced_sql else analysis,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        str(callback_url),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            result = {
                "status": "success",
                "httpStatus": response.status,
                "body": response_body,
            }
            log(f"raw dify audit callback success event={event_id} httpStatus={response.status}")
            return result
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        log(f"raw dify audit callback http failed event={event_id} status={exc.code} body={response_body}")
        return {"status": "failed", "httpStatus": exc.code, "body": response_body}
    except Exception as exc:
        log(f"raw dify audit callback failed event={event_id}: {exc}")
        return {"status": "failed", "error": str(exc)}


def parse_ai_semantic_task(content: str) -> Optional[dict[str, Any]]:
    parsed = extract_first_json_object(content)
    if not isinstance(parsed, dict):
        return None
    if str(parsed.get("taskType") or "") != AI_SEMANTIC_TASK_TYPE:
        return None
    if not parsed.get("contextDownloadUrl") or not parsed.get("callbackUrl"):
        raise ValueError("AI semantic task missing contextDownloadUrl or callbackUrl")
    return parsed


def download_ai_semantic_context(task: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(str(task["contextDownloadUrl"]), method="GET")
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
        encoding = str(response.headers.get("Content-Encoding") or "").lower()
    payload = decode_ai_semantic_context_response(
        raw,
        encoding=encoding,
        url=str(task["contextDownloadUrl"]),
    )
    if not isinstance(payload, dict):
        raise ValueError("AI semantic context package is not a JSON object")
    return payload


def decode_ai_semantic_context_response(raw: bytes, *, encoding: str, url: str) -> Any:
    payload_bytes = raw
    if raw.startswith(b"\x1f\x8b"):
        payload_bytes = gzip.decompress(raw)
    elif "gzip" in encoding or url.endswith(".gz"):
        try:
            payload_bytes = gzip.decompress(raw)
        except OSError:
            payload_bytes = raw
    decoded = payload_bytes.decode("utf-8")
    payload = json.loads(decoded)
    if not isinstance(payload, str):
        return payload
    text_payload = payload.strip()
    if text_payload.startswith("{"):
        return json.loads(text_payload)
    try:
        binary_payload = base64.b64decode(text_payload)
    except Exception:
        return payload
    if binary_payload.startswith(b"\x1f\x8b"):
        binary_payload = gzip.decompress(binary_payload)
    return json.loads(binary_payload.decode("utf-8"))


def post_ai_semantic_callback(
    task: dict[str, Any],
    context_package: Optional[dict[str, Any]],
    output: str,
    *,
    error: str = "",
) -> dict[str, Any]:
    callback_url = str(
        (context_package or {}).get("callbackUrl")
        or task.get("callbackUrl")
        or ""
    )
    if not callback_url:
        return {"status": "skipped", "reason": "missing callbackUrl"}
    semantic_result, validation_error = validate_ai_semantic_output(output) if not error else (None, "")
    semantic_json = json.dumps(semantic_result, ensure_ascii=False, separators=(",", ":")) if semantic_result else ""
    callback_error = error or validation_error
    if not callback_error and not semantic_json:
        callback_error = "LLM输出不包含可回调的语义JSON"
    payload = {
        "assetKey": task.get("assetKey") or (context_package or {}).get("assetKey"),
        "batchId": task.get("batchId") or (context_package or {}).get("batchId"),
        "rawTableName": task.get("assetKey") or (context_package or {}).get("assetKey"),
        "sceneType": task.get("batchId") or (context_package or {}).get("batchId"),
        "bridgeRequestId": task.get("bridgeRequestId") or (context_package or {}).get("bridgeRequestId"),
        "semanticResultJson": semantic_json,
        "analysis": semantic_json,
        "outputText": output,
        "errorMsg": callback_error,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        callback_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            return {"status": "success", "httpStatus": response.status, "body": response_body}
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        return {"status": "failed", "httpStatus": exc.code, "body": response_body}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def build_ai_semantic_retry_prompt(prompt: str, attempt: int, last_error: Optional[Exception]) -> str:
    error_text = str(last_error) if last_error else ""
    retry_instruction = f"""

【自动重试补充指令】
上一次 AI 语义生成未成功完成，错误信息：{error_text}

本次是第 {attempt} 次尝试。请优先保证最终答案是一个完整、可解析、顶层合法的 JSON 对象，必须包含 tableSemantic 和 columnSemantics 等顶层语义字段。

为避免输出过长导致截断，请在不改变业务事实和字段覆盖范围的前提下主动压缩输出：
1. columnSemantics 必须覆盖本次字段范围内的每个字段，但每个字段的 evidence 控制在 1-2 条短证据。
2. tableSemantic、aiUsageSemantic、metricSemantics、glossarySemantics、relationSemantics 中的数组字段保留核心项即可，避免长篇展开。
3. sampleSql、recommendedQuestionPatterns、pendingQuestions 只保留最关键内容；如非必需可输出空数组。
4. evidence 只写来源摘要，不要粘贴长 SQL、长路径、长枚举或大段上下文。
5. 不要使用 Markdown，不要输出解释性正文，不要输出被截断的 JSON 片段。
6. 如果上下文信息过多，请先在内部摘要事实来源，再生成精简 JSON；最终只输出 JSON。
7. 请在顶层 JSON 增加 "_bridgeMeta": {{"retryCompactOutput": true, "retryAttempt": {attempt}}}，用于标记本次结果经过自动重试压缩；不要改变其它业务字段含义。
"""
    return prompt.rstrip() + "\n" + retry_instruction.strip() + "\n"


def run_ai_semantic_llm_with_retries(
    prompt: str,
    appender: Any,
    config: BridgeConfig,
    timing: Optional[TimingTrace] = None,
) -> str:
    attempts = max(2, int(config.codex_retries) + 1)
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        if timing:
            timing.set_meta("ai_semantic_llm_attempt", attempt)
            timing.set_meta("ai_semantic_llm_max_attempts", attempts)
        try:
            if attempt > 1:
                reset_appender_for_retry(appender)
                appender.progress(f"LLM Retry\n第 {attempt - 1} 次输出不完整，正在自动重试。")
                if timing:
                    timing.bump("feishu_progress_updates")
                    timing.mark("ai_semantic_retry_started")
            attempt_prompt = build_ai_semantic_retry_prompt(prompt, attempt, last_error) if attempt > 1 else prompt
            output = run_llm_streaming(
                attempt_prompt,
                appender,
                config,
                timing=timing,
                require_claude_final_result=True,
                output_validator=require_ai_semantic_output,
            )
            if attempt > 1:
                output = mark_ai_semantic_retry_compact_output(output, attempt, last_error)
            return output
        except Exception as exc:
            last_error = exc
            if timing:
                timing.set_meta(f"ai_semantic_llm_attempt_{attempt}_error", str(exc))
            log(f"ai semantic llm attempt {attempt}/{attempts} failed: {exc}")
            if attempt >= attempts:
                break
            time.sleep(min(2.0 * attempt, 5.0))
    assert last_error is not None
    raise last_error


def run_ai_semantic_task(
    task: dict[str, Any],
    appender: Any,
    config: BridgeConfig,
    timing: Optional[TimingTrace] = None,
) -> str:
    if timing:
        timing.mark("ai_semantic_context_download_start")
    context_package = download_ai_semantic_context(task)
    if timing:
        timing.mark("ai_semantic_context_download_done")
        timing.set_meta("ai_semantic_bridge_request_id", task.get("bridgeRequestId"))
        timing.set_meta("ai_semantic_asset_key", task.get("assetKey"))
    prompt = str(context_package.get("promptText") or "")
    if not prompt:
        request_payload = context_package.get("requestPayload")
        if isinstance(request_payload, dict):
            prompt = str(request_payload.get("prompt") or "")
    if not prompt:
        raise ValueError("AI semantic context package missing promptText")
    output = run_ai_semantic_llm_with_retries(prompt, appender, config, timing=timing)
    callback = post_ai_semantic_callback(task, context_package, output)
    if callback.get("status") != "success":
        raise RuntimeError(f"AI semantic callback failed: {callback}")
    record = {
        "schema": "feishu-codex.ai-semantic-result.v1",
        "ts": iso_utc(),
        "task": task,
        "callback": callback,
        "outputLength": len(output),
    }
    append_json_line(AI_SEMANTIC_RESULTS_PATH, record)
    log(
        "ai semantic result callback success "
        f"bridgeRequestId={task.get('bridgeRequestId')} assetKey={task.get('assetKey')}"
    )
    return output


def run_codex_streaming(
    prompt: str,
    appender: Any,
    config: BridgeConfig,
    timing: Optional[TimingTrace] = None,
    output_validator: Optional[Callable[[str], None]] = None,
) -> str:
    if os.environ.get("BRIDGE_FAKE_CODEX"):
        fake = os.environ["BRIDGE_FAKE_CODEX"]
        if timing:
            timing.mark("codex_start")
            timing.mark("codex_first_output")
            timing.set_meta("codex_streaming_source", "fake")
        for chunk in chunk_text(fake, config.simulated_chunk_size):
            if timing:
                timing.bump("codex_simulated_chunks")
            appender.append(chunk)
            time.sleep(min(config.update_interval, 0.25))
        if output_validator:
            output_validator(fake)
        appender.flush()
        if timing:
            timing.mark("codex_done")
            timing.mark("final_update")
        return fake

    cmd = [
        *config.codex_cmd,
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-",
    ]
    log(f"$ {' '.join(cmd[:6])} ...")
    if timing:
        timing.mark("codex_start")
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    assert proc.stdout is not None
    assert proc.stdin is not None
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    if timing:
        timing.mark("codex_process_started")

    completed_text = ""
    streamed_any = False
    started = time.monotonic()
    last_progress = time.monotonic()
    progress_count = 0
    raw_output_tail: list[str] = []

    while True:
        if config.codex_timeout > 0 and time.monotonic() - started >= config.codex_timeout:
            if timing:
                timing.mark("codex_timeout")
                timing.set_meta("codex_timeout_seconds", config.codex_timeout)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            raise TimeoutError(f"codex exec timed out after {int(config.codex_timeout)}s")

        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if not ready:
            line = ""
        else:
            line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                line = proc.stdout.readline()
                if not line:
                    break
                if timing:
                    timing.bump("codex_stdout_tail_drained")
                    timing.set_meta("codex_drained_stdout_after_exit", True)
            else:
                if (
                    config.progress_enabled
                    and not completed_text
                    and not streamed_any
                    and time.monotonic() - last_progress >= config.progress_interval
                ):
                    progress_count += 1
                    elapsed = int(time.monotonic() - (timing.started_monotonic if timing else last_progress))
                    if progress_count == 1:
                        progress_text = f"Codex Thinking\n已等待约 {elapsed}s，正在理解上下文。"
                    elif progress_count == 2:
                        progress_text = f"Codex Call Tools\n已等待约 {elapsed}s，正在读取工具/上下文。"
                    else:
                        progress_text = f"Codex Thinking\n已等待约 {elapsed}s，仍在处理，请稍候。"
                    try:
                        appender.progress(progress_text)
                        if timing:
                            timing.bump("feishu_progress_updates")
                            timing.mark("feishu_first_progress_update")
                            timing.set_meta("progress_updates", progress_count)
                    except Exception as exc:
                        log(f"progress update failed: {exc}")
                    last_progress = time.monotonic()
                time.sleep(0.05)
                continue
        line = line.strip()
        if not line:
            continue
        raw_output_tail.append(line)
        if len(raw_output_tail) > CODEX_FAILURE_TAIL_LINES * 2:
            raw_output_tail = raw_output_tail[-CODEX_FAILURE_TAIL_LINES:]
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if timing:
            timing.bump("codex_json_events")
            timing.mark("codex_first_json_event")

        delta = extract_delta(event)
        if delta:
            streamed_any = True
            completed_text += delta
            if timing:
                timing.bump("codex_delta_chunks")
                timing.mark("codex_first_output")
            appender.append(delta)
            continue

        final_text = extract_completed_text(event)
        if final_text:
            completed_text = final_text
            if timing:
                timing.bump("codex_completed_messages")
                timing.mark("codex_completed_text")

    exit_code = proc.wait()
    if timing:
        timing.mark("codex_process_exit")
    if exit_code != 0 and not completed_text:
        failure_tail = compact_codex_failure_tail(raw_output_tail)
        if failure_tail:
            raise RuntimeError(f"codex exec failed with exit {exit_code}: {failure_tail}")
        raise RuntimeError(f"codex exec failed with exit {exit_code}")

    if completed_text and not streamed_any:
        appender.buffer = completed_text
        if timing:
            timing.set_meta("codex_streaming_source", "final_message_chunked")
            timing.bump("codex_simulated_chunks")
            timing.mark("codex_first_output")
    elif timing:
        timing.set_meta("codex_streaming_source", "delta")

    if output_validator:
        output_validator(completed_text)
    if hasattr(appender, "finalize"):
        appender.finalize(completed_text)
    else:
        appender.flush()
    if timing:
        timing.mark("codex_done")
        timing.mark("final_update")
    return completed_text


def run_claude_streaming(
    prompt: str,
    appender: Any,
    config: BridgeConfig,
    timing: Optional[TimingTrace] = None,
    require_final_result: bool = False,
    output_validator: Optional[Callable[[str], None]] = None,
) -> str:
    cmd = [
        *config.claude_cmd,
        "-p",
        "--model",
        config.claude_model,
        "--effort",
        config.claude_effort,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        "",
        "--no-session-persistence",
        "--include-partial-messages",
        "--input-format",
        "text",
    ]
    log(f"$ {' '.join(cmd[:8])} ...")
    if timing:
        timing.mark("claude_start")
        timing.set_meta("claude_model", config.claude_model)
        timing.set_meta("claude_effort", config.claude_effort)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    assert proc.stdout is not None
    assert proc.stdin is not None
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    if timing:
        timing.mark("claude_process_started")

    completed_text = ""
    saw_final_result = False
    streamed_any = False
    started = time.monotonic()
    last_progress = time.monotonic()
    progress_count = 0
    raw_output_tail: list[str] = []

    while True:
        if config.codex_timeout > 0 and time.monotonic() - started >= config.codex_timeout:
            if timing:
                timing.mark("claude_timeout")
                timing.set_meta("claude_timeout_seconds", config.codex_timeout)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            raise TimeoutError(f"claude code timed out after {int(config.codex_timeout)}s")

        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if not ready:
            line = ""
        else:
            line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                line = proc.stdout.readline()
                if not line:
                    break
                if timing:
                    timing.bump("claude_stdout_tail_drained")
                    timing.set_meta("claude_drained_stdout_after_exit", True)
            else:
                if (
                    config.progress_enabled
                    and not completed_text
                    and not streamed_any
                    and time.monotonic() - last_progress >= config.progress_interval
                ):
                    progress_count += 1
                    elapsed = int(time.monotonic() - (timing.started_monotonic if timing else last_progress))
                    if progress_count == 1:
                        progress_text = f"Claude Thinking\n已等待约 {elapsed}s，正在理解上下文。"
                    elif progress_count == 2:
                        progress_text = f"Claude Running\n已等待约 {elapsed}s，正在生成语义结果。"
                    else:
                        progress_text = f"Claude Thinking\n已等待约 {elapsed}s，仍在处理，请稍候。"
                    try:
                        appender.progress(progress_text)
                        if timing:
                            timing.bump("feishu_progress_updates")
                            timing.mark("feishu_first_progress_update")
                            timing.set_meta("progress_updates", progress_count)
                    except Exception as exc:
                        log(f"progress update failed: {exc}")
                    last_progress = time.monotonic()
                time.sleep(0.05)
                continue
        line = line.strip()
        if not line:
            continue
        raw_output_tail.append(line)
        if len(raw_output_tail) > CODEX_FAILURE_TAIL_LINES * 2:
            raw_output_tail = raw_output_tail[-CODEX_FAILURE_TAIL_LINES:]
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if timing:
            timing.bump("codex_json_events")
            timing.mark("claude_first_json_event")

        delta = extract_claude_delta(event)
        if delta:
            streamed_any = True
            completed_text += delta
            if timing:
                timing.bump("codex_delta_chunks")
                timing.mark("codex_first_output")
            appender.append(delta)
            continue

        final_text = extract_claude_completed_text(event)
        if final_text:
            saw_final_result = True
            completed_text = final_text
            if timing:
                timing.bump("codex_completed_messages")
                timing.mark("claude_completed_text")

    exit_code = proc.wait()
    if timing:
        timing.mark("claude_process_exit")
    if exit_code != 0 and not completed_text:
        failure_tail = compact_codex_failure_tail(raw_output_tail)
        if failure_tail:
            raise RuntimeError(f"claude code failed with exit {exit_code}: {failure_tail}")
        raise RuntimeError(f"claude code failed with exit {exit_code}")
    if require_final_result and not saw_final_result:
        if timing:
            timing.set_meta("claude_missing_final_result", True)
        raise RuntimeError("Claude Code 未返回最终 result 事件，疑似输出被截断")

    if completed_text and not streamed_any:
        appender.buffer = completed_text
        if timing:
            timing.set_meta("codex_streaming_source", "claude_final_message_chunked")
            timing.bump("codex_simulated_chunks")
            timing.mark("codex_first_output")
    elif timing:
        timing.set_meta("codex_streaming_source", "claude_delta")

    if output_validator:
        output_validator(completed_text)
    if hasattr(appender, "finalize"):
        appender.finalize(completed_text)
    else:
        appender.flush()
    if timing:
        timing.mark("codex_done")
        timing.mark("final_update")
    return completed_text


def run_llm_streaming(
    prompt: str,
    appender: Any,
    config: BridgeConfig,
    timing: Optional[TimingTrace] = None,
    require_claude_final_result: bool = False,
    output_validator: Optional[Callable[[str], None]] = None,
) -> str:
    executor = resolve_llm_executor(config, timing=timing)
    if executor == LLM_EXECUTOR_CLAUDE:
        return run_claude_streaming(
            prompt,
            appender,
            config,
            timing=timing,
            require_final_result=require_claude_final_result,
            output_validator=output_validator,
        )
    return run_codex_streaming(prompt, appender, config, timing=timing, output_validator=output_validator)


def run_codex_with_retries(
    prompt: str,
    appender: Any,
    config: BridgeConfig,
    timing: Optional[TimingTrace] = None,
) -> str:
    attempts = max(1, int(config.codex_retries) + 1)
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        if timing:
            timing.set_meta("codex_attempt", attempt)
            timing.set_meta("codex_max_attempts", attempts)
        try:
            if attempt > 1:
                appender.progress(f"Codex Retry\n第 {attempt - 1} 次执行失败，正在自动重试。")
                if timing:
                    timing.bump("feishu_progress_updates")
                    timing.mark("codex_retry_started")
            return run_codex_streaming(prompt, appender, config, timing=timing)
        except Exception as exc:
            last_error = exc
            if timing:
                timing.set_meta(f"codex_attempt_{attempt}_error", str(exc))
            log(f"codex attempt {attempt}/{attempts} failed: {exc}")
            if attempt >= attempts:
                break
            time.sleep(min(2.0 * attempt, 5.0))
    assert last_error is not None
    raise last_error


def run_llm_with_retries(
    prompt: str,
    appender: Any,
    config: BridgeConfig,
    timing: Optional[TimingTrace] = None,
) -> str:
    attempts = max(1, int(config.codex_retries) + 1)
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        if timing:
            timing.set_meta("llm_attempt", attempt)
            timing.set_meta("llm_max_attempts", attempts)
        try:
            if attempt > 1:
                appender.progress(f"LLM Retry\n第 {attempt - 1} 次执行失败，正在自动重试。")
                if timing:
                    timing.bump("feishu_progress_updates")
                    timing.mark("codex_retry_started")
            return run_llm_streaming(prompt, appender, config, timing=timing)
        except Exception as exc:
            last_error = exc
            if timing:
                timing.set_meta(f"llm_attempt_{attempt}_error", str(exc))
            log(f"llm attempt {attempt}/{attempts} failed: {exc}")
            if attempt >= attempts:
                break
            time.sleep(min(2.0 * attempt, 5.0))
    assert last_error is not None
    raise last_error


def mirror_request(mirror: Optional[AgentBridgeMirror], event: dict[str, Any]) -> None:
    if mirror is None:
        return
    try:
        mirror.record_request(event)
    except Exception as exc:
        log(f"agent-bridge request mirror failed: {exc}")


def mirror_result(
    mirror: Optional[AgentBridgeMirror],
    *,
    event_id: str,
    chat_id: str,
    incoming_message_id: str,
    reply_message_id: Optional[str],
    output: str,
    status: str,
    error: str = "",
) -> None:
    if mirror is None:
        return
    try:
        mirror.record_result(
            event_id=event_id,
            chat_id=chat_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            output=output,
            status=status,
            error=error,
        )
    except Exception as exc:
        log(f"agent-bridge result mirror failed: {exc}")


def enqueue_ai_semantic_event(
    event: dict[str, Any],
    *,
    state: State,
    feishu: FeishuClient,
    config: BridgeConfig,
    mirror: Optional[AgentBridgeMirror],
    task_queue: "queue.Queue[BridgeTask]",
) -> bool:
    event_id = str(event.get("event_id") or "")
    incoming_message_id = str(event.get("message_id") or event.get("id") or "")
    chat_id = str(event.get("chat_id") or "")
    content = str(event.get("content") or "")

    if not event_id or not incoming_message_id or not chat_id:
        log(f"skip incomplete event: {event}")
        return True

    try:
        ai_semantic_task = parse_ai_semantic_task(content)
    except Exception:
        return False
    if not ai_semantic_task:
        return False

    if not state.insert_event_once(event):
        log(f"skip duplicate event: {event_id}")
        return True

    timing = TimingTrace(event, output_mode=config.output_mode)
    timing.mark("event_accepted")
    timing.set_meta("llm_executor_mode", LLM_EXECUTOR_WORKER_POOL)
    timing.set_meta("worker_queue_size", config.worker_queue_size)
    log(f"worker_pool received {event_id}: {content}")
    mirror_request(mirror, event)
    timing.mark("request_mirrored")
    state.add_message(
        chat_id=chat_id,
        role="user",
        content=content,
        feishu_message_id=incoming_message_id,
        event_id=event_id,
    )
    timing.mark("sqlite_user_message_saved")

    reply_message_id: Optional[str] = None
    try:
        timing.mark("placeholder_start")
        reply_message_id = feishu.reply_placeholder(
            incoming_message_id,
            local_bridge_text(config.placeholder_text),
        )
        timing.set_meta("placeholder_method", "reply")
        timing.mark("placeholder_sent")
    except Exception as exc:
        timing.mark("placeholder_reply_failed")
        log(f"reply placeholder failed, falling back to chat send: {exc}")
        reply_message_id = feishu.send_placeholder(chat_id, local_bridge_text(config.placeholder_text))
        timing.set_meta("placeholder_method", "chat_send_fallback")
        timing.mark("placeholder_sent")

    state.mark_response(
        event_id=event_id,
        incoming_message_id=incoming_message_id,
        reply_message_id=reply_message_id,
        chat_id=chat_id,
        status="queued",
    )
    timing.mark("sqlite_response_queued_saved")

    try:
        task_queue.put(
            BridgeTask(
                event=event,
                event_id=event_id,
                incoming_message_id=incoming_message_id,
                reply_message_id=reply_message_id or "",
                chat_id=chat_id,
                content=content,
                ai_semantic_task=ai_semantic_task,
                created_at=time.time(),
                timing=timing,
            ),
            block=False,
        )
    except queue.Full:
        error_text = "Codex bridge 处理失败：本地 worker 队列已满，请稍后重试。"
        log(f"worker_pool queue full event={event_id} size={task_queue.qsize()}")
        callback = post_ai_semantic_callback(ai_semantic_task, None, "", error="worker queue full")
        append_json_line(AI_SEMANTIC_RESULTS_PATH, {
            "schema": "feishu-codex.ai-semantic-result.v1",
            "ts": iso_utc(),
            "task": ai_semantic_task,
            "callback": callback,
            "status": "failed",
            "error": "worker queue full",
        })
        try:
            feishu.update_text_message(reply_message_id or "", error_text)
        except Exception as update_exc:
            log(f"failed to update queue-full error into Feishu message: {update_exc}")
        state.mark_response(
            event_id=event_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            chat_id=chat_id,
            status="failed",
            error="worker queue full",
        )
        mirror_result(
            mirror,
            event_id=event_id,
            chat_id=chat_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            output=error_text,
            status="failed",
            error="worker queue full",
        )
        timing.write(status="failed", reply_message_id=reply_message_id, output=error_text, error="worker queue full")
        return True

    timing.mark("worker_queued")
    timing.set_meta("queue_depth_after_enqueue", task_queue.qsize())
    log(
        "worker_pool queued "
        f"event={event_id} bridgeRequestId={ai_semantic_task.get('bridgeRequestId')} "
        f"assetKey={ai_semantic_task.get('assetKey')} queueDepth={task_queue.qsize()}"
    )
    return True


def process_bridge_task(
    task: BridgeTask,
    *,
    worker_id: str,
    worker_type: str,
    state: State,
    feishu: FeishuClient,
    config: BridgeConfig,
    mirror: Optional[AgentBridgeMirror],
) -> None:
    event_id = task.event_id
    incoming_message_id = task.incoming_message_id
    reply_message_id = task.reply_message_id
    chat_id = task.chat_id
    ai_semantic_task = task.ai_semantic_task
    timing = task.timing
    timing.set_meta("worker_id", worker_id)
    timing.set_meta("worker_type", worker_type)
    timing.set_meta("llm_executor", worker_type)
    timing.set_meta("queue_wait_ms", int((time.time() - task.created_at) * 1000))
    timing.mark("worker_start")
    log(
        "worker_pool start "
        f"worker={worker_id} executor={worker_type} event={event_id} "
        f"bridgeRequestId={ai_semantic_task.get('bridgeRequestId')} assetKey={ai_semantic_task.get('assetKey')}"
    )

    state.mark_response(
        event_id=event_id,
        incoming_message_id=incoming_message_id,
        reply_message_id=reply_message_id,
        chat_id=chat_id,
        status="running",
    )
    timing.mark("sqlite_response_running_saved")

    def mark_running(buffer: str) -> None:
        timing.bump("feishu_updates")
        timing.mark("feishu_first_update")
        timing.set_meta("last_update_chars", len(buffer))
        state.mark_response(
            event_id=event_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            chat_id=chat_id,
            status="running",
            output=buffer,
        )

    if config.output_mode == "direct_chunks":
        appender = FeishuDirectSender(
            feishu,
            chat_id,
            min_chars=config.direct_chunk_min_chars,
            on_flush=mark_running,
        )
    else:
        appender = FeishuAppender(
            feishu,
            reply_message_id,
            chat_id=chat_id,
            fallback_interval=config.fallback_message_interval,
            interval=config.update_interval,
            on_flush=mark_running,
        )

    try:
        timing.mark("ai_semantic_task_detected")
        output = run_ai_semantic_task(ai_semantic_task, appender, config, timing=timing)
        state.add_message(
            chat_id=chat_id,
            role="assistant",
            content=output,
            feishu_message_id=reply_message_id,
            event_id=event_id,
        )
        timing.mark("sqlite_assistant_message_saved")
        state.mark_response(
            event_id=event_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            chat_id=chat_id,
            status="done",
            output=output,
        )
        timing.mark("sqlite_response_done_saved")
        mirror_result(
            mirror,
            event_id=event_id,
            chat_id=chat_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            output=output,
            status="done",
        )
        timing.mark("result_mirrored")
        timing.mark("worker_done")
        timing.write(status="done", reply_message_id=reply_message_id, output=output)
        log(f"worker_pool done worker={worker_id} event={event_id}: reply={reply_message_id}")
    except Exception as exc:
        error_text = f"Codex bridge 处理失败：{exc}"
        log(
            "worker_pool failed "
            f"worker={worker_id} event={event_id} "
            f"bridgeRequestId={ai_semantic_task.get('bridgeRequestId')}: {exc}"
        )
        callback = post_ai_semantic_callback(ai_semantic_task, None, "", error=str(exc))
        append_json_line(AI_SEMANTIC_RESULTS_PATH, {
            "schema": "feishu-codex.ai-semantic-result.v1",
            "ts": iso_utc(),
            "task": ai_semantic_task,
            "callback": callback,
            "status": "failed",
            "error": str(exc),
            "worker": {
                "workerId": worker_id,
                "workerType": worker_type,
            },
        })
        try:
            finish_appender(appender, error_text)
        except Exception as update_exc:
            log(f"failed to update error into Feishu message: {update_exc}")
        state.mark_response(
            event_id=event_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            chat_id=chat_id,
            status="failed",
            error=str(exc),
        )
        timing.mark("sqlite_response_failed_saved")
        mirror_result(
            mirror,
            event_id=event_id,
            chat_id=chat_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            output=error_text,
            status="failed",
            error=str(exc),
        )
        timing.mark("result_mirrored")
        timing.mark("worker_failed")
        timing.write(
            status="failed",
            reply_message_id=reply_message_id,
            output=error_text,
            error=str(exc),
        )


def worker_loop(
    *,
    worker_id: str,
    worker_type: str,
    task_queue: "queue.Queue[BridgeTask]",
    stop_event: threading.Event,
    base_config: BridgeConfig,
    mirror: Optional[AgentBridgeMirror],
) -> None:
    state = State()
    feishu = FeishuClient(base_config.lark_cli)
    config = config_for_executor(base_config, worker_type)
    log(f"worker_pool worker started worker={worker_id} executor={worker_type}")
    while not stop_event.is_set() or not task_queue.empty():
        try:
            task = task_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            process_bridge_task(
                task,
                worker_id=worker_id,
                worker_type=worker_type,
                state=state,
                feishu=feishu,
                config=config,
                mirror=mirror,
            )
        except Exception as exc:
            log(f"worker_pool unexpected worker error worker={worker_id}: {exc}")
        finally:
            task_queue.task_done()
    log(f"worker_pool worker stopped worker={worker_id} executor={worker_type}")


def process_event(
    event: dict[str, Any],
    *,
    state: State,
    feishu: FeishuClient,
    config: BridgeConfig,
    mirror: Optional[AgentBridgeMirror],
) -> None:
    event_id = event.get("event_id")
    incoming_message_id = event.get("message_id") or event.get("id")
    chat_id = event.get("chat_id")
    content = event.get("content", "")

    if not event_id or not incoming_message_id or not chat_id:
        log(f"skip incomplete event: {event}")
        return
    if not state.insert_event_once(event):
        log(f"skip duplicate event: {event_id}")
        return

    timing = TimingTrace(event, output_mode=config.output_mode)
    timing.mark("event_accepted")
    log(f"received {event_id}: {content}")
    mirror_request(mirror, event)
    timing.mark("request_mirrored")
    state.add_message(
        chat_id=chat_id,
        role="user",
        content=content,
        feishu_message_id=incoming_message_id,
        event_id=event_id,
    )
    timing.mark("sqlite_user_message_saved")

    reply_message_id: Optional[str] = None
    try:
        timing.mark("placeholder_start")
        reply_message_id = feishu.reply_placeholder(
            incoming_message_id,
            local_bridge_text(config.placeholder_text),
        )
        timing.set_meta("placeholder_method", "reply")
        timing.mark("placeholder_sent")
    except Exception as exc:
        timing.mark("placeholder_reply_failed")
        log(f"reply placeholder failed, falling back to chat send: {exc}")
        reply_message_id = feishu.send_placeholder(chat_id, local_bridge_text(config.placeholder_text))
        timing.set_meta("placeholder_method", "chat_send_fallback")
        timing.mark("placeholder_sent")

    state.mark_response(
        event_id=event_id,
        incoming_message_id=incoming_message_id,
        reply_message_id=reply_message_id,
        chat_id=chat_id,
        status="running",
    )
    timing.mark("sqlite_response_running_saved")

    def mark_running(buffer: str) -> None:
        timing.bump("feishu_updates")
        timing.mark("feishu_first_update")
        timing.set_meta("last_update_chars", len(buffer))
        state.mark_response(
            event_id=event_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            chat_id=chat_id,
            status="running",
            output=buffer,
        )

    if config.output_mode == "direct_chunks":
        appender = FeishuDirectSender(
            feishu,
            chat_id,
            min_chars=config.direct_chunk_min_chars,
            on_flush=mark_running,
        )
    else:
        appender = FeishuAppender(
            feishu,
            reply_message_id,
            chat_id=chat_id,
            fallback_interval=config.fallback_message_interval,
            interval=config.update_interval,
            on_flush=mark_running,
        )
    ai_semantic_task: Optional[dict[str, Any]] = None
    try:
        ai_semantic_task = parse_ai_semantic_task(content)
        if ai_semantic_task:
            timing.mark("ai_semantic_task_detected")
            output = run_ai_semantic_task(ai_semantic_task, appender, config, timing=timing)
        else:
            prompt = build_prompt(content, state.recent_history(chat_id, config.history_limit))
            timing.mark("prompt_built")
            output = local_fast_response(content, timing=timing)
            if output is not None:
                finish_appender(appender, output)
            else:
                output = run_llm_with_retries(prompt, appender, config, timing=timing)
            persist_raw_dify_audit_result(
                event_id=event_id,
                chat_id=chat_id,
                incoming_message_id=incoming_message_id,
                reply_message_id=reply_message_id,
                prompt=content,
                output=output,
                status="done",
                callback_enabled=config.raw_dify_audit_callback_enabled,
            )
            timing.mark("raw_dify_result_persisted")
        state.add_message(
            chat_id=chat_id,
            role="assistant",
            content=output,
            feishu_message_id=reply_message_id,
            event_id=event_id,
        )
        timing.mark("sqlite_assistant_message_saved")
        state.mark_response(
            event_id=event_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            chat_id=chat_id,
            status="done",
            output=output,
        )
        timing.mark("sqlite_response_done_saved")
        mirror_result(
            mirror,
            event_id=event_id,
            chat_id=chat_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            output=output,
            status="done",
        )
        timing.mark("result_mirrored")
        timing.write(status="done", reply_message_id=reply_message_id, output=output)
        log(f"completed {event_id}: reply={reply_message_id}")
    except Exception as exc:
        error_text = f"Codex bridge 处理失败：{exc}"
        log(error_text)
        if ai_semantic_task:
            callback = post_ai_semantic_callback(ai_semantic_task, None, "", error=str(exc))
            append_json_line(AI_SEMANTIC_RESULTS_PATH, {
                "schema": "feishu-codex.ai-semantic-result.v1",
                "ts": iso_utc(),
                "task": ai_semantic_task,
                "callback": callback,
                "status": "failed",
                "error": str(exc),
            })
        else:
            persist_raw_dify_audit_result(
                event_id=event_id,
                chat_id=chat_id,
                incoming_message_id=incoming_message_id,
                reply_message_id=reply_message_id,
                prompt=content,
                output=error_text,
                status="failed",
                callback_enabled=config.raw_dify_audit_callback_enabled,
                error=str(exc),
            )
        try:
            finish_appender(appender, error_text)
        except Exception as update_exc:
            log(f"failed to update error into Feishu message: {update_exc}")
        state.mark_response(
            event_id=event_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            chat_id=chat_id,
            status="failed",
            error=str(exc),
        )
        timing.mark("sqlite_response_failed_saved")
        mirror_result(
            mirror,
            event_id=event_id,
            chat_id=chat_id,
            incoming_message_id=incoming_message_id,
            reply_message_id=reply_message_id,
            output=error_text,
            status="failed",
            error=str(exc),
        )
        timing.mark("result_mirrored")
        timing.write(
            status="failed",
            reply_message_id=reply_message_id,
            output=error_text,
            error=str(exc),
        )


def run_foreground(args: argparse.Namespace) -> int:
    ensure_var_dir()
    state = State()
    lark_cli = resolve_lark_cli()
    codex_cmd = os.environ.get("CODEX_CMD", "codex").split()
    claude_cmd = os.environ.get("CLAUDE_CMD", "claude").split()
    config = BridgeConfig(
        lark_cli=lark_cli,
        codex_cmd=codex_cmd,
        claude_cmd=claude_cmd,
        update_interval=args.update_interval,
        simulated_chunk_size=args.chunk_size,
        placeholder_text=args.placeholder,
        output_mode=args.output_mode,
        direct_chunk_min_chars=args.direct_chunk_min_chars,
        agent_bridge_root=Path(args.agent_bridge_root).expanduser(),
        progress_interval=args.progress_interval,
        progress_enabled=not args.disable_progress,
        fallback_message_interval=args.fallback_message_interval,
        llm_executor=args.llm_executor,
        claude_model=args.claude_model,
        claude_effort=args.claude_effort,
        codex_timeout=args.codex_timeout,
        codex_retries=args.codex_retries,
        raw_dify_audit_callback_enabled=args.enable_raw_dify_audit_callback,
        enable_worker_pool=args.enable_worker_pool,
        executor_pool=args.executor_pool,
        worker_queue_size=args.worker_queue_size,
    )
    feishu = FeishuClient(config.lark_cli)
    mirror: Optional[AgentBridgeMirror] = AgentBridgeMirror(config.agent_bridge_root)
    try:
        mirror.ensure()
        log(f"agent-bridge mirror={mirror.base}")
    except Exception as exc:
        log(f"agent-bridge mirror disabled: {exc}")
        mirror = None
    consumer: Optional[EventConsumer] = None
    stopped = False
    worker_stop_event = threading.Event()
    task_queue: Optional["queue.Queue[BridgeTask]"] = None
    worker_threads: list[threading.Thread] = []

    if config.enable_worker_pool:
        workers = parse_executor_pool(config.executor_pool)
        task_queue = queue.Queue(maxsize=max(1, int(config.worker_queue_size)))
        worker_counts: dict[str, int] = {}
        for worker_type in workers:
            worker_counts[worker_type] = worker_counts.get(worker_type, 0) + 1
            worker_id = f"{worker_type}-{worker_counts[worker_type]}"
            thread = threading.Thread(
                target=worker_loop,
                kwargs={
                    "worker_id": worker_id,
                    "worker_type": worker_type,
                    "task_queue": task_queue,
                    "stop_event": worker_stop_event,
                    "base_config": config,
                    "mirror": mirror,
                },
                name=f"bridge-{worker_id}",
                daemon=True,
            )
            thread.start()
            worker_threads.append(thread)
        log(
            "worker_pool enabled "
            f"pool={config.executor_pool} workers={len(worker_threads)} queueSize={config.worker_queue_size}"
        )

    def handle_signal(signum: int, _frame: Any) -> None:
        nonlocal stopped
        log(f"received signal {signum}, shutting down")
        stopped = True
        worker_stop_event.set()
        if consumer and consumer.poll() is None:
            consumer.terminate()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    auth = feishu.auth_status()
    log(f"auth identity={auth.get('identity')} appId={auth.get('appId')}")
    while not stopped:
        consumer = feishu.start_event_consumer()
        line = consumer.readline()
        while not stopped:
            if not line:
                if consumer.poll() is not None:
                    log(f"event consumer exited with {consumer.returncode}")
                    break
                time.sleep(0.05)
                line = consumer.readline()
                continue
            line = line.strip()
            if not line:
                line = consumer.readline()
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log(f"event non-json: {line}")
                line = consumer.readline()
                continue
            if config.enable_worker_pool and task_queue is not None and enqueue_ai_semantic_event(
                event,
                state=state,
                feishu=feishu,
                config=config,
                mirror=mirror,
                task_queue=task_queue,
            ):
                line = consumer.readline()
                continue
            process_event(event, state=state, feishu=feishu, config=config, mirror=mirror)
            line = consumer.readline()
        consumer.close()
        if not stopped:
            time.sleep(2.0)
            continue
        if consumer and consumer.poll() is None:
            consumer.terminate()
            try:
                consumer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                consumer.kill()
            break

    if args.stop_event_bus_on_exit:
        feishu.stop_event_bus(force=True)
    worker_stop_event.set()
    for thread in worker_threads:
        thread.join(timeout=2.0)
    return 0


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pid_command(pid: int) -> str:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def is_bridge_pid(pid: int) -> bool:
    if not is_pid_alive(pid):
        return False
    command = pid_command(pid)
    script_path = str(Path(__file__).resolve())
    return script_path in command and " run" in command


def is_heartbeat_pid(pid: int) -> bool:
    if not is_pid_alive(pid):
        return False
    command = pid_command(pid)
    script_path = str(Path(__file__).resolve())
    return script_path in command and " watch" in command


def start_background(args: argparse.Namespace) -> int:
    ensure_var_dir()
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
            if is_bridge_pid(pid):
                feishu = FeishuClient(resolve_lark_cli())
                health = build_healthcheck(feishu)
                if health["healthy"]:
                    print(f"bridge already running: pid {pid}")
                    return 0
                print(f"bridge pid {pid} is alive but unhealthy: {health['issues']}; restarting")
                stop_background(argparse.Namespace())
            if is_pid_alive(pid):
                print(f"stale bridge pid reused by another process: {pid}; starting a new bridge")
            else:
                print(f"removing stale bridge pid: {pid}")
            PID_PATH.unlink(missing_ok=True)
        except ValueError:
            print("removing invalid bridge pid file")
            PID_PATH.unlink(missing_ok=True)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--update-interval",
        str(args.update_interval),
        "--chunk-size",
        str(args.chunk_size),
        "--placeholder",
        args.placeholder,
        "--output-mode",
        args.output_mode,
        "--direct-chunk-min-chars",
        str(args.direct_chunk_min_chars),
        "--progress-interval",
        str(args.progress_interval),
        "--fallback-message-interval",
        str(args.fallback_message_interval),
        "--llm-executor",
        args.llm_executor,
        "--claude-model",
        args.claude_model,
        "--claude-effort",
        args.claude_effort,
        "--codex-timeout",
        str(args.codex_timeout),
        "--codex-retries",
        str(args.codex_retries),
        "--agent-bridge-root",
        args.agent_bridge_root,
    ]
    if args.enable_raw_dify_audit_callback:
        cmd.append("--enable-raw-dify-audit-callback")
    if args.enable_worker_pool:
        cmd.extend([
            "--enable-worker-pool",
            "--executor-pool",
            args.executor_pool,
            "--worker-queue-size",
            str(args.worker_queue_size),
        ])
    if args.disable_progress:
        cmd.append("--disable-progress")
    if args.stop_event_bus_on_exit:
        cmd.append("--stop-event-bus-on-exit")

    log_file = LOG_PATH.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    PID_PATH.write_text(str(proc.pid), encoding="utf-8")
    print(f"started bridge pid {proc.pid}")
    print(f"log: {LOG_PATH}")
    return 0


def stop_background(_args: argparse.Namespace) -> int:
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
        except OSError as exc:
            print(f"failed to read bridge pid file: {exc}")
            pid = None
        except ValueError:
            print("pid file is invalid")
            pid = None
        if pid is None:
            pass
        elif is_bridge_pid(pid):
            os.kill(pid, signal.SIGTERM)
            print(f"sent SIGTERM to bridge pid {pid}")
            for _ in range(30):
                if not is_pid_alive(pid):
                    break
                time.sleep(0.1)
            if is_pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
                print(f"sent SIGKILL to bridge pid {pid}")
        elif is_pid_alive(pid):
            print(f"pid file points to another live process, not killing it: {pid}")
        else:
            print(f"pid file exists but process is not running: {pid}")
        PID_PATH.unlink(missing_ok=True)
    else:
        print("no bridge pid file")

    feishu = FeishuClient(resolve_lark_cli())
    feishu.stop_event_bus(force=True)
    return 0


def status(_args: argparse.Namespace) -> int:
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
            state = "running" if is_bridge_pid(pid) else "stale"
            print(f"bridge pid: {pid} ({state})")
        except ValueError:
            print("bridge pid: invalid")
    else:
        print("bridge pid: not running")
    feishu = FeishuClient(resolve_lark_cli())
    print(feishu.event_status())
    if LOG_PATH.exists():
        print(f"log: {LOG_PATH}")
    return 0


def healthcheck(args: argparse.Namespace) -> int:
    feishu = FeishuClient(resolve_lark_cli())
    health = build_healthcheck(
        feishu,
        error_window_seconds=args.error_window_seconds,
        error_threshold=args.error_threshold,
    )
    if args.format == "json":
        print(json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"healthy: {health['healthy']}")
        for issue in health["issues"]:
            print(f"issue: {issue}")
        bridge = health["bridge"]
        bus = health["event_bus"]
        print(f"bridge pid: {bridge.get('pid')} ({'running' if bridge.get('running') else 'not running'})")
        print(
            "event bus: "
            f"{'running' if bus.get('bus_running') else 'not running'} "
            f"pid={bus.get('bus_pid')} active_consumers={bus.get('active_consumers')} "
            f"received={bus.get('received')} dropped={bus.get('dropped')}"
        )
    return 0 if health["healthy"] else 1


def _start_from_watch(args: argparse.Namespace) -> None:
    start_args = argparse.Namespace(
        update_interval=args.update_interval,
        chunk_size=args.chunk_size,
        placeholder=args.placeholder,
        output_mode=args.output_mode,
        direct_chunk_min_chars=args.direct_chunk_min_chars,
        progress_interval=args.progress_interval,
        fallback_message_interval=args.fallback_message_interval,
        llm_executor=args.llm_executor,
        claude_model=args.claude_model,
        claude_effort=args.claude_effort,
        codex_timeout=args.codex_timeout,
        codex_retries=args.codex_retries,
        enable_raw_dify_audit_callback=args.enable_raw_dify_audit_callback,
        enable_worker_pool=args.enable_worker_pool,
        executor_pool=args.executor_pool,
        worker_queue_size=args.worker_queue_size,
        disable_progress=args.disable_progress,
        stop_event_bus_on_exit=False,
        agent_bridge_root=args.agent_bridge_root,
    )
    start_background(start_args)


def _restart_from_watch(args: argparse.Namespace, health: dict[str, Any]) -> dict[str, Any]:
    record_watch("restart_begin", health)
    stop_background(argparse.Namespace())
    time.sleep(args.restart_delay)
    _start_from_watch(args)
    time.sleep(args.restart_delay)
    refreshed = build_healthcheck(
        FeishuClient(resolve_lark_cli()),
        error_window_seconds=args.error_window_seconds,
        error_threshold=args.error_threshold,
    )
    record_watch("restart_done", refreshed)
    return refreshed


def watch(args: argparse.Namespace) -> int:
    feishu = FeishuClient(resolve_lark_cli())
    while True:
        health = build_healthcheck(
            feishu,
            error_window_seconds=args.error_window_seconds,
            error_threshold=args.error_threshold,
        )
        if health["healthy"]:
            if args.once or args.verbose:
                record_watch("healthy", health)
            notify_long_running_responses(args, feishu, health)
        else:
            record_watch("unhealthy", health)
            if not args.no_restart:
                health = _restart_from_watch(args, health)
                feishu = FeishuClient(resolve_lark_cli())
                if health["healthy"]:
                    notify_long_running_responses(args, feishu, health)
        if args.once:
            return 0 if health["healthy"] else 1
        time.sleep(args.interval)


def heartbeat_start(args: argparse.Namespace) -> int:
    ensure_var_dir()
    if HEARTBEAT_PID_PATH.exists():
        try:
            pid = int(HEARTBEAT_PID_PATH.read_text().strip())
            if is_heartbeat_pid(pid):
                print(f"heartbeat already running: pid {pid}")
                return 0
            if is_pid_alive(pid):
                print(f"heartbeat pid points to another live process, not killing it: {pid}")
            else:
                print(f"removing stale heartbeat pid: {pid}")
            HEARTBEAT_PID_PATH.unlink(missing_ok=True)
        except ValueError:
            print("removing invalid heartbeat pid file")
            HEARTBEAT_PID_PATH.unlink(missing_ok=True)

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "watch",
        "--interval",
        str(args.interval),
        "--restart-delay",
        str(args.restart_delay),
        "--running-heartbeat-after",
        str(args.running_heartbeat_after),
        "--running-heartbeat-interval",
        str(args.running_heartbeat_interval),
        "--error-window-seconds",
        str(args.error_window_seconds),
        "--error-threshold",
        str(args.error_threshold),
        "--llm-executor",
        args.llm_executor,
        "--claude-model",
        args.claude_model,
        "--claude-effort",
        args.claude_effort,
        "--codex-timeout",
        str(args.codex_timeout),
        "--codex-retries",
        str(args.codex_retries),
    ]
    if args.no_restart:
        cmd.append("--no-restart")
    if args.verbose:
        cmd.append("--verbose")
    if args.enable_worker_pool:
        cmd.extend([
            "--enable-worker-pool",
            "--executor-pool",
            args.executor_pool,
            "--worker-queue-size",
            str(args.worker_queue_size),
        ])

    log_file = HEARTBEAT_LOG_PATH.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    HEARTBEAT_PID_PATH.write_text(str(proc.pid), encoding="utf-8")
    print(f"started heartbeat pid {proc.pid}")
    print(f"log: {HEARTBEAT_LOG_PATH}")
    return 0


def heartbeat_stop(_args: argparse.Namespace) -> int:
    if HEARTBEAT_PID_PATH.exists():
        try:
            pid = int(HEARTBEAT_PID_PATH.read_text().strip())
            if is_heartbeat_pid(pid):
                os.kill(pid, signal.SIGTERM)
                print(f"sent SIGTERM to heartbeat pid {pid}")
                for _ in range(30):
                    if not is_pid_alive(pid):
                        break
                    time.sleep(0.1)
                if is_pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
                    print(f"sent SIGKILL to heartbeat pid {pid}")
            elif is_pid_alive(pid):
                print(f"heartbeat pid points to another live process, not killing it: {pid}")
            else:
                print(f"heartbeat pid file exists but process is not running: {pid}")
        except ValueError:
            print("heartbeat pid file is invalid")
        HEARTBEAT_PID_PATH.unlink(missing_ok=True)
    else:
        print("heartbeat pid: not running")
    return 0


def heartbeat_status(_args: argparse.Namespace) -> int:
    if HEARTBEAT_PID_PATH.exists():
        try:
            pid = int(HEARTBEAT_PID_PATH.read_text().strip())
            state = "running" if is_heartbeat_pid(pid) else "stale"
            print(f"heartbeat pid: {pid} ({state})")
        except ValueError:
            print("heartbeat pid: invalid")
    else:
        print("heartbeat pid: not running")
    if HEARTBEAT_LOG_PATH.exists():
        print(f"log: {HEARTBEAT_LOG_PATH}")
    return 0


def append_existing(args: argparse.Namespace) -> int:
    feishu = FeishuClient(resolve_lark_cli())
    existing = args.existing_text
    if existing is None:
        existing = feishu.get_message_text(args.message_id)
    if not existing:
        print(
            "warning: could not read existing text; pass --existing-text to avoid replacing it",
            file=sys.stderr,
        )
    separator = "" if not existing or existing.endswith(("\n", " ")) else "\n"
    next_text = f"{existing}{separator}{args.text}"
    try:
        feishu.update_text_message(args.message_id, next_text)
    except RuntimeError as exc:
        if "230075" in str(exc):
            print(
                "append failed: Feishu/Lark says this message is outside the editable time window "
                "(230075). Send a new continuation message instead.",
                file=sys.stderr,
            )
        raise
    print(f"appended {len(args.text)} chars to {args.message_id}")
    return 0


def sync_agent_bridge(args: argparse.Namespace) -> int:
    state = State()
    mirror = AgentBridgeMirror(Path(args.agent_bridge_root).expanduser())
    mirror.ensure()

    existing_requests = mirror.existing_request_ids()
    existing_results = mirror.existing_result_request_ids()
    request_count = 0
    result_count = 0

    for row in state.all_events():
        event_id = row["event_id"]
        if not event_id or event_id in existing_requests:
            continue
        try:
            event = json.loads(row["raw_json"])
        except json.JSONDecodeError:
            event = {}
        event.setdefault("event_id", event_id)
        event.setdefault("message_id", row["message_id"])
        event.setdefault("chat_id", row["chat_id"])
        event.setdefault("sender_id", row["sender_id"])
        event.setdefault("content", row["content"] or "")
        mirror.record_request(event, ts=iso_utc_from_ms(row["received_at"]))
        existing_requests.add(event_id)
        request_count += 1

    for row in state.all_responses():
        event_id = row["event_id"]
        if not event_id or event_id in existing_results:
            continue
        output = row["output"] or ""
        error = row["error"] or ""
        if not output and error:
            output = f"Codex bridge 处理失败：{error}"
        mirror.record_result(
            event_id=event_id,
            chat_id=row["chat_id"] or "",
            incoming_message_id=row["incoming_message_id"] or "",
            reply_message_id=row["reply_message_id"],
            output=output,
            status=row["status"] or "unknown",
            error=error,
            ts=iso_utc_from_ms(row["updated_at"]),
        )
        existing_results.add(event_id)
        result_count += 1

    print(f"synced requests={request_count} results={result_count} into {mirror.base}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Feishu/Lark CLI <-> Codex bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--update-interval", type=float, default=0.6)
        p.add_argument("--chunk-size", type=int, default=80)
        p.add_argument("--placeholder", default="Codex Thinking\n已收到，准备处理。")
        p.add_argument(
            "--output-mode",
            choices=("edit_append", "direct_chunks"),
            default="edit_append",
        )
        p.add_argument("--direct-chunk-min-chars", type=int, default=80)
        p.add_argument("--progress-interval", type=float, default=30.0)
        p.add_argument("--fallback-message-interval", type=float, default=60.0)
        p.add_argument(
            "--llm-executor",
            choices=(LLM_EXECUTOR_CODEX, LLM_EXECUTOR_CLAUDE, LLM_EXECUTOR_ROUND_ROBIN),
            default=os.environ.get("BRIDGE_LLM_EXECUTOR", LLM_EXECUTOR_CODEX),
            help="local model executor; default keeps the existing codex path",
        )
        p.add_argument(
            "--claude-model",
            default=os.environ.get("BRIDGE_CLAUDE_MODEL", "claude-opus-4-8"),
            help="Claude Code model used when --llm-executor selects claude",
        )
        p.add_argument(
            "--claude-effort",
            default=os.environ.get("BRIDGE_CLAUDE_EFFORT", "high"),
            help="Claude Code effort used when --llm-executor selects claude",
        )
        p.add_argument(
            "--codex-timeout",
            type=float,
            default=900.0,
            help="maximum seconds for one codex exec attempt; 0 disables the timeout",
        )
        p.add_argument(
            "--codex-retries",
            type=int,
            default=0,
            help="retry codex exec this many times after timeout or failure",
        )
        p.add_argument(
            "--enable-raw-dify-audit-callback",
            action="store_true",
            default=env_bool("ENABLE_RAW_DIFY_AUDIT_CALLBACK", False),
            help="POST raw Dify audit callback after Codex finishes; default is disabled",
        )
        p.add_argument(
            "--enable-worker-pool",
            action="store_true",
            default=env_bool("BRIDGE_ENABLE_WORKER_POOL", False),
            help="enable local AI semantic worker pool; disabled by default to keep the existing serial path",
        )
        p.add_argument(
            "--executor-pool",
            default=os.environ.get("BRIDGE_EXECUTOR_POOL", "codex:1,claude:1"),
            help="worker pool layout used only with --enable-worker-pool, e.g. codex:1,claude:1",
        )
        p.add_argument(
            "--worker-queue-size",
            type=int,
            default=int(os.environ.get("BRIDGE_WORKER_QUEUE_SIZE", "100")),
            help="max queued AI semantic tasks used only with --enable-worker-pool",
        )
        p.add_argument("--disable-progress", action="store_true")
        p.add_argument("--stop-event-bus-on-exit", action="store_true")
        p.add_argument(
            "--agent-bridge-root",
            default=str(default_agent_bridge_root()),
            help="agent-bridge root; Feishu data is isolated under <root>/feishu-codex",
        )

    run_p = sub.add_parser("run", help="run in foreground")
    add_common(run_p)
    run_p.set_defaults(func=run_foreground)

    start_p = sub.add_parser("start", help="start background process without installing a service")
    add_common(start_p)
    start_p.set_defaults(func=start_background)

    stop_p = sub.add_parser("stop", help="stop background process and event bus")
    stop_p.set_defaults(func=stop_background)

    status_p = sub.add_parser("status", help="show bridge and lark event status")
    status_p.set_defaults(func=status)

    health_p = sub.add_parser("healthcheck", help="check bridge process, event bus, consumers, and drops")
    health_p.add_argument("--format", choices=("text", "json"), default="text")
    health_p.add_argument("--error-window-seconds", type=int, default=DEFAULT_HEALTHCHECK_ERROR_WINDOW_SECONDS)
    health_p.add_argument("--error-threshold", type=int, default=DEFAULT_HEALTHCHECK_ERROR_THRESHOLD)
    health_p.set_defaults(func=healthcheck)

    watch_p = sub.add_parser("watch", help="watch health and recover the bridge when unhealthy")
    add_common(watch_p)
    watch_p.add_argument("--interval", type=float, default=30.0)
    watch_p.add_argument("--restart-delay", type=float, default=2.0)
    watch_p.add_argument(
        "--running-heartbeat-after",
        type=float,
        default=120.0,
        help="send a fallback heartbeat message when a request stays running longer than this many seconds",
    )
    watch_p.add_argument(
        "--running-heartbeat-interval",
        type=float,
        default=120.0,
        help="minimum seconds between fallback heartbeat messages for the same running request",
    )
    watch_p.add_argument("--once", action="store_true")
    watch_p.add_argument("--no-restart", action="store_true")
    watch_p.add_argument("--verbose", action="store_true")
    watch_p.add_argument("--error-window-seconds", type=int, default=DEFAULT_HEALTHCHECK_ERROR_WINDOW_SECONDS)
    watch_p.add_argument("--error-threshold", type=int, default=DEFAULT_HEALTHCHECK_ERROR_THRESHOLD)
    watch_p.set_defaults(func=watch)

    heartbeat_start_p = sub.add_parser("heartbeat-start", help="start background health/watch process")
    heartbeat_start_p.add_argument("--interval", type=float, default=30.0)
    heartbeat_start_p.add_argument("--restart-delay", type=float, default=2.0)
    heartbeat_start_p.add_argument("--running-heartbeat-after", type=float, default=120.0)
    heartbeat_start_p.add_argument("--running-heartbeat-interval", type=float, default=120.0)
    heartbeat_start_p.add_argument("--error-window-seconds", type=int, default=DEFAULT_HEALTHCHECK_ERROR_WINDOW_SECONDS)
    heartbeat_start_p.add_argument("--error-threshold", type=int, default=DEFAULT_HEALTHCHECK_ERROR_THRESHOLD)
    heartbeat_start_p.add_argument(
        "--llm-executor",
        choices=(LLM_EXECUTOR_CODEX, LLM_EXECUTOR_CLAUDE, LLM_EXECUTOR_ROUND_ROBIN),
        default=os.environ.get("BRIDGE_LLM_EXECUTOR", LLM_EXECUTOR_CODEX),
    )
    heartbeat_start_p.add_argument(
        "--claude-model",
        default=os.environ.get("BRIDGE_CLAUDE_MODEL", "claude-opus-4-8"),
    )
    heartbeat_start_p.add_argument(
        "--claude-effort",
        default=os.environ.get("BRIDGE_CLAUDE_EFFORT", "high"),
    )
    heartbeat_start_p.add_argument("--codex-timeout", type=float, default=900.0)
    heartbeat_start_p.add_argument("--codex-retries", type=int, default=0)
    heartbeat_start_p.add_argument(
        "--enable-worker-pool",
        action="store_true",
        default=env_bool("BRIDGE_ENABLE_WORKER_POOL", False),
    )
    heartbeat_start_p.add_argument(
        "--executor-pool",
        default=os.environ.get("BRIDGE_EXECUTOR_POOL", "codex:1,claude:1"),
    )
    heartbeat_start_p.add_argument(
        "--worker-queue-size",
        type=int,
        default=int(os.environ.get("BRIDGE_WORKER_QUEUE_SIZE", "100")),
    )
    heartbeat_start_p.add_argument("--no-restart", action="store_true")
    heartbeat_start_p.add_argument("--verbose", action="store_true")
    heartbeat_start_p.set_defaults(func=heartbeat_start)

    heartbeat_stop_p = sub.add_parser("heartbeat-stop", help="stop background health/watch process")
    heartbeat_stop_p.set_defaults(func=heartbeat_stop)

    heartbeat_status_p = sub.add_parser("heartbeat-status", help="show background heartbeat process")
    heartbeat_status_p.set_defaults(func=heartbeat_status)

    append_p = sub.add_parser("append", help="append text to an existing bot text message")
    append_p.add_argument("--message-id", required=True)
    append_p.add_argument("--text", required=True)
    append_p.add_argument("--existing-text")
    append_p.set_defaults(func=append_existing)

    sync_p = sub.add_parser("sync-agent-bridge", help="copy SQLite history into agent-bridge/feishu-codex")
    sync_p.add_argument(
        "--agent-bridge-root",
        default=str(default_agent_bridge_root()),
        help="agent-bridge root; only <root>/feishu-codex is written",
    )
    sync_p.set_defaults(func=sync_agent_bridge)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

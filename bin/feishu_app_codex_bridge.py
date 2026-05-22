#!/usr/bin/env python3
"""
Local-first Feishu/Lark app <-> Codex bridge.

This open-source version intentionally contains no user-specific data.
Runtime data is written under the repository's inbox/, logs/, state/, and var/
directories and is ignored by Git.
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import pty
import re
import select
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is Unix-only.
    fcntl = None


ROOT = Path(__file__).resolve().parents[1]
INBOX_DIR = ROOT / "inbox"
LOGS_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"
VAR_DIR = ROOT / "var"

DB_PATH = VAR_DIR / "bridge.sqlite3"
PID_PATH = VAR_DIR / "bridge.pid"
LOG_PATH = VAR_DIR / "bridge.log"

EVENTS_PATH = LOGS_DIR / "events.ndjson"
TIMINGS_PATH = LOGS_DIR / "timings.ndjson"
WATCH_PATH = LOGS_DIR / "watch.ndjson"

REQUEST_PATH = INBOX_DIR / "feishu-to-codex.ndjson"
RESULT_PATH = INBOX_DIR / "codex-to-feishu.ndjson"
CALLBACK_RESULT_PATH = INBOX_DIR / "callback-results.ndjson"
SESSION_MAP_PATH = STATE_DIR / "session-map.json"

PROTOCOL_VERSION = "agent-bridge.v1"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs() -> None:
    for path in (INBOX_DIR, LOGS_DIR, STATE_DIR, VAR_DIR):
        path.mkdir(parents=True, exist_ok=True)


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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)


def resolve_command(env_name: str, default: str) -> list[str]:
    value = os.environ.get(env_name)
    return shlex.split(value) if value else shlex.split(default)


def run_json(cmd: list[str], *, check: bool = True) -> dict[str, Any]:
    log(f"$ {' '.join(shlex.quote(part) for part in cmd)}")
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
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
    return json.loads(payload)


def request_id_for(event_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", event_id or str(now_ms()))
    return f"req_feishu_{safe}"


def event_record_id(prefix: str, event_id: str) -> str:
    return f"{prefix}_{request_id_for(event_id)}_{now_ms()}"


class State:
    def __init__(self, path: Path = DB_PATH) -> None:
        ensure_dirs()
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
                output TEXT,
                error TEXT,
                updated_at INTEGER NOT NULL
            );
            """
        )
        self.conn.commit()

    def insert_event_once(self, event: dict[str, Any]) -> bool:
        try:
            self.conn.execute(
                """
                INSERT INTO events(event_id, message_id, chat_id, sender_id, content, raw_json, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event.get("message_id"),
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

    def add_message(self, chat_id: str, role: str, content: str, *, feishu_message_id: str = "", event_id: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO messages(chat_id, role, content, feishu_message_id, event_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, role, content, feishu_message_id, event_id, now_ms()),
        )
        self.conn.commit()

    def recent_history(self, chat_id: str, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT role, content
            FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()[::-1]

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
            INSERT INTO responses(event_id, incoming_message_id, reply_message_id, chat_id, status, output, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
              reply_message_id=excluded.reply_message_id,
              status=excluded.status,
              output=excluded.output,
              error=excluded.error,
              updated_at=excluded.updated_at
            """,
            (event_id, incoming_message_id, reply_message_id, chat_id, status, output, error, now_ms()),
        )
        self.conn.commit()


@dataclass
class BridgeConfig:
    lark_cli: list[str]
    codex_cmd: list[str]
    history_limit: int = 12
    update_interval: float = 0.8
    progress_interval: float = 30.0
    fallback_message_interval: float = 60.0
    placeholder_text: str = "Codex Thinking\nReceived. Preparing to process."
    output_mode: str = "edit_append"
    direct_chunk_min_chars: int = 120
    callback_enabled: bool = False


class TimingTrace:
    def __init__(self, event: dict[str, Any], output_mode: str) -> None:
        self.started_monotonic = time.monotonic()
        self.started_at = iso_utc()
        self.event = event
        self.output_mode = output_mode
        self.marks: dict[str, int] = {}
        self.counters: dict[str, int] = {
            "codex_json_events": 0,
            "codex_delta_chunks": 0,
            "codex_completed_messages": 0,
            "feishu_updates": 0,
            "feishu_progress_updates": 0,
        }
        self.meta: dict[str, Any] = {}

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, int((time.monotonic() - self.started_monotonic) * 1000))

    def bump(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def set_meta(self, key: str, value: Any) -> None:
        self.meta[key] = value

    def write(self, *, status: str, reply_message_id: Optional[str], output: str = "", error: str = "") -> None:
        self.mark("total")
        event_id = str(self.event.get("event_id") or "")
        record = {
            "schema": "feishu-app-codex.timing.v1",
            "ts": iso_utc(),
            "started_at": self.started_at,
            "status": status,
            "event": {
                "event_id": event_id,
                "request_id": request_id_for(event_id),
                "incoming_message_id": self.event.get("message_id"),
                "reply_message_id": reply_message_id,
                "chat_id": self.event.get("chat_id"),
                "sender_id": self.event.get("sender_id"),
            },
            "output_mode": self.output_mode,
            "timings_ms": self.marks,
            "counters": self.counters,
            "output_chars": len(output or ""),
            "error": error,
            "meta": self.meta,
        }
        append_json_line(TIMINGS_PATH, record)


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
            result["bus_running"] = "running" in line
            marker = "PID "
            if marker in line:
                pid_text = line.split(marker, 1)[1].split(",", 1)[0].strip().strip(")")
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
                    pid = part.split("=", 1)[1]
                    if pid.isdigit():
                        consumer["pid"] = int(pid)
                elif part.startswith("im."):
                    consumer["event_key"] = part
                elif part.isdigit():
                    if "received" not in consumer:
                        consumer["received"] = int(part)
                    else:
                        consumer["dropped"] = int(part)
            result["consumers"].append(consumer)
    result["received"] = sum(int(item.get("received", 0)) for item in result["consumers"])
    result["dropped"] = sum(int(item.get("dropped", 0)) for item in result["consumers"])
    return result


class FeishuClient:
    def __init__(self, lark_cli: list[str]) -> None:
        self.lark_cli = lark_cli

    def auth_status(self) -> dict[str, Any]:
        return run_json([*self.lark_cli, "auth", "status"])

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

    def stop_event_bus(self, force: bool = True) -> None:
        cmd = [*self.lark_cli, "event", "stop"]
        if force:
            cmd.append("--force")
        subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)

    def start_event_consumer(self) -> "EventConsumer":
        cmd = [*self.lark_cli, "event", "consume", "im.message.receive_v1", "--as", "bot", "--quiet"]
        log(f"$ {' '.join(shlex.quote(part) for part in cmd)}")
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(cmd, cwd=ROOT, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True)
        os.close(slave_fd)
        stream = os.fdopen(master_fd, "r", encoding="utf-8", errors="replace", buffering=1)
        return EventConsumer(process=process, stream=stream)

    def reply_placeholder(self, incoming_message_id: str, text: str) -> str:
        content = json.dumps({"text": text}, ensure_ascii=False)
        data = json.dumps({"msg_type": "text", "content": content}, ensure_ascii=False)
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
        return str(result["data"]["message_id"])

    def send_text(self, chat_id: str, text: str) -> str:
        content = json.dumps({"text": text}, ensure_ascii=False)
        data = json.dumps({"receive_id": chat_id, "msg_type": "text", "content": content}, ensure_ascii=False)
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
        return str(result["data"]["message_id"])

    def update_text_message(self, message_id: str, text: str) -> None:
        content = json.dumps({"text": text}, ensure_ascii=False)
        data = json.dumps({"msg_type": "text", "content": content}, ensure_ascii=False)
        run_json([*self.lark_cli, "api", "PUT", f"/open-apis/im/v1/messages/{message_id}", "--data", data, "--as", "bot"])


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
        chat_id: str,
        *,
        interval: float,
        fallback_interval: float,
        on_flush: Optional[Any] = None,
    ) -> None:
        self.client = client
        self.message_id = message_id
        self.chat_id = chat_id
        self.interval = interval
        self.fallback_interval = fallback_interval
        self.on_flush = on_flush
        self.buffer = ""
        self.last_flushed = ""
        self.last_flush = 0.0
        self.last_progress = ""
        self.last_fallback = time.monotonic()
        self.edit_failed = False
        self.final_sent = False

    def append(self, text: str, *, force: bool = False) -> None:
        if not text:
            return
        self.buffer += text
        if force or self._should_flush(text):
            self.flush()

    def flush(self) -> None:
        if self.edit_failed or self.final_sent or not self.buffer or self.buffer == self.last_flushed:
            return
        try:
            self.client.update_text_message(self.message_id, self.buffer)
        except Exception as exc:
            self.edit_failed = True
            log(f"message edit failed; final output will be sent as fallback: {exc}")
            return
        self.last_flushed = self.buffer
        self.last_flush = time.monotonic()
        if self.on_flush:
            self.on_flush(self.buffer)

    def progress(self, text: str) -> None:
        if self.final_sent or not text or text == self.last_progress:
            return
        if not self.edit_failed:
            try:
                self.client.update_text_message(self.message_id, text)
            except Exception as exc:
                self.edit_failed = True
                log(f"progress edit failed; switching to fallback progress messages: {exc}")
        if self.edit_failed:
            self._send_fallback_progress(text)
        self.last_progress = text

    def finalize(self, final_text: str) -> None:
        if self.final_sent:
            return
        final_text = (final_text or self.buffer).strip()
        if not final_text:
            return
        self.final_sent = True
        if not self.edit_failed:
            try:
                self.client.update_text_message(self.message_id, final_text)
                if self.on_flush:
                    self.on_flush(final_text)
                return
            except Exception as exc:
                self.edit_failed = True
                log(f"final edit failed; sending final result as new message: {exc}")
        self.client.send_text(self.chat_id, final_text)
        if self.on_flush:
            self.on_flush(final_text)

    def _should_flush(self, text: str) -> bool:
        return time.monotonic() - self.last_flush >= self.interval or text.endswith(("\n", "。", "！", "？", ".", "!", "?"))

    def _send_fallback_progress(self, text: str) -> None:
        if self.fallback_interval <= 0:
            return
        if time.monotonic() - self.last_fallback < self.fallback_interval:
            return
        self.client.send_text(self.chat_id, f"[Bridge progress]\n{text}")
        self.last_fallback = time.monotonic()


def normalize_event(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    content = ""
    raw_content = message.get("content") or event.get("content") or ""
    if isinstance(raw_content, str):
        try:
            parsed = json.loads(raw_content)
            content = str(parsed.get("text") or raw_content)
        except json.JSONDecodeError:
            content = raw_content
    return {
        "event_id": str(payload.get("event_id") or payload.get("uuid") or event.get("event_id") or message.get("message_id") or now_ms()),
        "message_id": str(message.get("message_id") or event.get("message_id") or event.get("id") or ""),
        "chat_id": str(message.get("chat_id") or event.get("chat_id") or ""),
        "sender_id": str(
            sender.get("sender_id", {}).get("open_id")
            if isinstance(sender.get("sender_id"), dict)
            else sender.get("sender_id") or event.get("sender_id") or ""
        ),
        "chat_type": str(message.get("chat_type") or event.get("chat_type") or ""),
        "message_type": str(message.get("message_type") or event.get("message_type") or "text"),
        "content": content,
        "raw": payload,
    }


def build_prompt(content: str, history: Iterable[sqlite3.Row]) -> str:
    history_text = "\n".join(f"{row['role']}: {row['content']}" for row in history)
    return (
        "You are Codex, responding through a Feishu/Lark bot.\n"
        "Answer directly and concisely. If the user asks for a business callback workflow, "
        "return valid JSON with result, analysis, and callback fields when appropriate.\n\n"
        f"Recent conversation:\n{history_text}\n\n"
        f"Current user message:\n{content}"
    ).strip()


def extract_delta(event: dict[str, Any]) -> str:
    if "delta" not in str(event.get("type", "")).lower():
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
    if not isinstance(item, dict) or item.get("type") != "agent_message":
        return ""
    value = item.get("text")
    return value if isinstance(value, str) else ""


def parse_first_json_object(text: str) -> Optional[dict[str, Any]]:
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


def maybe_post_callback(*, output: str, request_id: str, enabled: bool) -> dict[str, Any]:
    parsed = parse_first_json_object(output) or {}
    callback = parsed.get("callback")
    if not isinstance(callback, dict):
        callback = {}
    callback_url = callback.get("callbackUrl") or callback.get("url")
    if not callback_url:
        result = {"status": "skipped", "reason": "no callbackUrl in output"}
        append_json_line(CALLBACK_RESULT_PATH, {"ts": iso_utc(), "request_id": request_id, **result})
        return result
    if not enabled:
        result = {"status": "disabled", "reason": "callback switch is off"}
        append_json_line(CALLBACK_RESULT_PATH, {"ts": iso_utc(), "request_id": request_id, "callbackUrl": callback_url, **result})
        return result

    payload = {
        "bridgeRequestId": request_id,
        "result": parsed.get("result") or parsed.get("replacedSql") or "",
        "analysis": parsed.get("analysis") or parsed.get("reason") or "",
        "callback": callback,
        "rawOutput": output,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        str(callback_url),
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            result = {"status": "success", "httpStatus": response.status, "body": response_body}
    except urllib.error.HTTPError as exc:
        result = {"status": "failed", "httpStatus": exc.code, "body": exc.read().decode("utf-8", errors="replace")}
    except Exception as exc:
        result = {"status": "failed", "error": str(exc)}
    append_json_line(CALLBACK_RESULT_PATH, {"ts": iso_utc(), "request_id": request_id, "callbackUrl": callback_url, **result})
    return result


def run_codex_streaming(prompt: str, appender: FeishuAppender, config: BridgeConfig, timing: TimingTrace) -> str:
    if os.environ.get("FEISHU_APP_CODEX_FAKE_OUTPUT"):
        fake = os.environ["FEISHU_APP_CODEX_FAKE_OUTPUT"]
        appender.finalize(fake)
        timing.set_meta("codex_streaming_source", "fake")
        return fake

    cmd = [
        *config.codex_cmd,
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        prompt,
    ]
    log(f"$ {' '.join(shlex.quote(part) for part in cmd[:6])} ...")
    timing.mark("codex_start")
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    assert proc.stdout is not None
    timing.mark("codex_process_started")

    completed_text = ""
    streamed_any = False
    last_progress = time.monotonic()
    progress_count = 0

    while True:
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        line = proc.stdout.readline() if ready else ""
        if not line:
            if proc.poll() is not None:
                break
            if not completed_text and not streamed_any and time.monotonic() - last_progress >= config.progress_interval:
                progress_count += 1
                elapsed = int(time.monotonic() - timing.started_monotonic)
                text = f"Codex Thinking\nWaited about {elapsed}s. Still processing."
                appender.progress(text)
                timing.bump("feishu_progress_updates")
                timing.mark("feishu_first_progress_update")
                timing.set_meta("progress_updates", progress_count)
                last_progress = time.monotonic()
            continue

        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        timing.bump("codex_json_events")
        timing.mark("codex_first_json_event")
        delta = extract_delta(event)
        if delta:
            streamed_any = True
            completed_text += delta
            appender.append(delta)
            timing.bump("codex_delta_chunks")
            timing.mark("codex_first_output")
            continue
        final_text = extract_completed_text(event)
        if final_text:
            completed_text = final_text
            timing.bump("codex_completed_messages")
            timing.mark("codex_completed_text")

    exit_code = proc.wait()
    timing.mark("codex_process_exit")
    if exit_code != 0 and not completed_text:
        raise RuntimeError(f"codex exec failed with exit {exit_code}")
    timing.set_meta("codex_streaming_source", "delta" if streamed_any else "final_message")
    appender.finalize(completed_text)
    timing.mark("codex_done")
    timing.mark("final_update")
    return completed_text


def write_request(event: dict[str, Any]) -> dict[str, Any]:
    event_id = str(event["event_id"])
    request_id = request_id_for(event_id)
    record = {
        "id": request_id,
        "ts": iso_utc(),
        "from": "feishu",
        "to": "codex",
        "kind": "request",
        "type": "request",
        "request_id": request_id,
        "project": ROOT.name,
        "session": {"feishu": event.get("chat_id"), "codex": "codex-cli"},
        "prompt": event.get("content", ""),
        "text": event.get("content", ""),
        "meta": {
            "protocol_version": PROTOCOL_VERSION,
            "feishu": {
                "event_id": event_id,
                "message_id": event.get("message_id"),
                "chat_id": event.get("chat_id"),
                "sender_id": event.get("sender_id"),
                "chat_type": event.get("chat_type"),
                "message_type": event.get("message_type"),
            },
        },
    }
    append_json_line(REQUEST_PATH, record)
    append_json_line(EVENTS_PATH, record)
    return record


def write_result(event: dict[str, Any], output: str, status: str, callback: dict[str, Any], error: str = "") -> None:
    event_id = str(event["event_id"])
    request_id = request_id_for(event_id)
    record = {
        "id": event_record_id("res", event_id),
        "ts": iso_utc(),
        "from": "codex",
        "to": "feishu",
        "kind": "result",
        "type": "result",
        "request_id": request_id,
        "project": ROOT.name,
        "session": {"feishu": event.get("chat_id"), "codex": "codex-cli"},
        "result": output,
        "text": output,
        "meta": {
            "protocol_version": PROTOCOL_VERSION,
            "status": status,
            "error": error,
            "callback": callback,
            "feishu": {
                "event_id": event_id,
                "incoming_message_id": event.get("message_id"),
                "chat_id": event.get("chat_id"),
            },
        },
    }
    append_json_line(RESULT_PATH, record)
    append_json_line(EVENTS_PATH, record)


def update_session_map(event: dict[str, Any], reply_message_id: Optional[str]) -> None:
    now = iso_utc()
    payload = {
        "version": 1,
        "updated_at": now,
        "projects": {
            ROOT.name: {
                "path": str(ROOT),
                "agents": {
                    "feishu": {
                        "session": event.get("chat_id"),
                        "identity": event.get("sender_id"),
                        "updated_at": now,
                        "meta": {
                            "last_event_id": event.get("event_id"),
                            "last_incoming_message_id": event.get("message_id"),
                            "last_reply_message_id": reply_message_id,
                        },
                    },
                    "codex": {
                        "session": "codex-cli",
                        "identity": "codex-cli",
                        "updated_at": now,
                        "meta": {"bridge": ROOT.name},
                    },
                },
            }
        },
    }
    write_json(SESSION_MAP_PATH, payload)


def process_event(event: dict[str, Any], *, state: State, feishu: FeishuClient, config: BridgeConfig) -> None:
    if not event.get("event_id") or not event.get("message_id") or not event.get("chat_id"):
        log(f"skip incomplete event: {event}")
        return
    if not state.insert_event_once(event):
        log(f"skip duplicate event: {event['event_id']}")
        return

    timing = TimingTrace(event, config.output_mode)
    timing.mark("event_accepted")
    write_request(event)
    timing.mark("request_written")
    state.add_message(event["chat_id"], "user", event.get("content", ""), feishu_message_id=event["message_id"], event_id=event["event_id"])

    reply_message_id: Optional[str] = None
    try:
        timing.mark("placeholder_start")
        reply_message_id = feishu.reply_placeholder(event["message_id"], config.placeholder_text)
        timing.mark("placeholder_sent")
    except Exception as exc:
        log(f"placeholder reply failed; sending chat message fallback: {exc}")
        reply_message_id = feishu.send_text(event["chat_id"], config.placeholder_text)
        timing.mark("placeholder_sent")

    state.mark_response(
        event_id=event["event_id"],
        incoming_message_id=event["message_id"],
        reply_message_id=reply_message_id,
        chat_id=event["chat_id"],
        status="running",
    )

    def on_flush(buffer: str) -> None:
        timing.bump("feishu_updates")
        timing.mark("feishu_first_update")
        state.mark_response(
            event_id=event["event_id"],
            incoming_message_id=event["message_id"],
            reply_message_id=reply_message_id,
            chat_id=event["chat_id"],
            status="running",
            output=buffer,
        )

    appender = FeishuAppender(
        feishu,
        reply_message_id or "",
        event["chat_id"],
        interval=config.update_interval,
        fallback_interval=config.fallback_message_interval,
        on_flush=on_flush,
    )

    try:
        prompt = build_prompt(event.get("content", ""), state.recent_history(event["chat_id"], config.history_limit))
        timing.mark("prompt_built")
        output = run_codex_streaming(prompt, appender, config, timing)
        callback = maybe_post_callback(output=output, request_id=request_id_for(event["event_id"]), enabled=config.callback_enabled)
        timing.mark("callback_recorded")
        write_result(event, output, "done", callback)
        update_session_map(event, reply_message_id)
        state.add_message(event["chat_id"], "assistant", output, feishu_message_id=reply_message_id or "", event_id=event["event_id"])
        state.mark_response(
            event_id=event["event_id"],
            incoming_message_id=event["message_id"],
            reply_message_id=reply_message_id,
            chat_id=event["chat_id"],
            status="done",
            output=output,
        )
        timing.write(status="done", reply_message_id=reply_message_id, output=output)
        log(f"completed event={event['event_id']} reply={reply_message_id}")
    except Exception as exc:
        error = f"Bridge failed: {exc}"
        log(error)
        try:
            appender.finalize(error)
        except Exception as update_exc:
            log(f"failed to send error to Feishu/Lark: {update_exc}")
        callback = {"status": "skipped", "reason": "bridge failed before callback", "error": str(exc)}
        write_result(event, error, "failed", callback, error=str(exc))
        state.mark_response(
            event_id=event["event_id"],
            incoming_message_id=event["message_id"],
            reply_message_id=reply_message_id,
            chat_id=event["chat_id"],
            status="failed",
            output=error,
            error=str(exc),
        )
        timing.write(status="failed", reply_message_id=reply_message_id, output=error, error=str(exc))


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def bridge_pid_status() -> dict[str, Any]:
    status: dict[str, Any] = {"pid": None, "pid_file_exists": PID_PATH.exists(), "running": False}
    if not PID_PATH.exists():
        return status
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except ValueError:
        status["error"] = "invalid pid file"
        return status
    status["pid"] = pid
    status["running"] = is_pid_alive(pid)
    return status


def build_healthcheck(feishu: FeishuClient) -> dict[str, Any]:
    bridge = bridge_pid_status()
    event_bus = parse_event_status(feishu.event_status())
    issues: list[str] = []
    if not bridge["running"]:
        issues.append("bridge process is not running")
    if not event_bus["bus_running"]:
        issues.append("lark event bus is not running")
    if event_bus["active_consumers"] != 1:
        issues.append(f"active consumers expected 1, got {event_bus['active_consumers']}")
    if event_bus.get("dropped", 0) > 0:
        issues.append(f"dropped events is {event_bus['dropped']}")
    return {
        "schema": "feishu-app-codex.health.v1",
        "ts": iso_utc(),
        "root": str(ROOT),
        "healthy": not issues,
        "issues": issues,
        "bridge": bridge,
        "event_bus": event_bus,
    }


def make_config(args: argparse.Namespace) -> BridgeConfig:
    return BridgeConfig(
        lark_cli=resolve_command("LARK_CLI", "lark-cli"),
        codex_cmd=resolve_command("CODEX_CMD", "codex"),
        history_limit=args.history_limit,
        update_interval=args.update_interval,
        progress_interval=args.progress_interval,
        fallback_message_interval=args.fallback_message_interval,
        placeholder_text=args.placeholder,
        output_mode=args.output_mode,
        direct_chunk_min_chars=args.direct_chunk_min_chars,
        callback_enabled=args.enable_callback,
    )


def cmd_init(_args: argparse.Namespace) -> int:
    ensure_dirs()
    State()
    if not SESSION_MAP_PATH.exists():
        write_json(SESSION_MAP_PATH, {"version": 1, "updated_at": None, "projects": {}})
    print(f"initialized {ROOT}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ensure_dirs()
    state = State()
    config = make_config(args)
    feishu = FeishuClient(config.lark_cli)
    auth = feishu.auth_status()
    log(f"auth identity={auth.get('identity')} appId={auth.get('appId')}")
    stopped = False
    consumer: Optional[EventConsumer] = None

    def handle_signal(signum: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True
        log(f"received signal {signum}, shutting down")
        if consumer and consumer.poll() is None:
            consumer.terminate()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while not stopped:
        consumer = feishu.start_event_consumer()
        while not stopped:
            line = consumer.readline()
            if not line:
                if consumer.poll() is not None:
                    log(f"event consumer exited with {consumer.returncode}")
                    break
                time.sleep(0.05)
                continue
            try:
                payload = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            event = normalize_event(payload)
            process_event(event, state=state, feishu=feishu, config=config)
        consumer.close()
        if not stopped:
            time.sleep(2.0)
    if args.stop_event_bus_on_exit:
        feishu.stop_event_bus(force=True)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    ensure_dirs()
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text(encoding="utf-8").strip())
            if is_pid_alive(pid):
                print(f"bridge already running: pid {pid}")
                return 0
        except ValueError:
            pass

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--history-limit",
        str(args.history_limit),
        "--update-interval",
        str(args.update_interval),
        "--progress-interval",
        str(args.progress_interval),
        "--fallback-message-interval",
        str(args.fallback_message_interval),
        "--placeholder",
        args.placeholder,
        "--output-mode",
        args.output_mode,
        "--direct-chunk-min-chars",
        str(args.direct_chunk_min_chars),
    ]
    if args.enable_callback:
        cmd.append("--enable-callback")
    if args.stop_event_bus_on_exit:
        cmd.append("--stop-event-bus-on-exit")
    log_file = LOG_PATH.open("a", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
    PID_PATH.write_text(str(proc.pid), encoding="utf-8")
    print(f"started bridge pid {proc.pid}")
    print(f"log: {LOG_PATH}")
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text(encoding="utf-8").strip())
            if is_pid_alive(pid):
                os.kill(pid, signal.SIGTERM)
                print(f"sent SIGTERM to bridge pid {pid}")
                for _ in range(30):
                    if not is_pid_alive(pid):
                        break
                    time.sleep(0.1)
                if is_pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
                    print(f"sent SIGKILL to bridge pid {pid}")
        except ValueError:
            print("invalid pid file")
        PID_PATH.unlink(missing_ok=True)
    else:
        print("no bridge pid file")
    FeishuClient(resolve_command("LARK_CLI", "lark-cli")).stop_event_bus(force=True)
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    bridge = bridge_pid_status()
    print(f"bridge pid: {bridge.get('pid') or 'not running'} ({'running' if bridge.get('running') else 'not running'})")
    print(FeishuClient(resolve_command("LARK_CLI", "lark-cli")).event_status())
    if LOG_PATH.exists():
        print(f"log: {LOG_PATH}")
    return 0


def cmd_healthcheck(args: argparse.Namespace) -> int:
    health = build_healthcheck(FeishuClient(resolve_command("LARK_CLI", "lark-cli")))
    if args.format == "json":
        print(json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"healthy: {health['healthy']}")
        for issue in health["issues"]:
            print(f"issue: {issue}")
    return 0 if health["healthy"] else 1


def record_watch(action: str, health: dict[str, Any]) -> None:
    record = {
        "schema": "feishu-app-codex.watch.v1",
        "ts": iso_utc(),
        "action": action,
        "healthy": health.get("healthy"),
        "issues": health.get("issues", []),
        "bridge": health.get("bridge"),
        "event_bus": {
            key: health.get("event_bus", {}).get(key)
            for key in ("bus_running", "bus_pid", "active_consumers", "received", "dropped")
        },
    }
    append_json_line(WATCH_PATH, record)
    log(f"watch action={action} healthy={record['healthy']} issues={record['issues']}")


def cmd_watch(args: argparse.Namespace) -> int:
    while True:
        feishu = FeishuClient(resolve_command("LARK_CLI", "lark-cli"))
        health = build_healthcheck(feishu)
        if health["healthy"]:
            if args.once or args.verbose:
                record_watch("healthy", health)
        else:
            record_watch("unhealthy", health)
            if not args.no_restart:
                cmd_stop(argparse.Namespace())
                time.sleep(args.restart_delay)
                cmd_start(args)
                time.sleep(args.restart_delay)
                health = build_healthcheck(FeishuClient(resolve_command("LARK_CLI", "lark-cli")))
                record_watch("restart_done", health)
        if args.once:
            return 0 if health["healthy"] else 1
        time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Feishu/Lark app to Codex bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--history-limit", type=int, default=12)
        p.add_argument("--update-interval", type=float, default=float(os.environ.get("FEISHU_APP_CODEX_UPDATE_INTERVAL", "0.8")))
        p.add_argument("--progress-interval", type=float, default=float(os.environ.get("FEISHU_APP_CODEX_PROGRESS_INTERVAL", "30")))
        p.add_argument(
            "--fallback-message-interval",
            type=float,
            default=float(os.environ.get("FEISHU_APP_CODEX_FALLBACK_MESSAGE_INTERVAL", "60")),
        )
        p.add_argument("--placeholder", default="Codex Thinking\nReceived. Preparing to process.")
        p.add_argument("--output-mode", choices=("edit_append",), default="edit_append")
        p.add_argument("--direct-chunk-min-chars", type=int, default=120)
        p.add_argument(
            "--enable-callback",
            action="store_true",
            default=env_bool("FEISHU_APP_CODEX_ENABLE_CALLBACK", False),
            help="Enable business callback POSTs. Disabled by default.",
        )
        p.add_argument("--stop-event-bus-on-exit", action="store_true")

    init_p = sub.add_parser("init", help="initialize runtime directories and SQLite")
    init_p.set_defaults(func=cmd_init)

    run_p = sub.add_parser("run", help="run bridge in foreground")
    add_common(run_p)
    run_p.set_defaults(func=cmd_run)

    start_p = sub.add_parser("start", help="start bridge in background")
    add_common(start_p)
    start_p.set_defaults(func=cmd_start)

    stop_p = sub.add_parser("stop", help="stop bridge and event bus")
    stop_p.set_defaults(func=cmd_stop)

    status_p = sub.add_parser("status", help="show bridge and event bus status")
    status_p.set_defaults(func=cmd_status)

    health_p = sub.add_parser("healthcheck", help="check bridge, event bus, consumers, and drops")
    health_p.add_argument("--format", choices=("text", "json"), default="text")
    health_p.set_defaults(func=cmd_healthcheck)

    watch_p = sub.add_parser("watch", help="watch health and restart when unhealthy")
    add_common(watch_p)
    watch_p.add_argument("--interval", type=float, default=30)
    watch_p.add_argument("--restart-delay", type=float, default=2)
    watch_p.add_argument("--once", action="store_true")
    watch_p.add_argument("--no-restart", action="store_true")
    watch_p.add_argument("--verbose", action="store_true")
    watch_p.set_defaults(func=cmd_watch)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

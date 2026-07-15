#!/usr/bin/env python3
"""
Local HTTP bridge for raw Dify audit fallback tasks.

Java posts a task and gets an immediate accepted response. The bridge then runs
Codex in the background, extracts replacedSql/analysis from the final answer,
writes local NDJSON traces, and posts the result back to the Java callback URL.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "raw-dify-audit-http.ndjson"
REQUEST_PATH = ROOT / "inbox" / "raw-dify-audit-requests.ndjson"
RESULT_PATH = ROOT / "inbox" / "raw-dify-audit-results.ndjson"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_CODEX_TIMEOUT_SECONDS = 1800


def iso_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_ms() -> int:
    return int(time.time() * 1000)


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


def log_event(event: str, **fields: Any) -> None:
    record = {"ts": iso_utc(), "event": event, **fields}
    append_json_line(LOG_PATH, record)
    print(json.dumps(record, ensure_ascii=False), flush=True)


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(content_length)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


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
    if not isinstance(item, dict) or item.get("type") != "agent_message":
        return ""
    text = item.get("text")
    return text if isinstance(text, str) else ""


def parse_json_object(text: str) -> dict[str, Any]:
    trimmed = (text or "").strip()
    if not trimmed:
        return {}
    try:
        value = json.loads(trimmed)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    marker = "```json"
    if marker in trimmed:
        tail = trimmed.split(marker, 1)[1]
        tail = tail.split("```", 1)[0]
        try:
            value = json.loads(tail.strip())
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            pass
    start = trimmed.find("{")
    end = trimmed.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(trimmed[start:end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def parse_codex_result(text: str) -> tuple[str, str]:
    payload = parse_json_object(text)
    replaced_sql = (
        payload.get("replacedSql")
        or payload.get("replaced_sql")
        or payload.get("replacedSQL")
        or ""
    )
    analysis = payload.get("analysis") or payload.get("reason") or payload.get("message") or text
    return str(replaced_sql or "").strip(), str(analysis or "").strip()


def run_codex(prompt: str, *, timeout_seconds: int) -> tuple[str, list[dict[str, Any]]]:
    if os.environ.get("RAW_DIFY_BRIDGE_FAKE_CODEX"):
        fake = os.environ["RAW_DIFY_BRIDGE_FAKE_CODEX"]
        return fake, [{"type": "fake", "text": fake}]
    codex_cmd = shlex.split(os.environ.get("CODEX_CMD", "codex"))
    cmd = [
        *codex_cmd,
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        prompt,
    ]
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
    completed_text = ""
    events: list[dict[str, Any]] = []
    start = time.monotonic()
    while True:
        if time.monotonic() - start > timeout_seconds:
            proc.kill()
            raise TimeoutError(f"codex exec timeout after {timeout_seconds}s")
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
            continue
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(event)
        delta = extract_delta(event)
        if delta:
            completed_text += delta
            continue
        final_text = extract_completed_text(event)
        if final_text:
            completed_text = final_text
    exit_code = proc.wait()
    if exit_code != 0 and not completed_text:
        raise RuntimeError(f"codex exec failed with exit {exit_code}")
    return completed_text, events


def post_callback(url: str, payload: dict[str, Any], *, timeout_seconds: int = 30) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def build_callback_payload(task: dict[str, Any], bridge_request_id: str, replaced_sql: str, analysis: str,
                           error_msg: str = "") -> dict[str, Any]:
    return {
        "auditLogId": task.get("auditLogId"),
        "replaceAssetId": task.get("replaceAssetId"),
        "docId": task.get("docId"),
        "rawTableName": task.get("rawTableName"),
        "sceneType": task.get("sceneType"),
        "replacedSql": replaced_sql,
        "analysis": analysis,
        "source": task.get("source") or "LOCAL_CODEX_BRIDGE",
        "tableOwnerName": task.get("tableOwnerName"),
        "bridgeRequestId": bridge_request_id,
        "errorMsg": error_msg,
    }


def handle_task(task: dict[str, Any], bridge_request_id: str, codex_timeout_seconds: int) -> None:
    started_at = iso_utc()
    start_ms = now_ms()
    prompt = str(task.get("prompt") or "")
    callback_url = str(task.get("callbackUrl") or "")
    result_record: dict[str, Any] = {
        "ts": started_at,
        "bridgeRequestId": bridge_request_id,
        "auditLogId": task.get("auditLogId"),
        "replaceAssetId": task.get("replaceAssetId"),
        "docId": task.get("docId"),
        "rawTableName": task.get("rawTableName"),
        "sceneType": task.get("sceneType"),
        "callbackUrl": callback_url,
    }
    try:
        if not callback_url:
            raise ValueError("callbackUrl is blank")
        if not prompt:
            raise ValueError("prompt is blank")
        output_text, codex_events = run_codex(prompt, timeout_seconds=codex_timeout_seconds)
        replaced_sql, analysis = parse_codex_result(output_text)
        callback_payload = build_callback_payload(task, bridge_request_id, replaced_sql, analysis)
        callback_status, callback_body = post_callback(callback_url, callback_payload)
        result_record.update({
            "status": "success" if replaced_sql else "no_sql",
            "durationMs": now_ms() - start_ms,
            "replacedSqlLength": len(replaced_sql),
            "analysisLength": len(analysis),
            "codexEventCount": len(codex_events),
            "codexOutput": output_text,
            "callbackStatus": callback_status,
            "callbackBody": callback_body,
        })
        append_json_line(RESULT_PATH, result_record)
        log_event("task_completed", bridgeRequestId=bridge_request_id, auditLogId=task.get("auditLogId"),
                  status=result_record["status"], callbackStatus=callback_status)
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        callback_payload = build_callback_payload(task, bridge_request_id, "", "", error_msg)
        callback_status: Optional[int] = None
        callback_body = ""
        if callback_url:
            try:
                callback_status, callback_body = post_callback(callback_url, callback_payload)
            except Exception as callback_exc:  # pragma: no cover
                callback_body = f"{type(callback_exc).__name__}: {callback_exc}"
        result_record.update({
            "status": "failed",
            "durationMs": now_ms() - start_ms,
            "errorMsg": error_msg,
            "traceback": traceback.format_exc(),
            "callbackStatus": callback_status,
            "callbackBody": callback_body,
        })
        append_json_line(RESULT_PATH, result_record)
        log_event("task_failed", bridgeRequestId=bridge_request_id, auditLogId=task.get("auditLogId"),
                  errorMsg=error_msg, callbackStatus=callback_status)


class RawDifyAuditHandler(BaseHTTPRequestHandler):
    server_version = "RawDifyAuditBridge/1.0"
    executor: ThreadPoolExecutor
    codex_timeout_seconds: int

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        log_event("http_access", client=self.client_address[0], message=format % args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            json_response(self, 404, {"status": "not_found"})
            return
        json_response(self, 200, {"status": "ok", "ts": iso_utc(), "root": str(ROOT)})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/raw-dify-audit/dispatch":
            json_response(self, 404, {"status": "not_found"})
            return
        try:
            task = read_json_body(self)
            bridge_request_id = str(task.get("bridgeRequestId") or f"raw-dify-{now_ms()}-{uuid.uuid4().hex[:8]}")
            task["bridgeRequestId"] = bridge_request_id
            append_json_line(REQUEST_PATH, {"ts": iso_utc(), **task})
            self.executor.submit(handle_task, task, bridge_request_id, self.codex_timeout_seconds)
            log_event("task_accepted", bridgeRequestId=bridge_request_id, auditLogId=task.get("auditLogId"))
            json_response(self, 202, {"status": "accepted", "bridgeRequestId": bridge_request_id})
        except Exception as exc:
            log_event("dispatch_error", errorMsg=f"{type(exc).__name__}: {exc}")
            json_response(self, 400, {"status": "failed", "message": str(exc)})


def serve(host: str, port: int, workers: int, codex_timeout_seconds: int) -> None:
    ROOT.joinpath("inbox").mkdir(parents=True, exist_ok=True)
    ROOT.joinpath("logs").mkdir(parents=True, exist_ok=True)
    RawDifyAuditHandler.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="raw-dify-bridge")
    RawDifyAuditHandler.codex_timeout_seconds = codex_timeout_seconds
    server = ThreadingHTTPServer((host, port), RawDifyAuditHandler)
    log_event("server_started", host=host, port=port, workers=workers,
              codexTimeoutSeconds=codex_timeout_seconds, thread=threading.current_thread().name)
    try:
        server.serve_forever()
    finally:
        RawDifyAuditHandler.executor.shutdown(wait=False)
        server.server_close()
        log_event("server_stopped", host=host, port=port)


def main() -> int:
    parser = argparse.ArgumentParser(description="Raw Dify audit HTTP bridge")
    parser.add_argument("command", nargs="?", default="serve", choices=("serve",))
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workers", type=int, default=int(os.environ.get("RAW_DIFY_BRIDGE_WORKERS", "1")))
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=int(os.environ.get("RAW_DIFY_BRIDGE_CODEX_TIMEOUT_SECONDS", str(DEFAULT_CODEX_TIMEOUT_SECONDS))),
    )
    args = parser.parse_args()
    serve(args.host, args.port, max(1, args.workers), max(1, args.codex_timeout_seconds))
    return 0


if __name__ == "__main__":
    sys.exit(main())

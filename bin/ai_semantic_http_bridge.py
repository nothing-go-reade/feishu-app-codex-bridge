#!/usr/bin/env python3
"""
Local HTTP bridge for AI semantic checklist generation.

Java posts one semantic task and gets an immediate accepted response. The
bridge runs Codex in the background, extracts the semantic JSON from the final
answer, writes local NDJSON traces, and posts the result back to Java.
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
LOG_PATH = ROOT / "logs" / "ai-semantic-http.ndjson"
REQUEST_PATH = ROOT / "inbox" / "ai-semantic-requests.ndjson"
RESULT_PATH = ROOT / "inbox" / "ai-semantic-results.ndjson"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_CODEX_TIMEOUT_SECONDS = 3600
DISPATCH_PATH = "/ai-semantic/dispatch"


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


def extract_text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("delta")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def extract_delta(event: dict[str, Any]) -> str:
    event_type = str(event.get("type", ""))
    if "delta" not in event_type.lower():
        return ""
    for key in ("delta", "text", "content"):
        value = event.get(key)
        text = extract_text_from_content(value)
        if text:
            return text
    item = event.get("item")
    if isinstance(item, dict):
        for key in ("delta", "text", "content"):
            text = extract_text_from_content(item.get(key))
            if text:
                return text
    return ""


def extract_completed_text(event: dict[str, Any]) -> str:
    event_type = str(event.get("type", ""))
    item = event.get("item")
    if isinstance(item, dict):
        item_type = str(item.get("type", ""))
        if "agent_message" in item_type or "assistant" in item_type:
            text = extract_text_from_content(item.get("text") or item.get("content"))
            if text:
                return text
    if "completed" in event_type.lower() or "message" in event_type.lower():
        text = extract_text_from_content(event.get("text") or event.get("content") or event.get("message"))
        if text:
            return text
    return ""


def parse_json_object(text: str) -> dict[str, Any]:
    trimmed = (text or "").strip()
    if not trimmed:
        return {}
    try:
        value = json.loads(trimmed)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    for marker in ("```json", "```"):
        if marker in trimmed:
            tail = trimmed.split(marker, 1)[1]
            tail = tail.split("```", 1)[0]
            try:
                value = json.loads(tail.strip())
                return value if isinstance(value, dict) else {}
            except json.JSONDecodeError:
                pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(trimmed):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(trimmed[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def run_codex(prompt: str, *, timeout_seconds: int) -> tuple[str, list[dict[str, Any]]]:
    if os.environ.get("AI_SEMANTIC_BRIDGE_FAKE_CODEX"):
        fake = os.environ["AI_SEMANTIC_BRIDGE_FAKE_CODEX"]
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
        "-",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        stdout, _ = proc.communicate(input=prompt, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise TimeoutError(f"codex exec timeout after {timeout_seconds}s")
    events: list[dict[str, Any]] = []
    completed_text = ""
    delta_text = ""
    for line in stdout.splitlines():
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
            delta_text += delta
        final_text = extract_completed_text(event)
        if final_text:
            completed_text = final_text
    if not completed_text:
        completed_text = delta_text
    if proc.returncode != 0 and not completed_text:
        raise RuntimeError(f"codex exec failed with exit {proc.returncode}")
    return completed_text, events


def post_callback(url: str, payload: dict[str, Any], *, timeout_seconds: int = 60) -> tuple[int, str]:
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


def resolve_asset_key(task: dict[str, Any]) -> str:
    return str(task.get("assetKey") or task.get("rawTableName") or "")


def resolve_batch_id(task: dict[str, Any]) -> str:
    return str(task.get("batchId") or task.get("sceneType") or "")


def build_callback_payload(
    task: dict[str, Any],
    bridge_request_id: str,
    output_text: str,
    semantic_result: Optional[dict[str, Any]] = None,
    error_msg: str = "",
) -> dict[str, Any]:
    semantic_json = json.dumps(semantic_result, ensure_ascii=False, separators=(",", ":")) if semantic_result else ""
    asset_key = resolve_asset_key(task)
    batch_id = resolve_batch_id(task)
    return {
        "assetKey": asset_key,
        "batchId": batch_id,
        "rawTableName": asset_key,
        "sceneType": batch_id,
        "bridgeRequestId": bridge_request_id,
        "semanticResultJson": semantic_json,
        "analysis": semantic_json,
        "outputText": output_text,
        "errorMsg": error_msg,
    }


def handle_task(task: dict[str, Any], bridge_request_id: str, codex_timeout_seconds: int) -> None:
    started_at = iso_utc()
    start_ms = now_ms()
    asset_key = resolve_asset_key(task)
    batch_id = resolve_batch_id(task)
    prompt = str(task.get("prompt") or "")
    callback_url = str(task.get("callbackUrl") or "")
    result_record: dict[str, Any] = {
        "ts": started_at,
        "bridgeRequestId": bridge_request_id,
        "assetKey": asset_key,
        "batchId": batch_id,
        "callbackUrl": callback_url,
        "promptLength": len(prompt),
    }
    output_text = ""
    try:
        if not callback_url:
            raise ValueError("callbackUrl is blank")
        if not asset_key:
            raise ValueError("assetKey is blank")
        if not batch_id:
            raise ValueError("batchId is blank")
        if not prompt:
            raise ValueError("prompt is blank")
        log_event("codex_start", bridgeRequestId=bridge_request_id, assetKey=asset_key,
                  batchId=batch_id, promptLength=len(prompt))
        output_text, codex_events = run_codex(prompt, timeout_seconds=codex_timeout_seconds)
        semantic_result = parse_json_object(output_text)
        if not semantic_result:
            raise ValueError("Codex output does not contain semantic JSON")
        callback_payload = build_callback_payload(task, bridge_request_id, output_text, semantic_result)
        callback_status, callback_body = post_callback(callback_url, callback_payload)
        result_record.update({
            "status": "success",
            "durationMs": now_ms() - start_ms,
            "semanticResultLength": len(callback_payload["semanticResultJson"]),
            "outputLength": len(output_text),
            "codexEventCount": len(codex_events),
            "callbackStatus": callback_status,
            "callbackBody": callback_body,
        })
        append_json_line(RESULT_PATH, result_record)
        log_event("task_completed", bridgeRequestId=bridge_request_id, assetKey=asset_key,
                  batchId=batch_id, callbackStatus=callback_status)
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        callback_status: Optional[int] = None
        callback_body = ""
        if callback_url:
            try:
                callback_payload = build_callback_payload(task, bridge_request_id, output_text, None, error_msg)
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
            "outputLength": len(output_text),
        })
        append_json_line(RESULT_PATH, result_record)
        log_event("task_failed", bridgeRequestId=bridge_request_id, assetKey=asset_key,
                  batchId=batch_id, errorMsg=error_msg, callbackStatus=callback_status)


class AiSemanticHandler(BaseHTTPRequestHandler):
    server_version = "AiSemanticBridge/1.0"
    executor: ThreadPoolExecutor
    codex_timeout_seconds: int

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        log_event("http_access", client=self.client_address[0], message=format % args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            json_response(self, 404, {"status": "not_found", "expectedPath": DISPATCH_PATH})
            return
        json_response(self, 200, {"status": "ok", "ts": iso_utc(), "root": str(ROOT), "dispatchPath": DISPATCH_PATH})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != DISPATCH_PATH:
            json_response(self, 404, {"status": "not_found", "expectedPath": DISPATCH_PATH})
            return
        try:
            task = read_json_body(self)
            bridge_request_id = str(task.get("bridgeRequestId") or f"ai-semantic-{now_ms()}-{uuid.uuid4().hex[:8]}")
            task["bridgeRequestId"] = bridge_request_id
            append_json_line(REQUEST_PATH, {"ts": iso_utc(), **task, "prompt": f"<omitted length={len(str(task.get('prompt') or ''))}>"})
            self.executor.submit(handle_task, task, bridge_request_id, self.codex_timeout_seconds)
            log_event("task_accepted", bridgeRequestId=bridge_request_id, assetKey=resolve_asset_key(task),
                      batchId=resolve_batch_id(task), promptLength=len(str(task.get("prompt") or "")))
            json_response(self, 202, {"status": "accepted", "bridgeRequestId": bridge_request_id})
        except Exception as exc:
            log_event("dispatch_error", errorMsg=f"{type(exc).__name__}: {exc}")
            json_response(self, 400, {"status": "failed", "message": str(exc)})


def serve(host: str, port: int, workers: int, codex_timeout_seconds: int) -> None:
    ROOT.joinpath("inbox").mkdir(parents=True, exist_ok=True)
    ROOT.joinpath("logs").mkdir(parents=True, exist_ok=True)
    AiSemanticHandler.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ai-semantic-bridge")
    AiSemanticHandler.codex_timeout_seconds = codex_timeout_seconds
    server = ThreadingHTTPServer((host, port), AiSemanticHandler)
    log_event("server_started", host=host, port=port, workers=workers,
              codexTimeoutSeconds=codex_timeout_seconds, thread=threading.current_thread().name)
    try:
        server.serve_forever()
    finally:
        AiSemanticHandler.executor.shutdown(wait=False)
        server.server_close()
        log_event("server_stopped", host=host, port=port)


def main() -> int:
    parser = argparse.ArgumentParser(description="AI semantic checklist HTTP bridge")
    parser.add_argument("command", nargs="?", default="serve", choices=("serve",))
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workers", type=int, default=int(os.environ.get("AI_SEMANTIC_BRIDGE_WORKERS", "1")))
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=int(os.environ.get("AI_SEMANTIC_BRIDGE_CODEX_TIMEOUT_SECONDS", str(DEFAULT_CODEX_TIMEOUT_SECONDS))),
    )
    args = parser.parse_args()
    serve(args.host, args.port, max(1, args.workers), max(1, args.codex_timeout_seconds))
    return 0


if __name__ == "__main__":
    sys.exit(main())

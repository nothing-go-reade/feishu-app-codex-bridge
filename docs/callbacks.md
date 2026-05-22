# Callback Design

## Default Policy

Callbacks are disabled by default.

The bridge can run safely as:

```text
Feishu/Lark -> Codex -> local files -> Feishu/Lark
```

No business HTTP request is sent unless explicitly enabled.

## Enable Callback

```bash
python3 bin/feishu_app_codex_bridge.py start --enable-callback
```

or:

```bash
FEISHU_APP_CODEX_ENABLE_CALLBACK=true python3 bin/feishu_app_codex_bridge.py start
```

## Callback Contract

For callback workflows, Codex should output a JSON object:

```json
{
  "result": "candidate result",
  "analysis": "why this result is safe or why no result is produced",
  "callback": {
    "callbackUrl": "http://127.0.0.1:8080/example/callback",
    "taskId": "task-001",
    "source": "FEISHU_APP_CODEX_BRIDGE"
  }
}
```

The bridge reads `callback.callbackUrl` and posts a payload containing:

```json
{
  "bridgeRequestId": "req_feishu_xxx",
  "result": "candidate result",
  "analysis": "analysis text",
  "callback": {
    "callbackUrl": "http://127.0.0.1:8080/example/callback",
    "taskId": "task-001",
    "source": "FEISHU_APP_CODEX_BRIDGE"
  },
  "rawOutput": "original Codex output"
}
```

## Why Bridge Owns Callback

Codex should not be the component that mutates business state.

Reasons:

- The model runtime may be sandboxed.
- Network access may be restricted.
- Business side effects need one stable owner.
- Callback status must be logged locally.
- Teams need a switch to disable callbacks without changing prompts.

## Recommended Business Rule

Treat model output as a candidate result.

The receiving business system should still validate:

- schema
- permissions
- object identity
- SQL syntax or EXPLAIN
- ownership
- idempotency

## Callback Result File

Callback attempts are appended to:

```text
inbox/callback-results.ndjson
```

Typical records:

```json
{"status":"disabled","reason":"callback switch is off"}
```

```json
{"status":"success","httpStatus":200,"body":"ok"}
```

```json
{"status":"failed","error":"Connection refused"}
```


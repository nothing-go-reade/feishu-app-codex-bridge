# File Protocol

## Why Files

The bridge writes local files so a team can inspect what happened without relying on one in-memory process.

The file protocol is intentionally simple:

- Append-only NDJSON for request/result/event streams.
- JSON snapshot for current session state.
- SQLite for internal dedupe and response state.

## Directory Semantics

### `inbox/`

Direction-based message queues.

Recommended files:

```text
inbox/feishu-to-codex.ndjson
inbox/codex-to-feishu.ndjson
inbox/callback-results.ndjson
```

### `logs/`

Append-only audit and timing logs.

Recommended files:

```text
logs/events.ndjson
logs/timings.ndjson
logs/watch.ndjson
```

### `state/`

Current session snapshots.

Recommended files:

```text
state/session-map.json
```

### `var/`

Runtime internals. These files are not part of the cross-agent protocol.

Recommended files:

```text
var/bridge.log
var/bridge.pid
var/bridge.sqlite3
```

## Request Record

```json
{
  "id": "req_feishu_example",
  "ts": "2026-01-01T00:00:00Z",
  "from": "feishu",
  "to": "codex",
  "kind": "request",
  "type": "request",
  "request_id": "req_feishu_example",
  "project": "feishu-app-codex-bridge",
  "session": {
    "feishu": "chat_xxx",
    "codex": "codex-cli"
  },
  "prompt": "User message",
  "text": "User message",
  "meta": {
    "protocol_version": "agent-bridge.v1",
    "feishu": {
      "event_id": "event_xxx",
      "message_id": "message_xxx",
      "chat_id": "chat_xxx",
      "sender_id": "user_xxx"
    }
  }
}
```

## Result Record

```json
{
  "id": "res_feishu_example",
  "ts": "2026-01-01T00:00:30Z",
  "from": "codex",
  "to": "feishu",
  "kind": "result",
  "type": "result",
  "request_id": "req_feishu_example",
  "project": "feishu-app-codex-bridge",
  "session": {
    "feishu": "chat_xxx",
    "codex": "codex-cli"
  },
  "result": "Codex answer",
  "text": "Codex answer",
  "meta": {
    "status": "done",
    "callback": {
      "status": "disabled"
    }
  }
}
```

## Timing Record

```json
{
  "schema": "feishu-app-codex.timing.v1",
  "ts": "2026-01-01T00:00:30Z",
  "status": "done",
  "event": {
    "event_id": "event_xxx",
    "request_id": "req_feishu_example"
  },
  "timings_ms": {
    "placeholder_sent": 900,
    "codex_first_output": 12000,
    "feishu_first_update": 13000,
    "total": 30000
  },
  "counters": {
    "codex_json_events": 8,
    "feishu_updates": 3,
    "feishu_progress_updates": 2
  }
}
```

## State Snapshot

```json
{
  "version": 1,
  "updated_at": "2026-01-01T00:00:30Z",
  "projects": {
    "feishu-app-codex-bridge": {
      "path": "feishu-app-codex-bridge",
      "agents": {
        "feishu": {
          "session": "chat_xxx",
          "identity": "user_xxx",
          "updated_at": "2026-01-01T00:00:30Z",
          "meta": {
            "last_event_id": "event_xxx"
          }
        },
        "codex": {
          "session": "codex-cli",
          "identity": "codex-cli",
          "updated_at": "2026-01-01T00:00:30Z",
          "meta": {}
        }
      }
    }
  }
}
```


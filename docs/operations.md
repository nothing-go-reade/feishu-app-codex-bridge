# Operations

## Start

```bash
python3 bin/feishu_app_codex_bridge.py start
```

## Stop

```bash
python3 bin/feishu_app_codex_bridge.py stop
```

## Status

```bash
python3 bin/feishu_app_codex_bridge.py status
```

## Healthcheck

```bash
python3 bin/feishu_app_codex_bridge.py healthcheck --format json
```

Healthy means:

- Bridge pid exists and process is alive.
- `lark-cli event status` says bus is running.
- Active consumers is exactly `1`.
- Dropped event count is `0`.

## Watch

```bash
python3 bin/feishu_app_codex_bridge.py watch --interval 30
```

The watch loop can restart the bridge when unhealthy.

For production-like local usage, run the watch command from a user-level process manager such as macOS `launchd`, systemd user services, or a terminal multiplexer.

## Logs

```text
var/bridge.log
logs/events.ndjson
logs/timings.ndjson
logs/watch.ndjson
```

## Runtime Cleanup

Runtime files are intentionally ignored by Git. To reset a local dev environment:

```bash
python3 bin/feishu_app_codex_bridge.py stop
rm -f var/bridge.sqlite3 var/bridge.sqlite3-* var/bridge.pid var/bridge.log
rm -f inbox/*.ndjson logs/*.ndjson state/session-map.json
python3 bin/feishu_app_codex_bridge.py init
```

Do not run this cleanup on a directory that contains audit records you need to keep.


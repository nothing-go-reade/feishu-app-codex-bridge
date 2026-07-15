# Feishu Codex Bridge

> 本文档整合自 Bridge 目录下原有 Markdown 文档，作为唯一维护入口。运行态目录 `inbox/`、`logs/`、`state/`、`var/` 不纳入文档清理。

## 文档索引

- `README.md`

- `FEISHU_CODEX_BRIDGE_FULL_REPORT.md`

- `OBSERVABILITY_LAYER1_2026-05-19.md`

- `OPTIMIZATION_FULL_SUMMARY_2026-05-19.md`

- `STABILITY_LAYER2_2026-05-19.md`

- `TODO_2026-05-19.md`

- `UI_LAYER3_2026-05-19.md`

- `docs/raw_dify_bridge_final_design.md`


---

## 来源：`README.md`

# Feishu/Lark CLI-Codex Bridge

This directory contains a user-started Feishu/Lark CLI <-> Codex bridge. It does not install a launchd service or other permanent daemon.

## What It Does

- Runs `lark-cli event consume im.message.receive_v1` as the Feishu/Lark message inlet.
- Stores event dedupe, message history, and response state in `var/bridge.sqlite3`.
- Mirrors Feishu/Codex request and result records into `inbox/`, `logs/`, and `state/` using the same `agent-bridge.v1` field shape as the Claude Code/Codex bridge.
- Calls `codex exec --json` for each incoming message.
- Replies with one placeholder Feishu/Lark message, then keeps editing that same message with accumulated output.
- Supports a background process with `start`, `status`, `stop`, `healthcheck`, and `watch`; logs go to `var/bridge.log`.
- Records per-message timing diagnostics in `logs/timings.ndjson` and watch/self-recovery checks in `logs/watch.ndjson`.

## Project Layout

```text
<bridge-root>/
├── README.md
├── bin/
│   └── feishu_codex_bridge.py
├── inbox/
│   ├── feishu-to-codex.ndjson
│   └── codex-to-feishu.ndjson
├── logs/
│   ├── events.ndjson
│   ├── watch.ndjson
│   └── timings.ndjson
├── state/
│   └── session-map.json
└── var/
    ├── bridge.log
    ├── bridge.pid
    └── bridge.sqlite3
```

## Commands

Run in the foreground:

```bash
python3 bin/feishu_codex_bridge.py run
```

Run with direct chunk messages instead of editing one message:

```bash
python3 bin/feishu_codex_bridge.py run --output-mode direct_chunks
```

Start without installing a service:

```bash
python3 bin/feishu_codex_bridge.py start
```

Check status:

```bash
python3 bin/feishu_codex_bridge.py status
```

Check structured health:

```bash
python3 bin/feishu_codex_bridge.py healthcheck --format json
```

Run one health watch pass without restarting:

```bash
python3 bin/feishu_codex_bridge.py watch --once --no-restart --verbose
```

Run continuous self-recovery watch:

```bash
python3 bin/feishu_codex_bridge.py watch --interval 30
```

Stop:

```bash
python3 bin/feishu_codex_bridge.py stop
```

Backfill existing SQLite history into the isolated Feishu bridge memory:

```bash
python3 bin/feishu_codex_bridge.py sync-agent-bridge
```

Append to an existing bot text message:

```bash
python3 bin/feishu_codex_bridge.py append \
  --message-id om_xxx \
  --text "追加内容"
```

## Streaming Model

Feishu/Lark message editing is replacement-based at the API layer. The bridge provides append semantics by keeping a local buffer:

```text
buffer += codex_chunk
PUT /open-apis/im/v1/messages/<reply_message_id> with full buffer
```

`codex exec --json` may emit true delta events in some modes. When it only emits a final `agent_message`, the bridge slices the final text into chunks and updates Feishu/Lark in short intervals so the client still sees a growing message.

`--output-mode direct_chunks` skips message editing and sends each flushed chunk as a new Feishu/Lark text message. It is simpler and works when editing fails, but it can be noisy for long answers.

## Agent Bridge Memory

Feishu memory is isolated under:

```text
<bridge-root>/
├── bin/
│   └── feishu_codex_bridge.py
├── inbox/
│   ├── feishu-to-codex.ndjson
│   └── codex-to-feishu.ndjson
├── logs/
│   └── events.ndjson
└── state/
    └── session-map.json
```

The bridge does not write to the existing Claude Code/Codex files in the parent `agent-bridge` directory. It only creates and appends files inside `feishu-codex`.

The shared schema for root `agent-bridge` and this Feishu subproject is documented in:

```text
<agent-bridge-root>/AGENT_BRIDGE_UNIFIED_SPEC.md
```

## Observability

The first observability layer records per-message timing diagnostics without changing the bridge behavior.

Timing file:

```text
<bridge-root>/logs/timings.ndjson
```

Design note:

```text
<bridge-root>/OBSERVABILITY_LAYER1_2026-05-19.md
```

Each timing record captures placeholder latency, Codex CLI startup/output timings, Feishu first update timing, total time, update counts, and whether output came from true delta or final-message chunking.

Stability/watch note:

```text
<bridge-root>/STABILITY_LAYER2_2026-05-19.md
```

UI/latency note:

```text
<bridge-root>/UI_LAYER3_2026-05-19.md
```

## Useful Environment Variables

- `LARK_CLI`: override the lark-cli command. Example: `LARK_CLI="node /path/to/run.js"`.
- `CODEX_CMD`: override the Codex command. Default: `codex`.
- `AGENT_BRIDGE_DIR` or `AGENT_BRIDGE_ROOT`: override the desktop agent-bridge root.
- `BRIDGE_FAKE_CODEX`: bypass Codex and stream this text. Useful for testing Feishu output without model calls.


---

## 来源：`FEISHU_CODEX_BRIDGE_FULL_REPORT.md`

# Feishu/Lark CLI - Codex Bridge 完整交付记录

生成日期：2026-05-18
位置：`<bridge-root>`

## 1. 当前结论

Feishu/Lark bot 到 Codex CLI 的本地桥接已经完成并通过实测。当前只保留一个正式入口：

```bash
cd <bridge-root>
python3 bin/feishu_codex_bridge.py status
```

旧工作区 `<codex-workspace>` 已删除，不再承担任何运行职责。

当前状态：

- bridge pid：`27088`
- event bus：running
- active consumers：`1`
- received events：`5`
- dropped events：`0`
- 日志：`<bridge-root>/var/bridge.log`
- 数据库：`<bridge-root>/var/bridge.sqlite3`

## 2. 目标链路

整体链路如下：

```text
Feishu/Lark bot
  -> lark-cli event consume im.message.receive_v1
  -> bin/feishu_codex_bridge.py
  -> codex exec --json
  -> SQLite + agent-bridge NDJSON/state 记忆
  -> lark-cli Feishu message reply/update
  -> Feishu/Lark bot
```

目标能力：

- 本地常驻监听 Feishu/Lark bot 消息。
- 收到消息后调用 Codex CLI。
- 给飞书消息先回复 placeholder，再用 PUT 持续更新同一条消息，形成流式输出体验。
- 把 request/result 同步写入 `agent-bridge.v1` 风格的文件记忆。
- 和既有 Claude Code/Codex bridge 保持同一套目录语义和数据结构。
- Feishu 子项目和根 Claude/Codex bridge 文件互不污染。

## 3. 项目目录

根目录：

```text
<agent-bridge-root>/
├── AGENT_BRIDGE_UNIFIED_SPEC.md
├── inbox/
│   ├── claude-to-codex.ndjson
│   └── codex-to-claude.ndjson
├── logs/
│   └── events.ndjson
├── state/
│   └── session-map.json
└── feishu-codex/
```

Feishu 子项目：

```text
<bridge-root>/
├── README.md
├── FEISHU_CODEX_BRIDGE_FULL_REPORT.md
├── bin/
│   └── feishu_codex_bridge.py
├── inbox/
│   ├── feishu-to-codex.ndjson
│   └── codex-to-feishu.ndjson
├── logs/
│   └── events.ndjson
├── state/
│   └── session-map.json
└── var/
    ├── bridge.log
    ├── bridge.pid
    ├── bridge.sqlite3
    ├── bridge.sqlite3-shm
    └── bridge.sqlite3-wal
```

说明：

- `bin/` 放唯一实现脚本。
- `inbox/` 放方向性消息队列。
- `logs/` 放同目录全量审计流水。
- `state/` 放当前 session/project/agent 快照。
- `var/` 放运行时文件，不作为跨 Agent 协议。

## 4. 统一协议

统一规范文件：

```text
<agent-bridge-root>/AGENT_BRIDGE_UNIFIED_SPEC.md
```

根 `agent-bridge` 和 `feishu-codex` 子项目都遵循同一语义：

- `inbox/*.ndjson`：按方向追加事件。
- `logs/events.ndjson`：同目录内审计流水，追加同一种事件对象。
- `state/session-map.json`：统一使用 `version / updated_at / projects / agents` 结构。

### 4.1 request 事件

新写入 request 的主字段：

```json
{
  "id": "req_feishu_xxx",
  "ts": "2026-05-18T02:51:45Z",
  "from": "feishu",
  "to": "codex",
  "kind": "request",
  "type": "request",
  "request_id": "req_feishu_xxx",
  "project": "<bridge-root>",
  "session": {
    "feishu": "oc_xxx",
    "codex": "codex-cli"
  },
  "prompt": "用户消息",
  "text": "用户消息",
  "meta": {
    "protocol_version": "agent-bridge.v1",
    "source": "feishu-codex",
    "feishu": {
      "event_id": "xxx",
      "message_id": "om_xxx",
      "chat_id": "oc_xxx",
      "sender_id": "ou_xxx",
      "message_type": "text",
      "chat_type": "p2p"
    }
  }
}
```

### 4.2 result 事件

新写入 result 的主字段：

```json
{
  "id": "evt_20260518_xxx_codex_xxx",
  "ts": "2026-05-18T02:52:11Z",
  "from": "codex",
  "to": "feishu",
  "kind": "result",
  "type": "result",
  "request_id": "req_feishu_xxx",
  "project": "<bridge-root>",
  "session": {
    "feishu": "oc_xxx",
    "codex": "codex-cli"
  },
  "result": "Codex 回复",
  "text": "Codex 回复",
  "meta": {
    "protocol_version": "agent-bridge.v1",
    "source": "feishu-codex",
    "status": "done",
    "error": "",
    "feishu": {
      "event_id": "xxx",
      "incoming_message_id": "om_xxx",
      "reply_message_id": "om_xxx",
      "chat_id": "oc_xxx"
    }
  }
}
```

### 4.3 state 结构

`state/session-map.json` 统一结构：

```json
{
  "version": 1,
  "updated_at": "2026-05-18T02:52:11Z",
  "projects": {
    "<bridge-root>": {
      "path": "<bridge-root>",
      "agents": {
        "feishu": {
          "session": "oc_xxx",
          "identity": "ou_xxx",
          "updated_at": "2026-05-18T02:52:11Z",
          "meta": {
            "bridge": "feishu-codex",
            "feishu_chat_id": "oc_xxx",
            "last_event_id": "xxx",
            "last_incoming_message_id": "om_xxx",
            "last_reply_message_id": "om_xxx"
          }
        },
        "codex": {
          "session": "codex-cli",
          "identity": "codex-cli",
          "updated_at": "2026-05-18T02:52:11Z",
          "meta": {
            "bridge": "feishu-codex"
          }
        }
      }
    }
  }
}
```

## 5. 实现细节

脚本：

```text
<bridge-root>/bin/feishu_codex_bridge.py
```

主要模块：

- `State`：维护 SQLite 表 `events`、`messages`、`responses`。
- `AgentBridgeMirror`：把 Feishu/Codex request/result 镜像到 `inbox/`、`logs/`、`state/`。
- `FeishuClient`：封装 `lark-cli` 的 auth、event、send、reply、update 调用。
- `EventConsumer`：用 PTY 运行 `lark-cli event consume`，避免非 TTY 退出。
- `FeishuAppender`：用 PUT 更新同一条 Feishu bot 消息，形成增长式输出。
- `FeishuDirectSender`：可选 direct chunks 模式，按 chunk 新发消息。
- `run_codex_streaming`：调用 `codex exec --json --ephemeral --skip-git-repo-check --sandbox read-only`。

关键行为：

- 默认 `output_mode=edit_append`。
- 先创建一条 placeholder 回复：`Codex 正在思考...`
- Codex 有 delta 时直接追加；如果 Codex CLI 只给 final agent_message，则按文本切片模拟流式。
- 每次 flush 都用 Feishu API PUT 完整替换消息内容，本地 buffer 保证“增量追加”的视觉效果。
- 失败时会尝试把错误追加到 Feishu 消息，并在 SQLite/NDJSON 中记录失败状态。

## 6. lark-cli 细节

当前实际使用的 lark-cli 入口：

```text
<lark-cli-runtime>@larksuite/cli/scripts/run.js
```

运行方式：

```bash
node <lark-cli-runtime>@larksuite/cli/scripts/run.js event consume im.message.receive_v1 --as bot --quiet
```

原因：

- PATH 里的 `lark-cli` 可能是失效 symlink。
- 脚本会优先探测 `lark-cli --version`，失败后使用上述 cached Node CLI。
- Feishu reply/update 等 keychain 相关命令需要在非沙箱/授权环境执行。

## 7. 常用命令

状态：

```bash
cd <bridge-root>
python3 bin/feishu_codex_bridge.py status
```

启动：

```bash
python3 bin/feishu_codex_bridge.py start --output-mode edit_append
```

停止：

```bash
python3 bin/feishu_codex_bridge.py stop
```

前台运行：

```bash
python3 bin/feishu_codex_bridge.py run
```

同步 SQLite 到 agent-bridge 文件记忆：

```bash
python3 bin/feishu_codex_bridge.py sync-agent-bridge
```

追加已有 bot 消息：

```bash
python3 bin/feishu_codex_bridge.py append --message-id om_xxx --text "追加内容"
```

## 8. 当前数据统计

最后一次完整检查结果：

- `inbox/feishu-to-codex.ndjson`：6 行，有效 JSON。
- `inbox/codex-to-feishu.ndjson`：6 行，有效 JSON。
- `logs/events.ndjson`：12 行，有效 JSON。
- `state/session-map.json`：符合 `projects/agents` schema。
- SQLite `events`：6 行。
- SQLite `messages`：12 行。
- SQLite `responses`：6 行。
- 根目录 `agent-bridge` 的 Claude/Codex 文件没有混入 Feishu 记录。

最新 request/result：

- request id：`req_feishu_b0da0621e985b3f00f785e70883a7e1a`
- request 时间：`2026-05-18T02:51:45Z`
- result 时间：`2026-05-18T02:52:11Z`
- result status：`done`
- session：`{"feishu":"oc_xxx","codex":"codex-cli"}`

## 9. 已验证功能

已完成实测：

- Feishu bot 收到用户消息。
- 本地 `lark-cli event consume` 收到 `im.message.receive_v1`。
- Python bridge 去重并写入 SQLite。
- Python bridge 写入 `feishu-to-codex.ndjson` 和 `logs/events.ndjson`。
- Python bridge 调用 `codex exec --json`。
- Feishu bot 先显示 placeholder。
- Feishu bot 消息被多次 PUT 更新，形成流式输出。
- Codex 最终结果写入 `codex-to-feishu.ndjson` 和 `logs/events.ndjson`。
- `state/session-map.json` 更新到统一 `projects/agents` 结构。
- `status` 显示 event bus 正常，active consumers 为 1，dropped 为 0。

## 10. 隔离策略

Feishu 子项目只写：

```text
<bridge-root>/inbox/
<bridge-root>/logs/
<bridge-root>/state/
<bridge-root>/var/
```

不会写：

```text
<agent-bridge-root>/inbox/claude-to-codex.ndjson
<agent-bridge-root>/inbox/codex-to-claude.ndjson
<agent-bridge-root>/logs/events.ndjson
<agent-bridge-root>/state/session-map.json
```

根目录 Claude/Codex bridge 和 Feishu/Codex bridge 使用相同协议，但数据隔离。

## 11. 清理记录

已删除旧工作区：

```text
<codex-workspace>
```

已确认该旧目录不再存在。后续不要再从旧路径启动或维护脚本。

全局 skill 已更新 canonical 路径：

```text
<codex-home>
```

## 12. 注意事项

- 当前不是 launchd/service 安装，而是本地用户启动的常驻进程。
- `var/bridge.pid` 是当前后台进程 pid。
- 机器重启后需要重新执行 `python3 bin/feishu_codex_bridge.py start`，除非后续明确安装 launchd。
- Feishu 消息编辑有时间窗口限制；旧消息超过可编辑窗口时，append/update 会失败，需要新发消息。
- Codex CLI 有时不输出真实 token delta，bridge 会对 final text 切片模拟流式。
- `lark-cli event consume` 需要 PTY 运行，否则可能立即退出。
- 如果将来继续接入更多 Agent，应复用 `AGENT_BRIDGE_UNIFIED_SPEC.md` 中的目录和字段规范。

## 13. 打包与备份

本报告已放在 Feishu 子项目内，会随项目一起打包。

建议压缩包命名：

```text
<bridge-root>-bridge-backup-20260518.zip
```

压缩包应包含：

- bridge 脚本
- README
- 本完整报告
- inbox/logs/state 记忆文件
- var 中的 sqlite/log/pid 运行状态


---

## 来源：`OBSERVABILITY_LAYER1_2026-05-19.md`

# Feishu/Codex Bridge 第一层观测埋点

日期：2026-05-19

## 目标

在不改变当前 Feishu/Codex 主链路的前提下，增加阶段耗时观测，先回答一个问题：

```text
用户在 Feishu 发消息后，慢在哪里？
```

当前运行链路仍保持不变：

```text
Feishu bot -> lark-cli event -> bridge -> codex exec -> Feishu reply/update
```

本次只新增观测文件：

```text
<bridge-root>/logs/timings.ndjson
```

## 记录时机

每处理完一个 Feishu 事件，bridge 会向 `logs/timings.ndjson` 追加一行 JSON。

成功和失败都会记录：

- 成功：`status = "done"`
- 失败：`status = "failed"`

该文件只用于诊断，不参与业务决策，不影响 `inbox/`、`logs/events.ndjson`、`state/session-map.json` 的协议结构。

## Timing Schema

每一行是一个 JSON 对象：

```json
{
  "schema": "feishu-codex.timing.v1",
  "ts": "2026-05-19T01:30:00Z",
  "started_at": "2026-05-19T01:29:40Z",
  "status": "done",
  "event": {
    "event_id": "xxx",
    "request_id": "req_feishu_xxx",
    "incoming_message_id": "om_xxx",
    "reply_message_id": "om_xxx",
    "chat_id": "oc_xxx",
    "sender_id": "ou_xxx"
  },
  "output_mode": "edit_append",
  "timings_ms": {
    "event_accepted": 0,
    "request_mirrored": 5,
    "sqlite_user_message_saved": 8,
    "placeholder_start": 9,
    "placeholder_sent": 850,
    "sqlite_response_running_saved": 860,
    "prompt_built": 861,
    "codex_start": 862,
    "codex_process_started": 900,
    "codex_first_json_event": 12000,
    "codex_completed_text": 18000,
    "codex_process_exit": 18100,
    "codex_first_output": 18150,
    "feishu_first_update": 19000,
    "codex_done": 20500,
    "final_update": 20500,
    "sqlite_assistant_message_saved": 20510,
    "sqlite_response_done_saved": 20515,
    "result_mirrored": 20520,
    "total": 20520
  },
  "counters": {
    "codex_json_events": 12,
    "codex_delta_chunks": 0,
    "codex_completed_messages": 1,
    "codex_simulated_chunks": 3,
    "feishu_updates": 3
  },
  "output_chars": 42,
  "error": "",
  "meta": {
    "placeholder_method": "reply",
    "codex_streaming_source": "final_message_chunked",
    "last_update_chars": 42
  }
}
```

## 关键字段解释

### 1. `placeholder_sent`

Feishu 收到用户消息后，bridge 发出第一条 placeholder 的时间。

目标：

```text
1-2 秒内完成
```

如果这个值过大，优先排查：

- `lark-cli api POST /reply`
- Feishu API 延迟
- keychain/auth 权限

### 2. `codex_start` 和 `codex_process_started`

bridge 准备启动和实际启动 Codex CLI 的时间。

如果这里慢，优先排查：

- Codex CLI 启动开销
- 环境变量或 MCP 初始化
- 本机负载

### 3. `codex_first_json_event`

Codex CLI 第一次输出 JSON event 的时间。

如果这里很慢，说明等待主要发生在 Codex CLI 或模型侧。

### 4. `codex_completed_text`

Codex CLI 输出最终 agent message 的时间。

如果 `codex_delta_chunks = 0` 但 `codex_completed_messages = 1`，说明当前不是 token 级真流式，而是 final 后由 bridge 切片模拟流式。

### 5. `codex_first_output`

第一段可显示给 Feishu 的 Codex 内容出现时间。

它可能来自：

- 真实 delta：`meta.codex_streaming_source = "delta"`
- final message 切片：`meta.codex_streaming_source = "final_message_chunked"`
- fake test：`meta.codex_streaming_source = "fake"`

### 6. `feishu_first_update`

第一段 Codex 内容成功 PUT 到 Feishu 消息的时间。

如果 `codex_first_output` 很早，但 `feishu_first_update` 很晚，说明瓶颈在 Feishu update API 或 update 节流。

### 7. `final_update`

最终内容 flush 完成时间。

`total` 和 `final_update` 接近，说明大部分处理都在回复生成和消息更新中完成。

## 简单分析命令

查看最近 timing：

```bash
cd <bridge-root>
tail -n 5 logs/timings.ndjson
```

提取最近一条关键耗时：

```bash
python3 -c 'import json,pathlib; rows=[json.loads(x) for x in pathlib.Path("logs/timings.ndjson").read_text().splitlines() if x.strip()]; r=rows[-1]; print(r["status"], r["event"]["request_id"], r["timings_ms"], r["counters"], r["meta"])'
```

判断是否真流式：

```bash
python3 -c 'import json,pathlib; rows=[json.loads(x) for x in pathlib.Path("logs/timings.ndjson").read_text().splitlines() if x.strip()]; print(rows[-1]["meta"].get("codex_streaming_source"), rows[-1]["counters"])'
```

## 验收标准

下一次 Feishu 消息处理完成后：

- `logs/timings.ndjson` 新增 1 行。
- JSON 可解析。
- `timings_ms.total` 有值。
- `placeholder_sent`、`codex_start`、`codex_first_output`、`feishu_first_update`、`final_update` 至少能覆盖成功路径。
- `counters.feishu_updates` 能反映 Feishu PUT 更新次数。
- bridge 状态仍为 running，active consumers 仍为 1，dropped 为 0。

## 后续动作

完成 3-5 条真实消息采样后，结合第二层 `healthcheck/watch` 和第三层 UI timing 字段继续定位：

- 如果 `placeholder_sent` 慢，优先优化 Feishu reply API 或 lark-cli auth/keychain。
- 如果 `codex_first_json_event` 慢，优先优化 Codex CLI 启动和模型调用。
- 如果 `codex_delta_chunks = 0`，说明当前主要是 final 后模拟流式，需要调研 Codex CLI 是否支持更稳定 delta。
- 如果 `feishu_first_update` 慢，优先优化 Feishu PUT 更新频率和消息类型。


---

## 来源：`OPTIMIZATION_FULL_SUMMARY_2026-05-19.md`

# Feishu/Codex Bridge 优化完整总结

日期：2026-05-19
项目目录：`<bridge-root>`

## 1. 当前结论

本次优化已经完成并通过检查。

当前实时健康状态：

```text
bridge pid: 25779 running
event bus pid: 25798 running
active consumers: 1
dropped: 0
healthcheck: healthy
issues: []
```

这表示：

- Feishu/Lark CLI event bus 正常运行。
- bridge 后台进程正常运行。
- 当前只有 1 个 `im.message.receive_v1` consumer，没有重复 consumer。
- 当前没有 dropped 事件。
- Feishu bot 到 Codex bridge 的入口链路处于可用状态。

## 2. 总体链路

当前 bridge 的工作链路如下：

```text
Feishu bot
  -> lark-cli event consume im.message.receive_v1
  -> feishu_codex_bridge.py
  -> codex exec --json
  -> Feishu message placeholder / PUT update
  -> agent-bridge/feishu-codex 本地记忆与日志
```

核心设计原则：

- 所有 Feishu/Codex 相关文件统一放在 `agent-bridge/feishu-codex`。
- 不影响 parent `agent-bridge` 里已有的 Claude Code/Codex bridge 文件。
- Feishu 子项目内部同样使用 `inbox/`、`logs/`、`state/` 的结构，保持和原 agent-bridge 语义一致。
- 默认不安装系统常驻服务，先提供可被 launchd 调用的稳定入口。
- 启动、停止、状态、健康检查、watch 自恢复都通过同一个入口脚本管理。

## 3. 项目结构

```text
<bridge-root>/
├── bin/
│   └── feishu_codex_bridge.py
├── inbox/
│   ├── feishu-to-codex.ndjson
│   └── codex-to-feishu.ndjson
├── logs/
│   ├── events.ndjson
│   ├── timings.ndjson
│   └── watch.ndjson
├── state/
│   └── session-map.json
├── var/
│   ├── bridge.log
│   ├── bridge.pid
│   ├── bridge.sqlite3
│   ├── bridge.sqlite3-shm
│   └── bridge.sqlite3-wal
├── README.md
├── TODO_2026-05-19.md
├── FEISHU_CODEX_BRIDGE_FULL_REPORT.md
├── OBSERVABILITY_LAYER1_2026-05-19.md
├── STABILITY_LAYER2_2026-05-19.md
├── UI_LAYER3_2026-05-19.md
└── OPTIMIZATION_FULL_SUMMARY_2026-05-19.md
```

## 4. 第一层优化：观测埋点

目标：先看清楚慢在哪里，而不是凭感觉改。

新增文件：

```text
logs/timings.ndjson
OBSERVABILITY_LAYER1_2026-05-19.md
```

每处理完一条 Feishu 消息，bridge 会向 `logs/timings.ndjson` 写入一行 JSON。

记录内容包括：

- `event_id`
- `request_id`
- Feishu incoming message id
- Feishu reply message id
- output mode
- 总耗时
- placeholder 发送耗时
- Codex CLI 启动时间
- Codex 第一条 JSON event 时间
- Codex 第一段可展示输出时间
- Feishu 第一次内容更新时间
- final update 时间
- Feishu PUT 更新次数
- Codex JSON event 数量
- Codex delta chunk 数量
- final-message chunk 模拟流式次数
- 是否触发 progress update

重要 timing mark：

```text
event_accepted
request_mirrored
sqlite_user_message_saved
placeholder_start
placeholder_sent
sqlite_response_running_saved
prompt_built
codex_start
codex_process_started
codex_first_json_event
codex_completed_text
codex_first_output
feishu_first_update
codex_process_exit
codex_done
final_update
sqlite_assistant_message_saved
sqlite_response_done_saved
result_mirrored
total
```

这样后续可以明确区分：

- 是 lark-cli 收消息慢。
- 是 Feishu reply placeholder 慢。
- 是 Codex CLI 启动慢。
- 是模型生成慢。
- 是 Codex CLI 没有真实 delta。
- 是 Feishu PUT 更新慢。

当前说明：

- 本次重启后还没有新的 Feishu 消息，所以 `logs/timings.ndjson` 当前可能为空。
- 下一条真实 Feishu 消息完成后，会自动产生 timing 记录。

## 5. 第二层优化：稳定性与自恢复入口

目标：让 bridge 可以被持续监控，并在异常时安全恢复。

新增能力：

```bash
python3 bin/feishu_codex_bridge.py healthcheck
python3 bin/feishu_codex_bridge.py healthcheck --format json
python3 bin/feishu_codex_bridge.py watch --once --no-restart --verbose
python3 bin/feishu_codex_bridge.py watch --interval 30
```

新增文件：

```text
logs/watch.ndjson
STABILITY_LAYER2_2026-05-19.md
```

健康标准：

```text
bridge process is running
lark event bus is running
active consumers == 1
dropped == 0
```

`healthcheck --format json` 会返回：

```json
{
  "schema": "feishu-codex.health.v1",
  "healthy": true,
  "issues": [],
  "bridge": {
    "pid": 25779,
    "pid_file_exists": true,
    "running": true
  },
  "event_bus": {
    "bus_running": true,
    "bus_pid": 25798,
    "active_consumers": 1,
    "received": 0,
    "dropped": 0
  }
}
```

`watch` 行为：

- 健康时可保持安静，也可以通过 `--verbose` 写入健康记录。
- 不健康时写入 `logs/watch.ndjson`。
- 未加 `--no-restart` 时，会执行安全恢复：

```text
stop -> start -> healthcheck -> record restart_done
```

这为后续 macOS launchd user agent 做好了入口：

```bash
python3 <bridge-root>/bin/feishu_codex_bridge.py watch --interval 30
```

## 6. 第三层优化：Feishu UI 与等待体验

目标：用户在 Feishu bot 里发消息后，不再只看到一个长时间静止的占位回复。

新增能力：

```bash
--progress-interval 8.0
--disable-progress
```

默认 placeholder 从：

```text
Codex 正在思考...
```

调整为：

```text
已收到，正在调用 Codex...
```

如果 Codex 长时间没有任何可展示输出，bridge 会定期更新 placeholder：

```text
已收到，正在调用 Codex...

已等待约 8s，Codex 仍在处理。
```

重要点：

- 这个 progress update 只在还没有真实 Codex 输出时触发。
- 一旦 Codex 输出出现，消息内容会切换为真实回答。
- progress update 不进入最终回答 buffer，不污染最终答案。
- timing 会记录 `feishu_progress_updates` 和 `feishu_first_progress_update`。

这样可以区分：

- Feishu bot 是否及时收到请求。
- Codex 是否仍在处理。
- 用户看到的等待是否有反馈。

## 7. 流式输出策略

当前默认模式：

```bash
--output-mode edit_append
```

逻辑：

```text
发送一条 placeholder reply
维护本地 buffer
Codex 有新内容时 buffer += chunk
PUT /open-apis/im/v1/messages/<reply_message_id> 写入完整 buffer
```

Feishu/Lark API 的 message update 本质是替换，不是 append。bridge 通过本地 buffer 实现用户看到的“增量追加”。

如果 Codex CLI 输出真实 delta：

```text
收到 delta -> 立即追加到 buffer -> 按时间/句末节流更新 Feishu
```

如果 Codex CLI 只输出最终 `agent_message`：

```text
等待 final text -> 按 chunk 切片 -> 模拟流式更新 Feishu
```

备用模式：

```bash
--output-mode direct_chunks
```

这个模式不编辑同一条消息，而是把每个 chunk 作为新 Feishu 消息发送。它更稳但更吵，所以默认不使用。

## 8. agent-bridge 记忆与数据结构

SQLite 本地状态：

```text
var/bridge.sqlite3
```

主要表：

- `events`
- `messages`
- `responses`

agent-bridge mirror 文件：

```text
inbox/feishu-to-codex.ndjson
inbox/codex-to-feishu.ndjson
logs/events.ndjson
state/session-map.json
```

语义：

- `inbox/feishu-to-codex.ndjson`：Feishu 用户请求。
- `inbox/codex-to-feishu.ndjson`：Codex 回复结果。
- `logs/events.ndjson`：request/result 事件流。
- `state/session-map.json`：会话映射和 agent 信息。

命名隔离：

```text
<bridge-root>
```

不会写入 parent `agent-bridge` 现有 Claude Code/Codex 文件，避免影响之前已经打通的桥接链路。

## 9. 常用命令

启动：

```bash
cd <bridge-root>
python3 bin/feishu_codex_bridge.py start
```

状态：

```bash
python3 bin/feishu_codex_bridge.py status
```

健康检查：

```bash
python3 bin/feishu_codex_bridge.py healthcheck --format json
```

单次 watch：

```bash
python3 bin/feishu_codex_bridge.py watch --once --no-restart --verbose
```

持续自恢复 watch：

```bash
python3 bin/feishu_codex_bridge.py watch --interval 30
```

停止：

```bash
python3 bin/feishu_codex_bridge.py stop
```

同步历史到 agent-bridge：

```bash
python3 bin/feishu_codex_bridge.py sync-agent-bridge
```

## 10. 本次检查结果

已执行并通过：

```bash
python3 -B -c 'import ast, pathlib; ast.parse(pathlib.Path("bin/feishu_codex_bridge.py").read_text()); print("syntax ok")'
python3 bin/feishu_codex_bridge.py status
python3 bin/feishu_codex_bridge.py healthcheck --format json
python3 bin/feishu_codex_bridge.py watch --once --no-restart --verbose
```

结果：

```text
syntax ok
bridge pid: 25779 running
event bus: running
active consumers: 1
dropped: 0
healthcheck: healthy
watch: healthy
```

## 11. 真实 Feishu 消息样本

报告生成后，bridge 又处理了一条真实 Feishu 消息：

```text
用户消息：你还在吧
event_id: 77bdb699dadcd23bac130723fb3aaa3b
reply_message_id: om_xxx
最终回复：在的，我还在。
```

这条消息已经写入：

```text
logs/timings.ndjson
var/bridge.log
inbox/feishu-to-codex.ndjson
inbox/codex-to-feishu.ndjson
logs/events.ndjson
state/session-map.json
```

关键 timing 数据：

```text
placeholder_sent: 1203 ms
codex_first_json_event: 1619 ms
codex_completed_text: 15429 ms
codex_first_output: 17262 ms
feishu_first_update: 18136 ms
total: 18392 ms
```

关键 counters：

```text
codex_json_events: 8
codex_delta_chunks: 0
codex_completed_messages: 1
codex_simulated_chunks: 1
feishu_updates: 1
feishu_progress_updates: 0
codex_streaming_source: final_message_chunked
```

这个样本说明：

- Feishu 收消息、placeholder 回复和最终 PUT 更新链路正常。
- 当前这次 Codex CLI 没有输出真实 delta，主要是最终 `agent_message` 后模拟流式。
- 本次回答较短，所以只产生了 1 次 Feishu 内容更新。
- 第一层 timing、第二层 healthcheck、第三层 placeholder 文案都已经在真实消息上跑通。

## 12. 已知边界

1. 当前还没有安装 launchd user agent。

   本次只完成了可被 launchd 调用的 `watch --interval 30` 自恢复入口。后续如果要 macOS 登录后自动常驻，应创建 launchd plist，让它运行 watch，而不是只运行 start。

2. 当前 Codex CLI 不一定提供稳定 token delta。

   bridge 已兼容两种情况：

   - 有 delta：直接真流式更新。
   - 无 delta：final 后切片模拟流式。

3. `logs/timings.ndjson` 需要真实消息触发。

   当前重启后如果没有新 Feishu 消息，这个文件为空是正常的。

4. Feishu message PUT 是替换语义。

   所谓追加，是 bridge 在本地维护完整 buffer 后反复 PUT 全量文本实现。

## 13. 后续建议

下一步推荐顺序：

1. 采样 3-5 条真实 Feishu 消息，查看 `logs/timings.ndjson`。
2. 判断瓶颈是 Codex CLI 启动、模型生成、Feishu 更新，还是缺少真实 delta。
3. 将 `watch --interval 30` 接入 launchd user agent，实现 macOS 登录后默认自恢复常驻。
4. 如果 Codex CLI 启动成本很高，再考虑长期 worker 或会话复用。
5. 如果 Feishu UI 还不够清晰，再考虑 interactive card 或分阶段消息。

## 14. 打包内容说明

本次备份 zip 应包含：

- bridge 入口脚本。
- README 和完整设计文档。
- 三层优化说明文档。
- TODO 与后续规划。
- inbox/logs/state 的 agent-bridge 记忆文件。
- var 下的 SQLite 状态、pid 和运行日志。

该包可以作为后续继续优化、迁移或排查的完整上下文。


---

## 来源：`STABILITY_LAYER2_2026-05-19.md`

# Feishu/Codex Bridge 第二层稳定性优化

日期：2026-05-19

## 目标

在不改变主链路的前提下，为 bridge 增加健康检查和自恢复入口。

核心目标：

- 能判断 bridge 进程是否存在。
- 能判断 lark-cli event bus 是否 running。
- 能判断 active consumers 是否正好为 1。
- 能判断 dropped 是否异常。
- 能在不健康时执行安全重启。
- 能把 watch 行为记录到项目目录内。

## 新增文件

```text
<bridge-root>/logs/watch.ndjson
```

每行是一个 JSON 对象，schema 为：

```json
{
  "schema": "feishu-codex.watch.v1",
  "ts": "2026-05-19T09:18:47Z",
  "action": "healthy",
  "healthy": true,
  "issues": [],
  "detail": "",
  "bridge": {
    "pid": 25779,
    "pid_file_exists": true,
    "running": true
  },
  "event_bus": {
    "bus_running": true,
    "bus_pid": 25798,
    "active_consumers": 1,
    "received": 0,
    "dropped": 0
  }
}
```

## 新增命令

### 1. 健康检查

```bash
cd <bridge-root>
python3 bin/feishu_codex_bridge.py healthcheck
```

JSON 输出：

```bash
python3 bin/feishu_codex_bridge.py healthcheck --format json
```

健康标准：

- `bridge.running = true`
- `event_bus.bus_running = true`
- `event_bus.active_consumers = 1`
- `event_bus.dropped = 0`

### 2. 单次 watch 检查

只检查，不重启：

```bash
python3 bin/feishu_codex_bridge.py watch --once --no-restart --verbose
```

检查并允许自动恢复：

```bash
python3 bin/feishu_codex_bridge.py watch --once
```

### 3. 持续 watch

```bash
python3 bin/feishu_codex_bridge.py watch --interval 30
```

行为：

- 健康时默认保持安静。
- 不健康时写入 `logs/watch.ndjson`。
- 不健康且未设置 `--no-restart` 时，执行 `stop` -> `start` 安全重启。
- 重启后再次 healthcheck，并记录 `restart_done`。

## 验收结果

已执行：

```bash
python3 -m py_compile bin/feishu_codex_bridge.py
python3 bin/feishu_codex_bridge.py healthcheck --format json
python3 bin/feishu_codex_bridge.py watch --once --no-restart --verbose
```

最近一次验证结果：

- bridge pid：`25779`
- event bus：running
- active consumers：`1`
- dropped：`0`
- healthcheck：healthy
- watch 单次检测：healthy

说明：pid 会随 bridge 重启变化，判断是否正常应以 `healthcheck` 输出中的
`healthy=true`、`active_consumers=1`、`dropped=0` 为准。

## 后续

这一层先提供手动 watch 和可被 launchd 调用的稳定入口。下一阶段如果要做 macOS 自启动，推荐让 launchd 运行 `watch --interval 30`，而不是直接只运行 `start`，这样可以获得自恢复能力。


---

## 来源：`TODO_2026-05-19.md`

# Feishu/Codex Bridge 明日优化清单

日期：2026-05-19

## 背景

当前 Feishu/Lark bot -> lark-cli -> Codex CLI -> Feishu bot 的本地常驻桥接已经跑通，并已统一迁移到：

```text
<bridge-root>
```

今日已完成：

- bridge 常驻运行，event bus 正常。
- Feishu 消息可以触发 Codex 回复。
- Feishu 端可以看到编辑式流式更新。
- request/result/log/state 已统一写入 `agent-bridge.v1` 结构。
- 完整报告和压缩包已生成。

## 1. 优化连接常驻性

目标：支持 mac 启动后默认自动建立 Feishu/Lark CLI -> Codex bridge 的常驻连接。

待办：

- 设计 macOS 启动默认常驻方案，优先考虑 `launchd` user agent。
- 提供 `install`、`uninstall`、`status`、`restart` 命令或脚本。
- 确认启动时工作目录固定为 `<bridge-root>`。
- 确认启动后能正确读取 `lark-cli` auth/keychain。
- 确认启动后 `lark-cli event consume im.message.receive_v1 --as bot --quiet` 能正常建立 event bus。
- 把启动日志写入 `var/bridge.log` 或单独 `var/launchd.log`。

验收标准：

- mac 登录后无需手动执行命令，bridge 自动常驻。
- `python3 bin/feishu_codex_bridge.py status` 显示 bridge running、event bus running、active consumers 为 1。

## 2. 优化任务稳定性和自恢复

目标：进程不存在、event bus 异常退出、consumer 掉线时，能自动恢复常驻任务。

待办：

- 增加 watch/healthcheck 机制，周期性检查：
  - `var/bridge.pid` 对应进程是否存在。
  - `lark-cli event status` 是否 running。
  - active consumers 是否为 1。
  - dropped 是否异常增长。
- 进程不存在时自动执行 `start`。
- event bus 存在但 consumer 异常时执行安全 restart。
- 避免重复启动多个 bridge 或多个 consumer。
- 记录自恢复事件到 `var/bridge.log`，必要时同步写入 `logs/events.ndjson` 的 `note` 事件。
- 增加锁文件或 pid 校验，防止并发启动。

验收标准：

- 手动 kill bridge 后，watch 能自动拉起。
- 手动 stop event bus 后，watch 能自动恢复。
- 多次恢复后仍保持 active consumers 为 1。

## 3. 优化 Feishu bot 和 Codex 的 UI/回复体验

目标：查清“问一个问题不能及时给出 Codex 答复”的原因，并尝试优化到更快、更清晰、更像流式。

当前观察：

- Feishu 端已经能看到 bot 回复被编辑更新。
- 有些问题需要较长时间才看到完整 Codex 答复。
- 当前 bridge 使用 placeholder + PUT 更新同一条消息。
- Codex CLI 有时只输出 final `agent_message`，bridge 会切片模拟流式，而不是真正 token 级流式。

待办：

- 分段记录各阶段耗时：
  - Feishu event 到达时间。
  - placeholder 发出时间。
  - Codex CLI 启动时间。
  - Codex 第一段输出时间。
  - Feishu 第一次 update 时间。
  - Codex 完成时间。
  - Feishu final update 时间。
- 判断慢点来自哪里：
  - lark-cli event 接收慢。
  - placeholder/reply API 慢。
  - Codex CLI 启动慢。
  - Codex 模型生成慢。
  - Codex CLI 没有真实 delta，只能 final 后模拟。
  - Feishu PUT update 频率或编辑限制导致慢。
- 优化 Feishu UI：
  - placeholder 里显示“已收到，正在调用 Codex...”。
  - 长任务定期更新“仍在处理”状态。
  - 失败时明确显示错误和下一步。
  - 可以考虑卡片消息，但先评估 API 支持和更新频率限制。
- 优化 Codex 输出：
  - 调研 `codex exec --json` 是否有更稳定的 delta 输出模式。
  - 如果没有真实 delta，评估是否换用更适合 streaming 的调用路径。
  - 保留 final 切片模拟作为 fallback。

验收标准：

- Feishu 用户发消息后，1-2 秒内能看到明确 placeholder。
- 如果 Codex 生成较慢，Feishu 每隔合理时间能看到状态进展。
- 能输出一份耗时分析，说明瓶颈在哪里。
- 能明确结论：真实流式是否可行；不可行时 fallback 行为是什么。

## 4. 相关文件

```text
<bridge-root>/bin/feishu_codex_bridge.py
<bridge-root>/README.md
<bridge-root>/FEISHU_CODEX_BRIDGE_FULL_REPORT.md
<agent-bridge-root>/AGENT_BRIDGE_UNIFIED_SPEC.md
```

## 5. 明日建议顺序

1. 先加耗时埋点，确认 UI/时延瓶颈。
2. 再做自恢复 healthcheck，保证不掉线。
3. 最后做 macOS launchd 自启动，把稳定性方案固化。

## 6. 进展记录

2026-05-19 已完成第一层观测埋点：

- 新增 `logs/timings.ndjson`，每条 Feishu 事件完成后追加一条 timing JSON。
- 新增 `OBSERVABILITY_LAYER1_2026-05-19.md`，记录 timing schema、字段含义和分析命令。
- bridge 已短重启，当前新进程会对后续 Feishu 消息记录耗时。
- 第二层 healthcheck/watch 已完成；后续等待 3-5 条真实消息样本，再分析 Codex 启动、模型生成、Feishu 更新等阶段瓶颈。

2026-05-19 已完成第二层稳定性入口：

- 新增 `healthcheck --format json`，结构化检查 bridge pid、event bus、active consumers、dropped。
- 新增 `watch --once` 和持续 `watch --interval 30`，可在不健康时执行安全 stop/start 恢复。
- 新增 `logs/watch.ndjson`，记录 watch 检查和恢复动作。
- 新增 `STABILITY_LAYER2_2026-05-19.md`。
- 已执行 `healthcheck` 和 `watch --once --no-restart --verbose`，结果 healthy。

2026-05-19 已完成第三层 UI/时延体验优化：

- 默认 placeholder 改为 `已收到，正在调用 Codex...`。
- 新增 `--progress-interval` 和 `--disable-progress`。
- Codex 长时间无输出时，会定期更新 Feishu 消息提示 `Codex 仍在处理`。
- timing 里新增 `feishu_progress_updates` 和 `feishu_first_progress_update`。
- 新增 `UI_LAYER3_2026-05-19.md`。
- 已重新启动 bridge 应用新参数，并通过 `status`、`healthcheck`、`watch --once --no-restart --verbose` 复核通过。


---

## 来源：`UI_LAYER3_2026-05-19.md`

# Feishu/Codex Bridge 第三层 UI 与时延体验优化

日期：2026-05-19

## 目标

在保持现有 Feishu 回复链路稳定的前提下，改善用户等待 Codex 答复时的体验。

这层优化不改变：

- Feishu event consume
- Codex CLI 调用
- agent-bridge request/result 写入
- SQLite 状态记录

只优化：

- placeholder 文案
- 长任务等待期间的进度提示
- timing 观测字段

## 已调整内容

### 1. 更清晰的 placeholder

默认 placeholder 从：

```text
Codex 正在思考...
```

调整为：

```text
已收到，正在调用 Codex...
```

目的：

- 明确告诉用户 bridge 已收到消息。
- 明确当前卡点是正在调用 Codex，而不是 Feishu bot 没响应。

### 2. 长任务进度更新

新增参数：

```bash
--progress-interval 8.0
--disable-progress
```

默认行为：

- Codex 还没有任何可展示输出时，每隔约 8 秒更新一次 placeholder。
- 文案示例：

```text
已收到，正在调用 Codex...

已等待约 16s，Codex 仍在处理。
```

目的：

- 避免用户看到一条静止 placeholder，以为 bot 卡住。
- 在 Codex CLI 或模型生成慢时，给用户稳定的“仍在工作”反馈。

### 3. timing 记录进度更新

`logs/timings.ndjson` 会记录：

```json
{
  "timings_ms": {
    "feishu_first_progress_update": 8000,
    "feishu_first_update": 18000
  },
  "counters": {
    "feishu_progress_updates": 2,
    "feishu_updates": 3
  },
  "meta": {
    "progress_updates": 2
  }
}
```

含义：

- `feishu_progress_updates`：等待 Codex 输出期间发出的进度提示次数。
- `feishu_updates`：真正 Codex 内容更新次数。
- `feishu_first_progress_update`：第一次“仍在处理”提示出现时间。
- `feishu_first_update`：第一段 Codex 内容更新到 Feishu 的时间。

## 为什么这样设计

当前已有证据显示：

- Feishu bot 能正常收消息。
- placeholder 可以及时发出。
- Codex CLI 有时不会输出 token 级 delta，只在最终 `agent_message` 出来后才有内容。
- bridge 会把 final text 切片模拟流式，但用户在 final text 出现前仍然会等待。

因此这层先做低风险体验优化：

- 不改变 Codex 调用方式。
- 不提高 Feishu PUT 频率到危险水平。
- 不切换卡片消息。
- 先通过 progress + timing 观察真实瓶颈。

## 验收方法

发送一条需要 Codex 思考超过 8 秒的 Feishu 消息。

预期：

1. 1-2 秒内看到：

```text
已收到，正在调用 Codex...
```

2. 如果 Codex 仍无输出，约 8 秒后看到：

```text
已等待约 8s，Codex 仍在处理。
```

3. Codex 内容出来后，消息被替换为真实回答，并继续按原有编辑式流式更新。

4. `logs/timings.ndjson` 追加一行记录。

## 回滚方式

如果不想显示进度提示，可启动时加：

```bash
python3 bin/feishu_codex_bridge.py start --disable-progress
```

如果要恢复旧 placeholder，可启动时指定：

```bash
python3 bin/feishu_codex_bridge.py start --placeholder "Codex 正在思考..."
```


---

## 来源：`docs/raw_dify_bridge_final_design.md`

# Http Bridge & Feishu/Lark CLI 本地私有化协同方案

## 目标

raw 表穿透替换链路里，Dify 网关可能在 15 分钟左右返回 503/504，导致 Java 侧拿不到稳定结果。为提高长 SQL 替换的兜底能力，本方案引入本地 Feishu/Lark CLI + Codex/Agent Bridge，将失败或超时的 Dify 审计任务投递到本地智能体处理，并通过回调接口回写业务系统。

核心目标：

- 替换 SQL 必须准确，不确定时返回空 `replacedSql`，不能误替换。
- Codex/Agent 只负责产出结构化结果，不直接承担业务回调。
- Bridge Python 父进程负责落盘、飞书兜底消息、可选回调。
- 所有结果都要有本地 NDJSON 审计记录，方便排查。

## 最优架构

### 1. Java 侧

Java xxl-job 扫描 `tg_dify_audit_log` 中超时或失败的 Dify 调用记录，组装完整提示词后发送到 Feishu 群，并 `@Lark-cli-Codex-CC-Connect`。

提示词需要包含：

- 审计 ID、资产记录 ID、docId、rawTableName、sceneType。
- Dify request JSON。
- Dify response JSON。
- 完整 system/user/assistant prompts。
- Bridge 回调参数 JSON。
- 严格输出 JSON schema。

推荐输出协议：

```json
{
  "replacedSql": "完整替换后的 SQL；无法安全替换时为空字符串",
  "analysis": "处理说明、失败原因或校验结论",
  "callback": {
    "auditLogId": 6,
    "replaceAssetId": 512,
    "docId": 751986,
    "rawTableName": "hdp_xxx.raw_table",
    "sceneType": 1,
    "callbackUrl": "http://127.0.0.1:{callback-port}/api/tg/raw/governance/dify-audit/callback",
    "source": "FEISHU_LARK_CLI",
    "tableOwnerName": "owner"
  }
}
```

`callback` 必须原样使用 Java 提示词中的 Bridge 回调参数 JSON。Bridge 解析结果时优先使用 `callback` 内的 `auditLogId`、`replaceAssetId`、`docId`、`rawTableName`、`sceneType`、`callbackUrl`、`tableOwnerName`，只有缺失时才回退到文本正则或元数据表映射，避免把 Dify 元数据中的源表误当作业务回调 raw 表。

### 2. Feishu/Lark CLI Bridge

Bridge 常驻在本地：

```text
<bridge-root>
```

核心脚本：

```text
<bridge-root>/bin/feishu_codex_bridge.py
```

Bridge 收到飞书消息后：

1. 回复占位消息。
2. 启动 `codex exec --json --sandbox read-only`。
3. 非阻塞读取 Codex 输出。
4. 当占位消息不可编辑时，每 60 秒发送新的兜底消息。
5. Codex 结束后解析最终 JSON。
6. 写入本地结果文件。
7. 如果显式开启回调开关，由 Bridge Python 进程 POST Java callback。

Bridge 不要求 Codex 自己访问 HTTP。Codex 只需要输出最终 JSON，Bridge 父进程负责回调、落盘与飞书兜底消息。

### 3. Codex/Agent

Codex 只负责基于提示词和 skill 规范产出结构化 JSON。

不推荐让 Codex 直接回调 Java，因为它运行在 `--sandbox read-only` 下，本机 HTTP 或网络请求容易被沙箱拦截。刚才验证中，Codex 能产出 JSON，但直接 POST `127.0.0.1:{callback-port}` 被沙箱拦截，因此回调必须由 Bridge Python 父进程完成。

### 4. Java callback

回调接口：

```text
POST http://127.0.0.1:{callback-port}/api/tg/raw/governance/dify-audit/callback
```

请求体：

```json
{
  "auditLogId": 6,
  "replaceAssetId": 512,
  "docId": 751986,
  "rawTableName": "hdp_xxx.raw_table",
  "sceneType": 1,
  "replacedSql": "select ...",
  "analysis": "替换说明",
  "source": "FEISHU_LARK_CLI",
  "bridgeRequestId": "req_feishu_xxx",
  "tableOwnerName": "owner",
  "errorMsg": ""
}
```

Java 侧收到后继续走原业务逻辑：

- `replacedSql` 非空：执行 EXPLAIN 校验，再写业务日志。
- `replacedSql` 为空：记录失败原因，不更新为成功状态。

## 当前实现变更

### 1. Bridge 父进程回调

Bridge 新增 Python 父进程回调能力：

- 从 Codex 最终 stdout 解析 `replacedSql` 和 `analysis`。
- 优先从 Codex 输出 JSON 的 `callback` 对象解析 `auditLogId`、`replaceAssetId`、`docId`、`rawTableName`、`sceneType`、`callbackUrl`、`tableOwnerName`。
- 当 `callback` 缺失时，才从原始提示词解析 `auditLogId`、`replaceAssetId`、`docId`、`rawTableName`、`sceneType`、`callbackUrl`。
- 回调结果写入本地结果文件的 `callback` 字段。

### 2. 回调开关

默认不回调，防止测试或半成品 SQL 误写业务系统。

启动时显式开启：

```bash
python3 bin/feishu_codex_bridge.py start --enable-raw-dify-audit-callback
```

或使用环境变量：

```bash
ENABLE_RAW_DIFY_AUDIT_CALLBACK=true python3 bin/feishu_codex_bridge.py start
```

不开启时，Bridge 仍然会落本地结果文件：

```text
<bridge-root>/inbox/raw-dify-audit-results.ndjson
```

其中 `callback` 会记录为：

```json
{
  "status": "disabled",
  "reason": "raw dify audit callback switch is off"
}
```

### 3. 非阻塞读取 Codex 输出

旧版本使用阻塞式 `readline()`，当 Codex 长时间无输出时，Bridge 不能按分钟发送兜底消息。

新版使用 `select.select(..., 0.2)` 非阻塞轮询：

- Codex 无输出时也能继续触发进度消息。
- 长任务不会看起来“卡死”。
- Feishu 占位消息不可编辑时，会发新消息兜底。

### 4. 飞书兜底消息

当 Feishu 返回：

```text
230075 The message has exceeded the time that can be edited
```

Bridge 不再只失败，而是改为向同一 chat 发送新消息。

默认间隔：

```text
fallback_message_interval = 60s
```

## 本地审计文件

### 1. Bridge 日志

```text
<bridge-root>/var/bridge.log
```

### 2. 执行耗时

```text
<bridge-root>/logs/timings.ndjson
```

### 3. raw Dify 结果

```text
<bridge-root>/inbox/raw-dify-audit-results.ndjson
```

每行一个 JSON，核心字段：

```json
{
  "schema": "feishu-codex.raw-dify-audit-result.v1",
  "eventId": "xxx",
  "requestId": "req_feishu_xxx",
  "status": "done",
  "auditLogId": 6,
  "docId": 751986,
  "rawTableName": "hdp_xxx.raw_table",
  "modelTableName": "hdp_xxx.model_table",
  "replacedSql": "",
  "parsedJson": {
    "replacedSql": "",
    "analysis": "无法安全替换原因",
    "callback": {
      "auditLogId": 6,
      "replaceAssetId": 512,
      "docId": 751986,
      "rawTableName": "hdp_xxx.raw_table",
      "sceneType": 1,
      "callbackUrl": "http://127.0.0.1:{callback-port}/api/tg/raw/governance/dify-audit/callback",
      "source": "FEISHU_LARK_CLI",
      "tableOwnerName": "owner"
    }
  },
  "callback": {
    "status": "disabled"
  }
}
```

## 运行命令

### 默认启动，安全模式，不回调

```bash
cd <bridge-root>
python3 bin/feishu_codex_bridge.py start
```

### 启动并开启回调

```bash
cd <bridge-root>
python3 bin/feishu_codex_bridge.py stop
python3 bin/feishu_codex_bridge.py start --enable-raw-dify-audit-callback
```

### 健康检查

```bash
python3 bin/feishu_codex_bridge.py healthcheck --format json
```

健康状态应满足：

- `bridge.running = true`
- `event_bus.bus_running = true`
- `event_bus.active_consumers = 1`
- `event_bus.dropped = 0`

## 提示词优化建议

为了提高 SQL 替换准确率，Java 发送给飞书的 prompt 应明确以下要求：

1. 只返回 JSON，不返回 Markdown。
2. JSON 字段必须包含 `replacedSql`、`analysis`、`callback`。
3. `callback` 必须原样使用提示词中的 Bridge 回调参数 JSON，不要自行改写。
4. `replacedSql` 必须是完整 SQL，不能是片段、diff 或说明。
5. raw 表名必须与元数据源表一致，否则返回空 `replacedSql`。
6. 模型表、字段映射、分区字段不确定时，返回空 `replacedSql`。
7. 禁止为了产出结果而猜测字段。
8. SQL 结构不允许随意重写，只能根据映射做替换。
9. 如果产出 `replacedSql`，Java callback 会继续 EXPLAIN，Agent 不需要自己访问 Java。
10. Dify `prompt_text`、`request_json`、`response_json` 不做业务截断，Bridge/Codex 应按完整上下文处理。

推荐追加约束：

```text
你只负责产出候选 replacedSql，不要调用回调接口。
如果无法安全产出完整 SQL，请返回：
{"replacedSql":"","analysis":"具体原因","callback":{...原样回填Bridge回调参数JSON...}}
```

## 已验证结论

### 1. 本地 Java callback 路由可达

`GET /api/tg/raw/governance/dify-audit/callback` 返回 `405 Allow: POST`，说明接口存在，POST 才是正确方法。

### 2. 旧逻辑失败原因

旧逻辑让 Codex 自己回调，Codex 在 `--sandbox read-only` 下访问 `127.0.0.1:{callback-port}` 被拦截，因此没有成功回调。

### 3. 新逻辑可靠边界

新版让 Bridge Python 父进程回调，它不在 Codex 沙箱里，可以访问本机 Java 服务。Codex 即使无法联网，也不会影响 Bridge 的回调能力。

### 4. 当前样例返回空 SQL 是合理结果

测试样例里存在数据不一致：

- 告警记录中的 `rawTableName` 与 prompt 元数据中的源表不一致。
- 飞书消息里的 `raw_sql` 存在截断风险。
- 可见 SQL 字段与字段映射不完全匹配。

因此 Agent 返回空 `replacedSql` 是安全行为，避免误替换。

## 后续建议

1. Java 侧 prompt 必须保证 SQL 和元数据完整，不要发送被飞书截断后的内容。
2. 对超大 prompt，建议 Java 写入可访问的内部接口或对象存储，再在飞书消息中只发任务 ID 和拉取地址。
3. Bridge 可继续扩展为 HTTP server 模式，供测试环境直接调用。
4. 生产环境可优先使用 Feishu 群聊触发模式，因为线上无法访问本机 `127.0.0.1`。
5. 若要开启自动回调，先用一条真实失败数据在本地确认 `raw-dify-audit-results.ndjson.callback.status=success`。

## 当前默认策略

当前 bridge 已使用新版脚本启动，默认安全策略是：

- 接收飞书消息。
- 调用 Codex。
- 每分钟兜底发送新消息。
- 解析结果并落本地 NDJSON。
- 不自动回调 Java。

需要自动回调时，重启时加：

```bash
--enable-raw-dify-audit-callback
```

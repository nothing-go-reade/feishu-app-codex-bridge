# Product Positioning

## Short Description

`feishu-app-codex-bridge` is a local-first, file-backed Feishu/Lark to Codex bridge for auditable enterprise agent workflows.

It lets teams control local Codex from an online Feishu/Lark app while keeping memory, logs, timing, and optional business callbacks under local control.

## What Makes It Different

The project is not positioned as "the first Feishu bot for Codex." Similar chat-to-agent bridges already exist.

This project focuses on:

- local-private enterprise operation
- file-backed memory
- append-only audit logs
- SQLite dedupe and response state
- long-task progress UX
- Feishu/Lark edit-window fallback
- explicit callback safety
- business workflow examples

## Target Users

- Internal platform teams.
- Data governance teams.
- AI infrastructure teams.
- Developer productivity teams.
- Teams that need chat-triggered local agents with audit trails.

## Example Use Cases

- A Feishu/Lark bot dispatches a long SQL analysis task to local Codex.
- A business system sends a failed Dify task to a chat group for local fallback.
- An engineer asks Codex to inspect a local repository from Feishu/Lark.
- A local agent produces structured JSON and the bridge records it before any callback.

## Non-Goals

- Replacing Feishu/Lark app security.
- Replacing business-side validation.
- Running arbitrary production mutations from model output.
- Hosting a cloud SaaS.
- Storing secrets in prompts or repository files.


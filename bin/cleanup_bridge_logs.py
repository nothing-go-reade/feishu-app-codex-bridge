#!/usr/bin/env python3
"""
Safely compact local Feishu bridge log files by keeping recent records only.

Default mode is dry-run. Use --execute to rewrite files.
Only known bridge log/result files under this project are touched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DAYS = 7

NDJSON_FILES = (
    "logs/events.ndjson",
    "logs/timings.ndjson",
    "logs/watch.ndjson",
    "logs/ai-semantic-http.ndjson",
    "logs/raw-dify-audit-http.ndjson",
    "inbox/feishu-to-codex.ndjson",
    "inbox/codex-to-feishu.ndjson",
    "inbox/ai-semantic-results.ndjson",
    "inbox/ai-semantic-requests.ndjson",
    "inbox/ai-semantic-callback-compensation.ndjson",
    "inbox/raw-dify-audit-results.ndjson",
    "inbox/raw-dify-audit-requests.ndjson",
)

TEXT_LOG_FILES = (
    "var/bridge.log",
    "var/heartbeat.log",
)


@dataclass
class CleanupResult:
    relative_path: str
    exists: bool
    skipped: bool
    reason: str
    original_lines: int = 0
    kept_lines: int = 0
    removed_lines: int = 0
    original_bytes: int = 0
    kept_bytes: int = 0
    backup_path: str = ""


def parse_iso_datetime(value: object) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def parse_text_log_datetime(line: str) -> Optional[dt.datetime]:
    if len(line) < 19:
        return None
    raw = line[:19]
    try:
        parsed = dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    local_tz = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    return parsed.replace(tzinfo=local_tz).astimezone(dt.timezone.utc)


def keep_ndjson_line(line: str, cutoff: dt.datetime) -> bool:
    text = line.strip()
    if not text:
        return True
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return True
    if not isinstance(payload, dict):
        return True
    for key in ("ts", "started_at", "updated_at", "created_at"):
        parsed = parse_iso_datetime(payload.get(key))
        if parsed is not None:
            return parsed >= cutoff
    return True


def keep_text_log_line(line: str, cutoff: dt.datetime) -> bool:
    parsed = parse_text_log_datetime(line)
    if parsed is None:
        return True
    return parsed >= cutoff


def acquire_exclusive(handle: object) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def release_lock(handle: object) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def backup_file(root: Path, path: Path, backup_root: Path) -> Path:
    relative = path.relative_to(root)
    target = backup_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target


def compact_file(
    root: Path,
    relative_path: str,
    cutoff: dt.datetime,
    *,
    backup_root: Path,
    execute: bool,
    parser: str,
    skip_reason: str = "",
) -> CleanupResult:
    path = root / relative_path
    if not path.exists():
        return CleanupResult(relative_path, exists=False, skipped=True, reason="missing")
    if skip_reason:
        size = path.stat().st_size
        return CleanupResult(relative_path, exists=True, skipped=True, reason=skip_reason, original_bytes=size)
    if not path.is_file():
        return CleanupResult(relative_path, exists=True, skipped=True, reason="not a regular file")

    original_bytes = path.stat().st_size
    keep_func = keep_ndjson_line if parser == "ndjson" else keep_text_log_line
    backup_path = ""

    with path.open("r+", encoding="utf-8", errors="replace") as handle:
        acquire_exclusive(handle)
        try:
            lines = handle.readlines()
            kept = [line for line in lines if keep_func(line, cutoff)]
            kept_text = "".join(kept)
            if execute and len(kept) != len(lines):
                backup_path = str(backup_file(root, path, backup_root))
                handle.seek(0)
                handle.truncate(0)
                handle.write(kept_text)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            release_lock(handle)

    return CleanupResult(
        relative_path=relative_path,
        exists=True,
        skipped=False,
        reason="",
        original_lines=len(lines),
        kept_lines=len(kept),
        removed_lines=len(lines) - len(kept),
        original_bytes=original_bytes,
        kept_bytes=len(kept_text.encode("utf-8")),
        backup_path=backup_path,
    )


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{value}B"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clean Feishu bridge logs older than N days")
    parser.add_argument("--root", default=str(ROOT), help="bridge root directory")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="days to keep, default 7")
    parser.add_argument("--execute", action="store_true", help="rewrite files; default is dry-run")
    parser.add_argument(
        "--include-active-text-logs",
        action="store_true",
        help="also compact var/bridge.log and var/heartbeat.log; safer when bridge is stopped",
    )
    parser.add_argument(
        "--truncate-active-text-logs",
        action="store_true",
        help="backup and truncate var/bridge.log and var/heartbeat.log completely",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = Path(args.root).expanduser().resolve()
    expected_root = ROOT.resolve()
    if root != expected_root:
        print(f"Refuse to clean unexpected root: {root}")
        print(f"Expected bridge root: {expected_root}")
        return 2
    if args.days <= 0:
        print("--days must be positive")
        return 2

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    backup_root = root / "var" / "log-cleanup-backups" / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"mode={'EXECUTE' if args.execute else 'DRY-RUN'} root={root} keep_days={args.days} cutoff_utc={cutoff.isoformat()}")
    if args.execute:
        print(f"backup_root={backup_root}")

    results: list[CleanupResult] = []
    for relative in NDJSON_FILES:
        results.append(
            compact_file(root, relative, cutoff, backup_root=backup_root, execute=args.execute, parser="ndjson")
        )
    for relative in TEXT_LOG_FILES:
        if args.truncate_active_text_logs:
            path = root / relative
            if not path.exists():
                results.append(CleanupResult(relative, exists=False, skipped=True, reason="missing"))
                continue
            original_bytes = path.stat().st_size
            backup_path = ""
            if args.execute:
                backup_path = str(backup_file(root, path, backup_root))
                with path.open("r+", encoding="utf-8", errors="replace") as handle:
                    acquire_exclusive(handle)
                    try:
                        handle.truncate(0)
                        handle.flush()
                        os.fsync(handle.fileno())
                    finally:
                        release_lock(handle)
            results.append(
                CleanupResult(
                    relative,
                    exists=True,
                    skipped=False,
                    reason="",
                    original_lines=0,
                    kept_lines=0,
                    removed_lines=0,
                    original_bytes=original_bytes,
                    kept_bytes=0,
                    backup_path=backup_path,
                )
            )
        else:
            skip_reason = "" if args.include_active_text_logs else "active text log skipped by default"
            results.append(
                compact_file(
                    root,
                    relative,
                    cutoff,
                    backup_root=backup_root,
                    execute=args.execute,
                    parser="text",
                    skip_reason=skip_reason,
                )
            )

    total_removed_lines = 0
    total_saved_bytes = 0
    for result in results:
        if result.skipped:
            print(f"SKIP {result.relative_path}: {result.reason} ({human_bytes(result.original_bytes)})")
            continue
        saved = max(0, result.original_bytes - result.kept_bytes)
        total_removed_lines += result.removed_lines
        total_saved_bytes += saved
        action = "CLEAN" if args.execute else "WOULD"
        print(
            f"{action} {result.relative_path}: "
            f"lines {result.original_lines}->{result.kept_lines} "
            f"removed={result.removed_lines} "
            f"size {human_bytes(result.original_bytes)}->{human_bytes(result.kept_bytes)} "
            f"saved={human_bytes(saved)}"
        )
        if result.backup_path:
            print(f"  backup: {result.backup_path}")

    print(f"summary removed_lines={total_removed_lines} estimated_saved={human_bytes(total_saved_bytes)}")
    if not args.execute:
        print("dry-run only; add --execute to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

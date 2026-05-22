#!/usr/bin/env python3
"""Basic repository hygiene checks for public release."""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def iter_text_files() -> list[Path]:
    ignored_dirs = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv", "venv"}
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in ignored_dirs for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in {".py", ".md", ".toml", ".json", ".example", ".gitignore"}:
            files.append(path)
    return files


def main() -> int:
    banned = [
        token.strip()
        for token in os.environ.get("PRIVATE_TOKEN_PATTERNS", "").split(",")
        if token.strip()
    ]
    failed: list[str] = []
    for path in iter_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in banned:
            if token in text:
                failed.append(f"{path.relative_to(ROOT)} contains private token pattern: {token}")
    if failed:
        print("\n".join(failed))
        return 1
    print("sanitization ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

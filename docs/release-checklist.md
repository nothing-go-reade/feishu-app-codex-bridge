# Open Source Release Checklist

Use this checklist before publishing the repository.

## Repository Hygiene

- [ ] `python3 -m py_compile bin/feishu_app_codex_bridge.py tests/test_sanitization.py`
- [ ] `python3 tests/test_sanitization.py`
- [ ] Optional local private-token scan:
  `PRIVATE_TOKEN_PATTERNS='token1,token2,internal-prefix' python3 tests/test_sanitization.py`
- [ ] `python3 bin/feishu_app_codex_bridge.py --help`
- [ ] No runtime NDJSON files are committed.
- [ ] No SQLite database files are committed.
- [ ] No pid or log files are committed.
- [ ] No local machine paths are committed.
- [ ] No Feishu/Lark open IDs, chat IDs, message IDs, app IDs, or user names are committed.
- [ ] No business table names, SQL, callback URLs, or internal hostnames are committed.

## Documentation

- [ ] README explains the positioning clearly.
- [ ] Architecture diagram renders.
- [ ] File protocol is documented.
- [ ] Callback default is documented as disabled.
- [ ] Security policy exists.
- [ ] Contribution policy exists.

## Runtime Defaults

- [ ] Callback is disabled by default.
- [ ] Runtime directories are ignored by Git.
- [ ] Healthcheck and watch commands are documented.
- [ ] Example data is synthetic and safe.

## GitHub Metadata

- [ ] Replace placeholder URLs in `pyproject.toml`.
- [ ] Add repository description:
  `Local-first, file-backed Feishu/Lark to Codex bridge for auditable enterprise agent workflows.`
- [ ] Add topics:
  `feishu`, `lark`, `codex`, `agent`, `bridge`, `chatops`, `local-first`, `audit`.
- [ ] Add a short demo GIF or screenshots only with sanitized data.

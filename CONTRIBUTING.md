# Contributing

Thank you for considering a contribution.

This project is designed for local-private enterprise agent workflows. Please keep that boundary in mind when contributing.

## Principles

- Do not commit runtime data.
- Do not commit app secrets, tokens, chat IDs, message IDs, user IDs, or internal URLs.
- Keep callbacks disabled by default.
- Keep model output as candidate output; business systems should validate before mutation.
- Prefer small, auditable changes.
- Prefer standard library Python unless a dependency has a strong reason to exist.

## Local Checks

```bash
python3 -m py_compile bin/feishu_app_codex_bridge.py
python3 tests/test_sanitization.py
python3 bin/feishu_app_codex_bridge.py init
```

## Pull Request Checklist

- [ ] No runtime files from `inbox/`, `logs/`, `state/`, or `var/` are included.
- [ ] No secrets or private identifiers are included.
- [ ] Documentation is updated.
- [ ] Callback behavior remains disabled by default.
- [ ] New file formats are documented in `docs/file-protocol.md`.


# Security Policy

## Supported Versions

This project is currently pre-1.0. Security fixes target the latest `main` branch.

## Reporting a Vulnerability

Please report security issues privately to the repository maintainers. Do not open a public issue for secrets exposure, callback bypasses, prompt injection risks, or business-side mutation risks.

## Threat Model

Main risks:

- Prompt injection causing unsafe callback payloads.
- Accidental commit of runtime logs or private identifiers.
- Callback endpoint misuse.
- Treating model output as trusted business truth.
- Feishu/Lark bot permissions broader than necessary.

## Recommended Defaults

- Keep callbacks disabled unless explicitly needed.
- Validate callback payloads server-side.
- Use idempotency keys in business systems.
- Store runtime data locally and exclude it from Git.
- Run the bridge with the least privileges needed.
- Review logs before sharing bug reports.


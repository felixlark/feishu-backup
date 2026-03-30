# Contributing

Thanks for contributing to `feishu_backup`.

## Development setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -e .
```

## Test

```bash
./.venv/bin/python -m unittest tests.test_feishu_backup
```

## Scope

This project is intentionally narrow:

- stable Feishu wiki backup
- repeatable local recovery workflow
- low-friction installation and operation

Please avoid broadening it into a generic document platform or a replacement for `feishu-docx`.

## Contribution principles

- Keep user-facing commands stable
- Prefer backup reliability over clever routing
- Document new config and log outputs in `README.md`
- Add or update tests for behavior changes


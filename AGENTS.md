# Feishu-Backup Runbook

## Repo Scope

- Owner/escalation: Longbiao for backup workflow, local scheduling, and release docs.
- This repo owns the standalone `feishu-backup` CLI for Feishu knowledge-base backup on top of `feishu-docx`.
- It is a backup workflow layer, not a replacement export engine.

## Canonical Commands

- Create config template: `feishu-backup write-env-template`
- Browser OAuth: `feishu-backup authorize`
- Incremental sync: `feishu-backup sync`
- Full backup: `feishu-backup full`
- Install launchd job: `feishu-backup install-launchd`
- Local editable install: `./.venv/bin/pip install -e .`
- Tests: `python3 -m pytest tests`

## Routine Operations

| Trigger | Command | Expected Result | Failure Recovery |
| --- | --- | --- | --- |
| First setup | `feishu-backup write-env-template` then fill env and run `feishu-backup authorize` | Env file exists and OAuth succeeds | Recheck `APP_ID`, `APP_SECRET`, and redirect port before retrying |
| Daily/manual backup | `feishu-backup sync` | New/changed docs are archived with progress, summary, and report logs | Inspect `logs/*.progress.log` and `resume-state.json`, then rerun sync |
| Rebuild complete archive | `feishu-backup full` | Full traversal completes with failed count tracked | Do not delete existing archive until the full run produces a clean report |

## Troubleshooting

| Trigger | Command | Expected Result | Failure Recovery |
| --- | --- | --- | --- |
| OAuth/browser issue | `feishu-backup authorize` | Browser approval completes and token state is written | Check redirect port `9527` and Feishu app credentials |
| Stale partial run | Inspect `~/Library/Application Support/feishu-backup/resume-state.json` | Failed or pending doc is identifiable | Resume with `sync`; use `reset-resume` only after reading the current state |

## Verification

- Run `python3 -m pytest tests` for code changes.
- For backup behavior changes, run a small `feishu-backup sync` and verify progress log, summary, report, manifest, and archive path.

## Release/Deploy

- Keep README, changelog, demo assets, and release notes aligned with the same public command set.
- `feishu-backup install-launchd` is the scheduled-run entrypoint; verify generated LaunchAgent before calling scheduling done.

## Guardrails

- Do not expose Feishu credentials or local backup contents in docs.
- Keep local archive routing simple and deterministic; do not add AI classification to the core backup path.

## Known State

- Default state directory: `~/Library/Application Support/feishu-backup/`.
- User-facing stable commands are `authorize`, `sync`, `full`, and `install-launchd`.

## Browser Automation Constraint
- Follow the global `~/.codex/AGENTS.md` browser/GUI routing. Keep only this project's URL, account, extension, fixture, viewport, and acceptance details here; do not promote Chrome or `chrome-use` to a project-wide default.
- Keep only repo-specific verification surfaces here; do not copy the full global policy block into this runbook.

## Development Workflow

- Follow the global `~/.codex/AGENTS.md` and workspace `/Users/longbiao/Documents/AGENTS.md` development workflow: work in the canonical checkout with one writer and do not create new worktrees.
- Use a task branch in the canonical checkout only when the task needs one; do not create manual worktree directories.
- Put reusable build caches and DerivedData in `~/Library/Caches/<project-id>/`, runtime state, data, tool repositories and active releases in `~/Library/Application Support/<project-id>/`, logs in `~/Library/Logs/<project-id>/`, and human-facing documents in `~/Documents/<mainline>/<project>/` (do not write anything new to `~/Local`; retain transition symlinks only and delete it once empty), and one-run scratch in a directory created by `mktemp -d` under `$TMPDIR`, removed after use; pass output paths explicitly.
- Preserve existing dirty checkouts. Inspect `git status --short` before editing, and do not stash, commit, remove, migrate or clean user changes and pre-existing residue unless explicitly asked with an exact scope.
- Existing worktrees and other pre-existing noncompliant residue stay read-only unless the user explicitly requests an exact cleanup or migration scope.

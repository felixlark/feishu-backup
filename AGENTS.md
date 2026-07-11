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
- Follow the global `~/.codex/AGENTS.md` official browser/GUI automation policy: Chrome plugin for signed-in browser state, Browser plugin for unauthenticated rendering, and Computer Use for native desktop boundaries. Do not bypass it with AppleScript or `osascript` unless the global exception rules are met.
- Keep only repo-specific verification surfaces here; do not copy the full global policy block into this runbook.

## Worktree Policy

- Follow the global `~/.codex/AGENTS.md` main-first development rule: work in the current Local checkout by default and use a worktree only when the global exception list applies.
- Branch names should use `codex/<repo>-<short-task>`; manual long-lived worktree directories should use `~/Projects/<repo>-<short-task>`.
- Initialize dependencies inside each worktree and keep ports, databases, device/simulator state, build outputs, and ignored local config isolated per checkout.
- Preserve existing dirty checkouts. Inspect `git status --short` before editing, and do not stash, commit, remove, or migrate user changes unless explicitly asked.
- After merge or abandonment, clean up with `git worktree remove <path>` and use `git worktree prune` only for stale metadata.

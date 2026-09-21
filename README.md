# Feishu Backup

`feishu_backup` is a practical backup workflow built on top of [`feishu-docx`](https://github.com/wxy2ab/feishu-docx). It is not a replacement export engine. It turns single-document export into a repeatable backup pipeline for real Feishu knowledge bases.

In one sentence: back up Feishu knowledge bases to your local disk with incremental sync, document-level resume, local folder routing, and scheduled runs.

![Feishu Backup demo](./assets/demo/feishu-backup-demo.gif)

## Why this instead of manual export?

- Manual export is one-by-one and easy to miss.
- `feishu-docx` is great at exporting documents, but it is not a full backup workflow by itself.
- `feishu_backup` adds space discovery, incremental sync, document-level resume, local archiving, logs, and scheduling on top of `feishu-docx`.

## 10-minute quick start

1. Install:

```bash
pipx install git+https://github.com/felixlark/feishu-backup.git
```

2. Generate the local config:

```bash
feishu-backup write-env-template
```

3. Fill `APP_ID` and `APP_SECRET` in:

```text
~/Library/Application Support/feishu-backup/env
```

4. Authorize once in the browser:

```bash
feishu-backup authorize
```

5. Run the first sync:

```bash
feishu-backup sync
```

## What you get

- Full and incremental export of accessible Feishu wiki spaces
- Document-level resume with `resume-state.json`
- Real-time progress in terminal and `logs/*.progress.log`
- Local archive routed into `~/Documents`
- Strict top-level folder naming in two-word kebab-case
- Daily scheduling through `launchd`
- Shareable summary and Markdown report after each run

## Example output

```text
[backup] progress log: ~/Library/Application Support/feishu-backup/logs/20260331T010203Z.progress.log
[backup] start mode=sync
[backup] discovered spaces=4
[space 1/4] start 丽娟的知识库 (7428864556937248770)
[doc 1/128] start 首页 (7428864556937248770:KXSCwVjGRi3UcfkghXmcqxA6nff)
[doc] export 首页 -> https://xmu-mars.feishu.cn/wiki/KXSCwVjGRi3UcfkghXmcqxA6nff
[doc] archive 首页 -> liujuan-knowledge
[new 1/128] 首页 -> liujuan-knowledge
[backup] done spaces=4 docs=128 new=24 updated=7 skipped=95 deleted=2 failed=0
[backup] summary ~/Library/Application Support/feishu-backup/logs/20260331T010203Z.summary.txt
[backup] report ~/Library/Application Support/feishu-backup/logs/20260331T010203Z.report.md
```

See [examples/first-run.progress.log](./examples/first-run.progress.log), [examples/first-run.report.md](./examples/first-run.report.md), and [examples/output-tree.txt](./examples/output-tree.txt).

## Demo assets

- Short demo GIF: [assets/demo/feishu-backup-demo.gif](./assets/demo/feishu-backup-demo.gif)
- Short demo video: [assets/demo/feishu-backup-demo-short.mp4](./assets/demo/feishu-backup-demo-short.mp4)
- OAuth approval screenshot: [assets/demo/feishu-backup-auth.jpg](./assets/demo/feishu-backup-auth.jpg)
- Release notes draft: [assets/demo/release-notes-v0.1.0.md](./assets/demo/release-notes-v0.1.0.md)

## Installation

### Option 1: `pipx`

```bash
pipx install git+https://github.com/felixlark/feishu-backup.git
```

### Option 2: local checkout

```bash
python3 -m venv .venv
./.venv/bin/pip install -e .
```

## Commands

Main commands:

- `feishu-backup authorize`
- `feishu-backup sync`
- `feishu-backup full`
- `feishu-backup install-launchd`

Advanced commands:

- `feishu-backup normalize-folders`
- `feishu-backup reset-resume`
- `feishu-backup write-env-template`

## Configuration

The default state directory is:

```text
~/Library/Application Support/feishu-backup/
```

Minimal config:

```text
APP_ID=...
APP_SECRET=...
FEISHU_AUTH_MODE=oauth
```

Useful optional settings:

```text
DOCUMENTS_ROOT=~/Documents
SPACE_IDS=
SPACE_NAME_ALLOWLIST=
FEISHU_OAUTH_REDIRECT_PORT=9527
LAUNCHD_HOUR=2
LAUNCHD_MINUTE=15
```

## Public interface

The tool is published as a command-line workflow. The stable user-facing entrypoints are:

- `feishu-backup authorize`
- `feishu-backup sync`
- `feishu-backup full`
- `feishu-backup install-launchd`

Each run writes:

- `manifest.json`
- `resume-state.json`
- `logs/*.json`
- `logs/*.progress.log`
- `logs/*.summary.txt`
- `logs/*.report.md`
- `trash/...`

## Why not just use `feishu-docx`?

Because the problems are different:

- `feishu-docx` is the export engine
- `feishu_backup` is the backup workflow

`feishu_backup` depends on `feishu-docx` and should be understood as a workflow layer on top of it. It handles repeated syncs, resume, local routing, scheduling, and backup state.

## Risk boundary

- Requires Feishu app credentials and a one-time OAuth authorization
- Best suited for personal or team knowledge base backups
- Local routing is intentionally simple: filename and source path first, not semantic AI classification
- This project targets stable backup and recovery, not pixel-perfect publishing export

## Demo scenario

The core demo for launch content should stay consistent:

> Back up a team Feishu knowledge base into local `~/Documents`, resume from the last document after interruption, and run automatically every night.

## Publishing checklist

- Public GitHub repo with screenshots, examples, and FAQ
- GitHub Release with install command
- One terminal GIF or screen recording
- One Chinese tutorial article
- One comparison post: manual export vs `feishu-docx` vs `feishu_backup`
- One build story post explaining the workflow layer

## Chinese launch drafts

- Tutorial: [docs/launch/tutorial-zh.md](./docs/launch/tutorial-zh.md)
- Comparison: [docs/launch/comparison-zh.md](./docs/launch/comparison-zh.md)
- Build story: [docs/launch/build-story-zh.md](./docs/launch/build-story-zh.md)

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md).

## Changelog

See [CHANGELOG.md](./CHANGELOG.md).

## License

MIT. See [LICENSE](./LICENSE).

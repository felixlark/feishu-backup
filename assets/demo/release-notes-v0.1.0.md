# Feishu Backup v0.1.0

Feishu Backup is a practical Feishu knowledge base backup workflow built on top of `feishu-docx`.

## Highlights

- Export Feishu knowledge bases to local disk
- Incremental sync
- Resume from interruption at the document level
- Route documents into local folders under `~/Documents`
- Real-time progress, summary, and Markdown report
- Daily scheduling through `launchd`

## Install

```bash
pipx install git+https://github.com/longbiaochen/feishu-backup.git
```

## Commands

- `feishu-backup authorize`
- `feishu-backup sync`
- `feishu-backup full`
- `feishu-backup install-launchd`

## Positioning

`feishu-docx` is the export engine. `feishu-backup` is the backup workflow layer.

# 如何把飞书知识库稳定备份到本地

## 标题

如何把飞书知识库稳定备份到本地硬盘，并且支持断点续传

## 导语

如果你在飞书知识库里积累了越来越多团队文档，最麻烦的不是“能不能导出一篇”，而是“能不能长期、稳定、批量地备份下来”。`feishu-backup` 解决的是这个问题。

## 核心卖点

- 基于 `feishu-docx`，但做的是完整备份工作流
- 支持全量和增量同步
- 中断后从上次文档继续，不重新从头跑
- 自动把文档落到本地 `~/Documents`
- 可以接 `launchd` 每天定时跑

## 演示场景

把团队飞书知识库每天备份到本地 `Documents`，失败后自动续传。

## 素材位

- 插入桌面全流程录屏
- 插入终端实时进度截图
- 插入 `~/Documents` 落盘结果截图

## 结尾

项目地址：`https://github.com/longbiaochen/feishu-backup`

安装：

```bash
pipx install git+https://github.com/longbiaochen/feishu-backup.git
```


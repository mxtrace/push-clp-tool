---
name: push-clp-tool
description: "LCL Push CLP 自动化工具—在 CLP Cutoff 临近时筛选已 all-ready 但未完成 CLP 的 MSPP+SMP 订单，弹出 Outlook 草稿供 POC 审核后手动发送给 LSP。每工作日 09:30/14:00 运行。"
version: "1.0.0"
tags: [lcl, clp, push, email, automation, scheduled]
---

# Push CLP Tool — Aki Skill

## 工具说明

定时自动化 LCL 货件 CLP 跟进流程：

1. 从 OC PLOT 拉取本周+下周船期，获取 CLP Cutoff
2. 比对 Loading 表，识别 MSPP + SMP 订单
3. 筛选 CLP Cutoff 为今天/明天、所有 Task 已关闭、尚未入箱的订单
4. 按 POL 分组弹出 Outlook 新建邮件草稿（TO = LSP 邮笱），POC 审核后手动发送

---

## 首次安装（同事操作）

### 前置条件

| 项目 | 要求 |
|------|------|
| 浏览器 | Firefox，已登录 OC（trans-logistics-cn.amazon.com）|
| Outlook | 已登录并运行 |
| Loading 表 | 放在 `Desktop\Loading_V2.2.xlsm` |
| 工具文件夹 | 解压后放在 `Desktop\PushCLP\`（含 PushCLP.exe + config.local.yaml + data/ 文件夹）|

### data/ 目录内容（必须存在）

| 文件 | 来源 |
|------|------|
| `MSPP List Review 2026May.xlsx` | SharePoint 下载（每月更新）|
| `LSP邮箱.xlsx` | 工具自带 |
| `Blurb_CLP.txt` | 工具自带 |

### 初始化步骤（对 Aki 说以下内容）

> 帮我初始化 Push CLP 定时任务

---

## 定时任务执行指令

> 以下为 Aki Scheduled Task 的标准 instruction。2 个时间点使用同一内容。
> Workspace 设置为：`%USERPROFILE%\Desktop\PushCLP`

```
你是 Push CLP 自动跟进助手，请按以下步骤执行：

步骤1 — 运行 PushCLP.exe
  用 Bash 执行：
    powershell.exe -Command "echo '' | & \"$env:USERPROFILE\Desktop\PushCLP\PushCLP.exe\""
  等待进程完成（最多 300 秒）。若报错输出完整错误信息并继续步骤2。

步骤2 — 读取结果
  读取文件：./data/run_result.json
  解析字段：
    - clp_items：应推送的 AL0 列表（每条含 al0/pol/pod/etd/clp_cutoff）
    - popup_opened：Outlook 已弹出的草稿数
    - popup_anomalies：异常列表
  若文件不存在，跳至步骤4。

步骤3 — 展示本次摘要
  在本线程输出：
    [CLP 清单] N 条
    | AL0 | POL | POD | ETD | CLP Cutoff |
    （逐行列出 clp_items）

    [Outlook 草稿] M 封已弹出，请 POC 审核后手动发送

    [异常] K 条：逐条列出 POL + 原因

步骤4 — 异常处理（仅当步骤1/2 发生错误时）
  用 outlook draft 创建报错提醒草稿：
  - subject: [Push CLP ERROR] {today} — 程序异常
  - body: 错误信息
```

---

## 初始化指令（Aki 执行）

当用户说『帮我初始化 Push CLP 定时任务』时：

1. 检查文件存在性：
```bash
python3 -c "
import os
from pathlib import Path
home = Path(os.environ['USERPROFILE'])
root = home / 'Desktop' / 'PushCLP'
checks = {
  'exe': root / 'PushCLP.exe',
  'tbl': home / 'Desktop' / 'Loading_V2.2.xlsm',
  'mspp': root / 'data' / 'MSPP List Review 2026May.xlsx',
  'lsp': root / 'data' / 'LSP邮箱.xlsx',
  'cfg': root / 'config.local.yaml',
}
for k, p in checks.items(): print(k, p.exists(), p)
"
```

2. 若全部存在，创建 2 个 Scheduled Task：
```bash
scheduled create \\
  --name "Push CLP 09:30" \\
  --schedule "30 9 * * 1-5" \\
  --workspace "%USERPROFILE%\\Desktop\\PushCLP" \\
  --instructions "上方「定时任务执行指令」全文"

scheduled create \\
  --name "Push CLP 14:00" \\
  --schedule "0 14 * * 1-5" \\
  --workspace "%USERPROFILE%\\Desktop\\PushCLP" \\
  --instructions "上方「定时任务执行指令」全文"
```

---

## 手动触发（测试用）

对 Aki 说：『立即运行一次 Push CLP』

---

## 常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| CLP 清单 0 条 | 本次无满足条件的订单 | 正常 |
| HTTP 500 / 401 | OC 会话过期 | 重新登录 OC 后再运行 |
| Outlook 弹窗失败 | Outlook 未运行 | 先启动 Outlook 再运行 |
| exe 找不到 | 路径不对 | 确认 PushCLP/ 在 Desktop 根目录 |
| exe 被锁定 | 从其他电脑复制 | 右键 exe → 属性 → 勾选「解除锁定」|

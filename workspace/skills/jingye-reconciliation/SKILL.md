---
name: jingye-reconciliation
description: 敬业运输数据库对账流程。用户说“敬业对账”时，先询问采购对账起止日期；收到日期后直接读取平台同步维护的本地 SQLite 事实库，不启动 Appium、不重新爬取。销售固定取任务锚点日前9个自然日（不含锚点当天），同时复用数据库中的来源账号或选中车辆、PDF和对应运费确认，生成核验 Excel 并发送原有对账邮件。
---

# 敬业运输数据库对账

## 触发与交互

触发关键词：`敬业对账`。

首次收到关键词时不要运行命令。按上海自然日计算当天 `R`，先提醒销售范围和采购最晚截止日，再询问采购日期：

> 📋 本次销售对账固定为 `{R-9}..{R-1}`，即今天之前9个自然日、不含今天。采购建议并按当前对账口径截止至 `{R-1}`（昨天），不包含今天。请提供采购对账起止日期，例如：`2026-08-10 2026-08-12`

收到日期后重新取得执行时的上海自然日并冻结为 `{anchor_date}`。把用户输入规范为 `{purchase_start}`、`{purchase_end}` 两个 `YYYY-MM-DD` 日期。只提供一个日期时起止相同；开始日期不得晚于结束日期；采购结束日期不得晚于 `{anchor_date}-1`。日期不合法时不要启动，明确提示当前允许的最晚采购日期并重新询问。这里的用户日期只控制采购。

销售范围不向用户询问。销售固定为 `{anchor_date}-9` 至 `{anchor_date}-1`，共9个自然日且不含当天。例如 `{anchor_date}=2026-08-28` 时，销售范围为 `2026-08-19..2026-08-27`。把同一个 `{anchor_date}` 传给脚本；任务跨过午夜也不得改变本次范围。

## 唯一运行入口

收到合法采购日期后，后台启动：

```powershell
$py = 'C:\Users\YOUR_USER\OpenClawNative\python-venv\Scripts\python.exe'
$script = 'C:\Users\YOUR_USER\.openclaw-migration\workspace\skills\jingye-reconciliation\scripts\jingye_reconcile.py'
$baseDir = 'D:\openclaw_data\data\jingyeapp'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$stdout = Join-Path $baseDir "reconciliation_${stamp}.out.log"
$stderr = Join-Path $baseDir "reconciliation_${stamp}.err.log"

$process = Start-Process `
  -FilePath $py `
  -ArgumentList @(
    '-u', '-X', 'utf8', $script,
    '--base-dir', $baseDir,
    '--purchase-start', '{purchase_start}',
    '--purchase-end', '{purchase_end}',
    '--anchor-date', '{anchor_date}'
  ) `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr `
  -WindowStyle Hidden `
  -PassThru

Write-Output "RESULT:STARTED|PID:$($process.Id)|LOG:$stdout"
```

启动成功后只回复任务已启动、结果将通过邮件发送。不要等待报告完成或持续读取日志。若脚本报告已有任务运行，则不要重复启动。

直接调试或人工执行可使用：

```powershell
python skills/jingye-reconciliation/scripts/jingye_reconcile.py `
  --purchase-start 2026-08-10 --purchase-end 2026-08-12
```

仅验证数据库选择范围而不执行 PDF 核验和发邮件时，加 `--snapshot-only`。直接人工执行且不传 `--anchor-date` 时，脚本会在启动时冻结上海自然日；关键词交互必须传入本次确认的 `{anchor_date}`，确保提示范围与实际范围一致。

## 数据规则

- 唯一事实源是 `D:/openclaw_data/data/jingyeapp/platform_sync/jingye_facts.sqlite3`。对账不得调用 `yyb_search.py`、Appium、ADB、手机 App、OSS 或平台写接口。
- `build_reconciliation_snapshot.py` 以 SQLite 只读模式打开数据库，在 `D:/openclaw_data/data/jingyeapp/reconciliation_runs/<run_id>` 生成隔离的 Excel、来源 JSONL、运费 JSONL 和 PDF 硬链接；硬链接失败才复制 PDF。
- 采购按数据库中的装货日期选择 `{purchase_start}..{purchase_end}`。日期缺失的采购不猜测、不纳入，并在快照统计中计数。
- 销售按同一工作表、运单号、车牌号和运费目标的 `app_export_time` 选择 `R-9..R-1`。日期缺失的销售不猜测、不纳入，并在快照统计中计数。
- 来源优先使用原始 Excel 路径和行号对应的 `source_context_events`；只有旧数据缺失该证据时才回退 `waybill_sources`。河北按 `login_account`，上海按 `selected_car`，不得跨账号或跨车辆拼接。
- PDF 必须通过 `waybill_documents` 与运单关联，并同时匹配采购/销售方向和河北众沃/上海宾迪来源；没有精确位置时保持缺失，不得从其他公司目录猜测。
- 运费只为入选销售行导出，且必须匹配同一工作表、运单、车牌和来源范围；每条运费实例只能占用一条 PDF 明细。`result_check.py` 先按完全标准化规格分组，不论候选数量是否相等都优先使用“结算量与 PDF 理重”互相唯一且数值容差合格的关系消歧，并允许六小时内跨午夜；随后只允许互相唯一日期、唯一剩余一对，或结算量和单价完全等价的多条记录稳定配对。禁止按页面/PDF原始顺序硬配；重量重复、缺失、数值不等价或其他非唯一情况继续保持歧义异常，不做相似规格猜测。未能唯一匹配的每条运费事实仍必须作为独立异常明细行写入原始规格、结算量和单价，并注明“暂未匹配到PDF规格”；不得把该数值猜配到任一 PDF 明细行。即使销售 PDF 缺失，已经取得的运费事实也不得丢弃。
- 运费确认尚未生成时，结算量和单价保持空白，并注明“运费确认暂未生成”；这不是技术异常。页面错误、无效数值或规格歧义仍是异常。

## 报告与邮件

快照成功后由现有 `result_check.py` 完成 PDF 文本解析、必要的 VLM 兜底、Excel 核验和邮件发送。输出仍写入 `D:/openclaw_data/data/jingyeapp/excel_check`，识别缓存仍复用 `D:/openclaw_data/data/jingyeapp/result_check_cache`。

对账收件人按 `--email-to`、`JINGYE_RECONCILIATION_EMAIL_TO`、兼容项 `SMTP_RECEIVER` 的顺序选择，不得使用 `JINGYE_PLATFORM_EMAIL_TO`。两个敬业专用收件人统一维护在 `skills/jingye-platform-sync/.env`；多个收件人可用逗号、分号或空格分隔。单次命令的 `--email-to` 只覆盖本次对账，不修改持久配置。

邮件发送成功才算对账成功。不得在聊天、命令行或摘要中输出 SMTP 授权码、模型密钥或其他凭据。

## 运行期禁令

- Skill 运行时只能调用已经封装的脚本并传参，不得临时改源码、改常量或注入代码后重试。
- 不得为了补齐对账数据临时触发爬取；数据缺口应通过 `jingye-platform-sync` 的 `backfill` 入口补抓、入库、去重和上传，然后重新对账。
- 不得删除事实库、历史 PDF、识别缓存或已生成报告。

## 相关脚本

- `scripts/jingye_reconcile.py`：关键词和人工对账的唯一入口。
- `scripts/build_reconciliation_snapshot.py`：SQLite 只读选择与快照物化。
- `scripts/result_check.py`：现有核验、报告和邮件。
- `scripts/yyb_search.py`：仅由平台同步流水线用于抓取，本 Skill 不调用。

## 采购承运商与历史兼容

两个采购表在“货物名称”后增加“承运商”，后续列和结算公式同步后移，采购20列；两个销售表仍19列。旧19列采购表可读取，新快照缺失承运商时留空。数据库有采购有效行基线时，以该基线保留旧业务值，仅叠加同工作表来源的空承运商补值；新原始观察继续保留审计，但不能替换旧采购业务行。销售仍按最新观察读取。承运商为展示信息，不增加PDF核验条件，不改变运费匹配。

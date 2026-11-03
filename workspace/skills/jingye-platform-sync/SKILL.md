---
name: jingye-platform-sync
description: 敬业运输统一采集、事实入库与平台同步。用于完整触发采购/销售/PDF/运费确认抓取，将来源账号或选中车辆、原始 Excel 行、PDF 和延迟运费结果幂等写入 SQLite，生成附件并上传 OSS 和运输平台；也支持人工补抓指定采购范围和过往销售范围。用户提到“敬业平台同步”“敬业运输上传”“敬业数据补录”“补抓敬业数据”“补传敬业运单”“敬业司机信息”或检查敬业上传状态时使用。
---

# 敬业采集与平台同步

## 首选入口

OpenClaw 负责何时触发；Skill 不内置3小时、6小时或每天等调度规则。每次触发都完整执行一次统一驱动：

```powershell
python skills/jingye-platform-sync/scripts/jingye_pipeline.py --execute sync
```

`--execute` 必须放在 `sync` 或 `backfill` 子命令前。不带 `--execute` 时只输出日期、文件和全部步骤的计划，不创建 Excel、不登录、不上传、不发邮件，可用于定时任务配置检查：

```powershell
python skills/jingye-platform-sync/scripts/jingye_pipeline.py --anchor-date 2026-08-28 sync
```

生产默认读取 `skills/jingye-platform-sync/.env`，使用正式库、正式 OSS、正式平台和项目“敬业钢铁”。测试必须显式加 `--profile test`，并仍需显式 `--execute` 才会产生外部写入。

## 自动同步日期

脚本启动时冻结上海自然日 `D`，整个长任务即使跨过午夜也不变：

- 采购：`D-2..D`，通过 App 日期控件查询。
- 销售：`D-2..D`，通过列表预计出场时间同时应用下界和上界。

不要在 Skill 中再次计算或覆盖这些日期。需要复现实验日期时可在 `--execute` 前传 `--anchor-date YYYY-MM-DD`。

## 人工补数

### 交互触发

触发关键词：`敬业数据补录`、`敬业补数据`、`补抓敬业数据`。

首次收到关键词时不要运行命令，询问以下内容：

> 🧩 请提供采购补录起止日期，并选择销售范围：①提供销售起止日期；或 ②提供“截至执行当天（含当天）往前 N 天”。采购和销售都允许补录执行当天的数据，不适用敬业对账“截止昨天”的规则。例如：`采购 2026-08-10 到 2026-08-12，销售 2026-08-08 到 2026-08-12`；也可以说 `采购 2026-08-10 到 2026-08-12，销售往前5天`。

把日期规范为 `YYYY-MM-DD`。采购只给一个日期时起止相同。校验采购和销售的开始日期均不得晚于结束日期，结束日期允许等于执行锚点当天；不得把对账采购截止昨天的限制用于补录。`N` 必须是大于等于1的整数；信息缺失或不合法时只补问缺失项，不得猜测。

收到合法参数后，按上海自然日冻结执行锚点 `{anchor_date}`。销售选择“往前 N 天”时，解析为 `{anchor_date}-(N-1)..{anchor_date}`，包含执行当天。先向用户列出最终采购范围、最终销售范围，并说明这会执行真实抓取、入库、OSS 与平台上传以及邮件汇报；要求用户回复“确认补录”。如果用户在同一条消息中已经给全参数并明确说“确认补录”或“直接执行”，可以直接启动。

确认后在后台启动，不等待长任务完成：

```powershell
$py = 'C:\Users\SXL\OpenClawNative\python-venv\Scripts\python.exe'
$script = 'C:\Users\SXL\.openclaw-migration\workspace\skills\jingye-platform-sync\scripts\jingye_pipeline.py'
$baseDir = 'D:\openclaw_data\data\jingyeapp'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$stdout = Join-Path $baseDir "platform_backfill_${stamp}.out.log"
$stderr = Join-Path $baseDir "platform_backfill_${stamp}.err.log"

$pipelineArgs = @(
  '-u', '-X', 'utf8', $script,
  '--base-dir', $baseDir,
  '--anchor-date', '{anchor_date}',
  '--execute', 'backfill',
  '--purchase-start', '{purchase_start}',
  '--purchase-end', '{purchase_end}',
  '--sales-start', '{sales_start}',
  '--sales-end', '{sales_end}'
)

$process = Start-Process `
  -FilePath $py `
  -ArgumentList $pipelineArgs `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr `
  -WindowStyle Hidden `
  -PassThru

Write-Output "RESULT:STARTED|PID:$($process.Id)|LOG:$stdout"
```

交互层始终把“往前 N 天”先换算成明确的 `{sales_start}`、`{sales_end}`，再使用上面的统一命令，以便聊天确认范围、实际抓取范围和邮件范围完全一致。启动成功后只回复任务已启动、完成后会发送邮件，并给出启动日志路径；不要持续轮询长任务。流水线自身还会生成下述完整主日志。

### 直接命令行补数

程序漏跑数天时使用同一个完整流水线补抓；新事实入库，已存在事实和已成功上传记录不会重复：

```powershell
python skills/jingye-platform-sync/scripts/jingye_pipeline.py --execute backfill `
  --purchase-start 2026-08-10 --purchase-end 2026-08-12 `
  --sales-start 2026-08-08 --sales-end 2026-08-12
```

也可以让销售取以锚点日结束的连续 N 个自然日：

```powershell
python skills/jingye-platform-sync/scripts/jingye_pipeline.py `
  --anchor-date 2026-08-28 --execute backfill `
  --purchase-start 2026-08-10 --purchase-end 2026-08-12 `
  --sales-days 5
```

上例销售为 `2026-08-24..2026-08-28`。`--sales-days` 与显式 `--sales-start/--sales-end` 二选一。补数仍执行采购、销售、PDF、运费、本地事实刷新、附件、OSS 和平台上传，不是只写 Excel。

## 单次完整流程

`jingye_pipeline.py` 顺序执行并记录每步返回码：

1. 创建本次唯一四表 Excel。
2. 从 SQLite 把运单日期距本次冻结锚点不超过 14 个自然日（含第 14 天）的非 `COMPLETE` 历史运费目标和已有成功卡片，按原工作表、`login_account` 或 `selected_car` 精确写入本次运费种子；第 15 天起不再自动写入和查询。日期优先取 `YSYYYYMMDD...` 运单号中的日期，无法取得时回退销售 `app_export_time`；两者都无法解析时继续保留，禁止因日期未知静默漏单。已有记录用于幂等复用，但期限内的非完整运单仍按整个运单重新核对卡片集合。
3. 调用一次 `yyb_search.py`；每个账号沿现有登录流程依次处理采购、采购 PDF、销售、销售 PDF和运费确认，不为运费另开一次登录。运费以同一工作表、来源账号/选中车辆、车牌和运单为整体，沿列表方向每次只读取处理位中完整显示的一张实体卡。每张卡完成后最多执行一次大步推进，再用小步对齐，禁止连续两次大步。详情返回后先按业务内容和原屏幕槽位找回刚处理的卡并重新绑定当前元素 ID；本轮滚动期间严格用该 ID 跟踪旧卡，业务内容完全相同但 ID 不同且从旧卡上方进入的卡必须视为下一张实体卡。若 ID 变化却仍处于旧卡应在的移动区域，则报技术歧义并停止，不得猜测。稳定业务内容和坐标只用于判断列表是否实际移动，不用于合并实体卡。半遮挡卡片最多连续小步对齐 4 次，仍不能形成完整可遍历卡片时必须报技术错误，禁止无限滚动；几何完整的非目标卡允许只用运单和时间推进，目标卡则必须规格和详情按钮均完整后才能提取。实体卡序号按本次物理遍历顺序累计，不使用当前视口内会重置的临时序号；规格、结算量和单价全部相同的多张卡也必须按实际数量分别保留。
4. 即使爬虫中途失败，也先执行 `refresh` 保存已经落盘的 Excel、来源、PDF 和运费事件；爬虫失败时随后停止 OSS 和平台写入。
5. 成功时生成或复用长 JPG 附件，只上传尚无 OSS URL 的内容哈希对象；随后执行无网络 `dry-run`，把候选接受数、拒绝数和原因写入摘要。
6. 只提交尚未成功的平台记录；本地待完善候选或新增远端失败会把本次任务标为 `PARTIAL`，不会静默丢弃。
7. 无论成功、部分成功还是失败，都尝试发送一封流水线摘要邮件，并把最终状态和摘要写入 `crawl_runs`。

同一事实库由 `jingye_pipeline.lock` 防止两个完整任务并发。底层 `jingye_sync.py` 的短命令仍使用自己的事实库写锁。

销售入口从采购页返回后必须等待页面真实稳定，并识别园区主页、园区入口、业务中间层和销售页；短暂 unknown 只能等待或重新激活货运 App，不能返回空值并静默跳过。若刚从已识别的采购页或内层页面点击 iv_left，确认货运 App 仍在前台且持续 unknown 超过等待窗口，必须先记录当前包名、Activity、页面结构签名和资源 ID；仅在这些条件全部成立时，单次入口流程允许使用一次系统返回恢复层级，禁止无前置证据或连续系统返回。入口仍失败时重建 Appium 会话、重新登录并从该账号开头幂等重跑，单个账号最多完整执行三次；三次仍失败必须让爬虫返回非零，使流水线先 refresh 保存已取得事实后停止平台上传。

采购、销售和运费确认都要把“入口控件仍可见或点击已发送，但目标页面在有界等待后连续未出现”视为疑似账号被其他设备挤占，不得在原会话无限点击。运费入口状态机每个会话最多恢复 3 轮；销售入口保留正常跨越采购页、园区主页、园区入口和销售页所需的导航轮次，但停留在同一页面状态连续 3 次操作仍不跳转时必须立即升级为重登。达到对应上限后重建 Appium 会话并重新登录当前账号。河北采购或销售从当前账号开头幂等重跑，复用已写 Excel 与已归档 PDF；上海销售保留已完成车辆并从当前车辆继续；运费确认保留已完成范围并从当前账号/车辆的未完成目标继续。采购阶段直接抛出的 Timeout、NoSuchElement、StaleElement 或 WebDriver UI 故障同样进入账号级重登，单个账号总计仍最多三次完整执行。业务无数据、运费尚未生成、PDF 内容异常和数据解析异常不得伪装成会话故障重登。

PDF 下载保留三次尝试和每次结束后关闭 Honor 浏览器、切回货运 App、重新定位附件链接的清理机制。关闭浏览器前，只有确认 `com.honor.browser` 仍在前台时，才对本次外部打开的 PDF 链路执行最多三层的受限返回；浏览器内部 Activity 变化只代表退出一层，必须读取真机 `tab_switcher/toolbar_root` 的“X 个窗口”，窗口数减少或前台包真正离开 `com.honor.browser` 后才能判定本次链路已退出，随后继续 force-stop 浏览器释放进程。正常 Honor PDF 两层链路应在第二次返回时从 2 个窗口降为 1 个并立即停止，不得继续对浏览器主页执行第三次返回。禁止只 force-stop 而保留本次标签，也禁止清除浏览器数据、关闭非本流程标签或在货运 App 内执行该浏览器返回。App 详情页的磅单跳转元素出现后按原有方式立即点击，不在 App 内固定等待；进入 Honor 浏览器后，以 PDF 下载控件真实可见且可点击作为下载界面就绪标志，再稳定等待 2 秒并重新定位同一控件后点击。第一、第二次使用相同的自适应上限：浏览器出现 18 秒、下载控件出现 20 秒、确认弹窗 4 秒、PDF 写完 45 秒；第三次保持 25 秒、30 秒、5 秒和 60 秒的慢加载兜底。等待均在元素或完整 PDF 提前出现时立即进入下一阶段，除下载界面就绪后的 2 秒稳定窗口外不得增加固定长 sleep，也不得在文件仍增长或小于完整性阈值时拉取。

App 或浏览器确实没有提供 PDF 时，不把它伪装成登录失效，也不因此重登；保留销售或采购运单事实并标记“未找到PDF”，本轮禁止上传该运单。以后人工补录或定时任务只要取得并完整解析该运单的 PDF，`refresh` 就按同一运单幂等更新逻辑磅单，后续重新生成附件、上传 OSS 并把尚未成功的该运单提交平台，不要求先删除数据库中的待完善记录。

## 日志

每个带 `--execute` 的 `sync` 或 `backfill` 任务都必须由统一流水线自动生成 UTF-8 完整主日志，不依赖 OpenClaw 是否保存控制台输出：

```text
D:/openclaw_data/data/jingyeapp/platform_sync/logs/jingye_pipeline_<run_id>.log
```

主日志与 `crawl_runs`、邮件摘要使用同一个 `run_id`，记录冻结后的采购/销售范围、每一步开始与结束、返回码、耗时、子流程输出、最终摘要、邮件结果以及任务启动或加锁阶段的致命错误。日志路径同时写入数据库任务摘要和平台邮件。每次任务使用唯一文件，禁止覆盖或删除历史日志。

交互补录通过 `Start-Process` 产生的 `.out.log`、`.err.log` 是启动层日志，用于确认后台进程是否成功拉起；`platform_sync/logs` 中按 `run_id` 命名的文件是完整业务主日志。无 `--execute` 的计划检查不创建持久日志。

## 延迟运费状态

自动补查期限固定为 14 个自然日：运单日期当天为第 0 天，第 14 天仍查询，第 15 天起停止自动查询。超过期限的目标及已有事实继续保留在 SQLite，不改写成 `COMPLETE`，也不再写入日常任务的运费 JSONL 种子；日志必须输出 `targets_skipped_by_age`。期限按 `YSYYYYMMDD...` 日期优先、销售 `app_export_time` 回退；日期未知时继续查询，不能静默丢弃。期限内各状态处理如下：

- `PENDING`：已有销售目标，尚未完成一次有效遍历。
- `NOT_READY`：完整遍历未找到该销售运单；补查期限内下一次完整任务继续查。
- `PARTIAL`：已取得部分卡片、存在实体卡详情失败，或未能确认该运单卡片区域完整结束；已有记录幂等复用，补查期限内下一次完整任务重新核对该运单整体。
- `COMPLETE`：一次完整遍历中已确认该运单卡片区域边界、所有实体卡均读取成功，并且该运单没有剩余失败；后续永久跳过。不得仅因同 `card_key` 已有一条成功记录就标记完成。
- `ERROR`：技术或详情读取错误；补查期限内下一次完整任务继续查。
- `SOURCE_UNKNOWN`：缺少账号/选车证据；禁止猜测或跨范围查询。

河北目标以实际登录账号隔离，上海目标以实际选中车辆隔离。目标键同时包含工作表、来源类型、来源值、运单号和车牌；不得只按运单号合并。

## 去重与上传安全

- 正式事实库：`D:/openclaw_data/data/jingyeapp/platform_sync/jingye_facts.sqlite3`；测试库独立为 `jingye_facts.test.sqlite3`。
- Excel 行保留在 `waybill_observations` 和 `waybill_attributes`；来源证据保留在 `source_context_events`、`waybill_sources`；运费保留在 `freight_targets`、`freight_records`、`freight_failures`。
- Excel 重扫、来源 JSONL 重扫、运费 JSONL 重扫和 PDF 重扫均幂等。PDF 按 SHA-256 去重，附件对象名包含内容哈希。
- 平台请求先冻结到 SQLite outbox。`SUCCESS` 记录永久复用已经冻结的 `requestId`、`externalTicketNo` 和请求体，后续完整任务不重复 POST。当前候选内容发生变化时只记录变化字段名并跳过，不覆盖成功记录，也不得阻断后续新数据上传。
- 不把 API Key、OSS Secret、SMTP 授权码或模型密钥写入命令行、SQLite、报告或聊天。
- 真实 OSS 和平台写入只能通过明确的 `--execute` 完整驱动，或底层命令自身的 `--execute` 安全确认。

## 邮件

完整 `--execute` 任务结束后必须发送摘要。邮件使用自然中文说明任务是全部完成、部分完成还是失败，逐段写明任务编号、采购/销售窗口、各执行环节、本轮新增数据、平台校验与上传结果以及需要关注的问题；禁止把 `selected`、`accepted_without_submission`、`rejection_reasons` 等内部参数名直接作为正文。待完善数据必须逐项写出采购/销售类型、运单号、车牌和中文原因；缺少 PDF 时还要说明该运单本轮未上传、事实已保留，以及以后补录或定时任务取得完整 PDF 后会更新同一运单并继续上传。爬虫必须在末尾输出每个账号的结构化最终结果；邮件逐项列出成功账号和失败账号，但手机号只显示后四位，失败项同时注明失败阶段和完整尝试次数，不发送完整手机号或本地主日志中的详细异常正文。任一账号最终失败都必须让爬虫返回非零，流水线 refresh 已取得事实后停止上传，并在失败邮件中明确体现。自动同步与人工补录共用平台收件人，按 `--email-to`、`JINGYE_PLATFORM_EMAIL_TO`、兼容项 `SMTP_RECEIVER` 的顺序选择；不得读取 `JINGYE_RECONCILIATION_EMAIL_TO`。SMTP 连接继续读取 `SMTP_SERVER`、`SMTP_PORT`、`SMTP_AUTH_CODE` 和 `SMTP_SENDER`，缺失时复用现有邮件配置，不复制或打印授权码。多个收件人可用逗号、分号或空格分隔。邮件失败时任务最终状态为失败并返回非零。

在 `skills/jingye-platform-sync/.env` 中分别维护 `JINGYE_PLATFORM_EMAIL_TO` 和 `JINGYE_RECONCILIATION_EMAIL_TO`；前者只供本 Skill 使用，后者只供敬业对账使用。单次命令的 `--email-to` 只覆盖本次平台任务，不修改持久配置。

## 底层诊断命令

统一驱动用于日常调度和补数。只做只读/本地诊断时可直接使用：

```powershell
python skills/jingye-platform-sync/scripts/jingye_sync.py refresh
python skills/jingye-platform-sync/scripts/jingye_sync.py status
python skills/jingye-platform-sync/scripts/jingye_sync.py dry-run
python skills/jingye-platform-sync/scripts/jingye_sync.py attachments --format jpg
```

司机主数据仍来自 `D:/openclaw_data/data/jingyeapp/platform_sync/driver_roster.csv`，列为 `plate_no,driver_name,driver_phone,source_owner,active`，用 `jingye_sync.py drivers-import` 导入。不得把司机姓名和手机号写入对账 Excel。

单条调查或平台管理员明确删除远端记录后的修订，继续使用 `jingye_sync.py query`、`mark-unuploaded` 等底层安全命令；没有明确的远端删除证据时不得重置本地成功状态。

## 与对账的边界

本 Skill 是唯一爬取入口并维护共享事实库。`jingye-reconciliation` 只读该库生成对账快照，不启动 Appium。发现历史缺口时先运行本 Skill 的 `backfill`，待入库完成后再重新触发“敬业对账”。

## 依赖

Python 依赖包括 `openpyxl`、`requests`、`Appium-Python-Client`、`selenium`、`PyMuPDF`、`Pillow` 和 `oss2`。外部执行需要 Appium、ADB、手机 App、模型接口、OSS、平台 API 和 SMTP 按实际步骤可用。

## 采购承运商补充规则

仅采购采集与表格增加“承运商”（货物名称后第5列，采购20列，销售19列）。已有采购单再次入库只补空承运商，已有承运商和其它业务字段均不覆盖；新采购完整写入。字段保存在动态属性 `excel.承运商`，原始观察与采购有效行基线分别保留，旧单未实际重采到时保持缺失。

新采购平台请求的 `loadingPointName` 取承运商，遵循现有字符串及128字符上限，不截断、不改原始装货地点，也不改变采购仅 `unloadingTicket` 的规则。待上传采购缺承运商时列为待完善；SUCCESS采购始终使用冻结请求并跳过，不触发新必填校验或重复上传。任何历史冻结请求（包括无POST审计的PREPARED）均不自动补改装货点，内容冲突逐单待核实；只有没有旧提交记录的新采购生成含承运商的新请求，不改requestId绕过去重。销售规则不变。

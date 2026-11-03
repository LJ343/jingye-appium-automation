# 敬业运单业务事实模型

## 数据分层

- `waybill_observations.row_json`：保存每次 Excel 导入的完整原始行，作为对账和审计依据。
- `waybills`：保存稳定公共字段和最新非空来源快照，不存平台派生地点。
- `waybill_attributes`：自动保存所有非空 Excel 列的最新值。已知表头映射为稳定英文键；未知新列使用 `excel.{规范化表头}`，因此新增对账列不需要迁移数据库。
- `documents`、`waybill_documents`：保存 PDF 内容事实及多对多关联。
- `logical_tickets`：保存 PDF 解析、聚合后的单侧磅单，包括标准化的 `entry_time`、`weigh_time`、`exit_time`。
- `v_waybill_business_facts`：把来源字段和磅单字段映射成统一装卸业务字段，供上传、导出和后续对账读取。
- `driver_roster`：独立保存上传专用的车牌、司机姓名和手机号，不进入对账事实视图。
- `submissions`：保存已经冻结的平台请求；来源字段后续变化不得静默覆盖冻结请求。

正式环境与测试环境使用不同 SQLite：正式主库为 `jingye_facts.sqlite3`，测试库为 `jingye_facts.test.sqlite3`。两者可以共享本地 PDF 和 OSS 内容哈希，但不得共享 `submissions`、`upload_attempts` 和 `submission_revisions` 状态。

## 标准字段

`waybills` 的标准来源快照包括：

- `source_loading_date`
- `source_unloading_date`
- `source_loading_point_name`
- `source_unloading_point_name`
- `load_weight_kg`（Excel 装货重量）
- `unload_weight_kg`（Excel 卸货重量）

采购和销售共用一张表，不建立重复的方向专用表。

## 统一业务映射

销售：

- 装货点：优先取 Excel 装货点；为空时取 `JINGYE_SALES_LOADING_POINT_NAME`。
- 卸货点：取 Excel 卸货点。
- 装货时间、装货重量：取销售逻辑磅单的 `weigh_time`、`net_weight_kg`。
- 平台磅票：只生成 `loadingTicket`；`entry_time` 取 PDF 皮重行时间，`exit_time` 取 PDF 毛重行时间。
- 货物名称：平台 `cargoName` 始终取 Excel 运单货物名称。销售多阶段 PDF 货名不同时，保留各 PDF 原货名作审计，继续按时间和重量链聚合，并把全部 PDF 合成一个附件；不得因此放宽车牌、解析状态、重量链或 Excel 装货重量校验。
- 卸货时间、卸货重量：若未来 Excel 提供，则从标准来源字段读取。

采购：

- 装货日期、装货重量：取 Excel 来源字段。
- 卸货点：SQLite 保留 Excel 原值；平台字段把配置前缀与原值直接拼接，不加入空格、短横线等分隔符。
- 卸货时间、卸货重量：取采购逻辑磅单的 `weigh_time`、`net_weight_kg`。
- 平台磅票：只生成 `unloadingTicket`；`entry_time` 取 PDF 毛重行时间，`exit_time` 取 PDF 皮重行时间。

`documents.gross_time`、`tare_time`、`net_time` 始终保存 PDF 标签对应的原始事实，不因方向改名；进出厂语义只在 `logical_tickets` 和平台 payload 层派生，因此不会改变现有对账字段。

## 地点变化

按 Excel 文件修改时间从旧到新扫描；同一运单的新非空地点更新 `waybills` 最新快照。每次来源行仍保留在 `waybill_observations`，动态属性还保存其 `source_observation_id`，可以追溯字段来自哪条 Excel 记录。

已经 `prepare` 的请求保持冻结。若地点在冻结后变化，不要自动覆盖；先核对平台状态，再走明确的请求修订流程。

## 未来新增 Excel 字段

新列会自动进入原始 `row_json` 和 `waybill_attributes`。查询最新值：

```sql
SELECT w.direction, w.waybill_no, a.field_key, a.source_column, a.value_text
FROM waybill_attributes a
JOIN waybills w ON w.id=a.waybill_id
WHERE a.field_key='excel.新增字段的规范化名称';
```

若字段只用于对账，直接按属性键读取，无需改 schema。若字段需要进入统一业务视图或平台 JSON：

1. 把其常用表头加入对应 alias 列表，或增加一个稳定 canonical key。
2. 在 `v_waybill_business_facts` 中增加清晰别名。
3. 仅在平台接口确实定义该字段后修改 payload 映射。
4. 对历史数据执行 `refresh` 回填，并做 dry-run 与冻结请求差异检查。

不得把未知 Excel 列未经接口确认就自动透传到平台 JSON。

## 上传专用司机快照

- 以完整新能源车牌（例如 `冀Z00007`）作为 `driver_roster.plate_no` 主键。
- 优先精确匹配；只有运单车牌不以 `D` 结尾且精确匹配失败时，才尝试“原车牌加 D”的受控兜底。
- 不修改 `waybills.plate_no`，不向平台发送本地 `source_owner`。
- CSV 缺行不自动删除 SQLite 旧绑定；车辆换司机时更新同车牌行，停用时显式设置 `active=0`。
- 只把接口已经定义的 `driverName` 和 `driverPhone` 合并到尚未冻结的上传 payload。
- 已冻结请求继续保留原司机快照；司机资料更新不得静默改写 `submissions.payload_json`。

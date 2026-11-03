# USB有线基线与运行环境

## 设备及链路

- 手机：HONOR ANN-AN00，Android15，MagicOS9.0.0；只记录型号，不公开真实设备串号。
- 敬业App：包com.ibb.tizi，V2.3.4.3；启动Activity为com.ibb.tizi.activity.LoginActivity。
- 原链路：OpenClaw → 本机Appium → USB ADB → 手机UiAutomator2。
- Appium默认地址http://127.0.0.1:4723；手机控制与PDF拉取、端口转发共用ADB_SERIAL。
- 原有能力参数包括noReset=true、newCommandTimeout=7200、adbExecTimeout=60000。noReset保留数据，不代表跳过每账号登录。
- Appium固定Node为22.22.2；OpenClaw使用另一套Node24.21.0，不能混为同一个服务运行环境。

## 文件布局和配置

`workspace`作为原技能工作目录，两个技能保持在其skills下同级。同步pipeline调用对账目录的creat_excel.py和yyb_search.py，动态读取result_check.py的邮件默认配置。对账复用同步侧的事实库及本地配置。

`.env`、司机主数据、SQLite、原始/结果Excel和PDF均保留在用户本机，不属于本仓库。生产与测试profile、业务日期、输出和邮件合同以当前SKILL为准。本次未为发布重构配置加载或业务流程。

`ADB_SERIAL`是原有环境覆盖入口；归档副本默认串号为占位。所有Appium和ADB操作必须指向同一设备。使用无线时不能只修改Appium服务器URL；无线地址和端口也尚未在本基线配置。

## 启动及长期进程

Appium需要长期常驻。operations中的Start/Status/Watch/Logs/Stop只是原有管理工具的源码归档，发布过程没有执行这些操作。Status.cmd实际依赖Appium-Manage.ps1，Start.cmd依赖Appium.ps1，故一并保留。

Run-JingyeSyncWait.ps1和WaitAdapter.Core.ps1是2026-11-02经用户授权、仅用于08:30/23:50两项同步cron的临时外层适配：仍执行原run_daily_sync.ps1，仅等待同一个进程自然结束并传递真实退出码；不是新调度器、自动重试或登录风控修复。原脚本SHA固定校验不得绕过；根因查明、修复并验证后，按本机独立备份恢复原cron消息及临时规则。仓库没有发布整个OpenClaw配置或账号交付定义。

## 本次验证范围

完成：源码导出、来源/导出SHA256清单、Python静态语法解析、已安装依赖版本整理、敏感信息独立审查。

未执行：真实抓取、登录、数据上传、发邮件、cron重跑或服务启停。仓库中原有测试文件一并归档，但没有把它们标记为本次全部通过。

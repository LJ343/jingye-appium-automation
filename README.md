# Wireless release: local deployment verified

The release owner has deployed the reviewed wireless runtime locally and passed independent postdeployment verification. See the [wireless release record](docs/WIRELESS_RELEASE.md) for scoped tests, the recovered account crawl and ongoing, not completed three-hour connection observation. USB main remains the preserved baseline. This staging exporter has performed no Git publication. The USB baseline description below remains historical context.

# 敬业 Appium 自动化

归档敬业平台同步与敬业对账两项技能的 **USB有线基线**。基线日期：2026-11-03，Asia/Shanghai。

这是从现有原生 OpenClaw 工作区导出的脱敏副本。本机生产文件没有修改；硬编码密钥已移除，账号、私人邮箱、设备唯一串号及车牌已用一致的演示值替换。**仓库不是含完整生产配置的即用备份，也不代表当前登录风险已解决。**

## 包含的流程

- `workspace/skills/jingye-platform-sync`：敬业运输采集、事实入库、附件及平台同步。
- `workspace/skills/jingye-reconciliation`：读取同一事实库完成对账；对账入口本身不启动Appium或重抓。
- `operations`：自有Appium运维入口、当前临时外层等待适配。没有打包官方OpenClaw/Appium源码。

两技能需保持同级目录。同步会直接使用对账目录的建表、爬虫和邮件默认配置，不能拆开使用。具体业务步骤、日期参数和交付仍以各技能SKILL.md为准。

## 基线设备与连接

| 项目 | 本次归档依据 |
| --- | --- |
| 手机 | HONOR ANN-AN00 |
| 系统 | Android 15 / API 35，MagicOS 9.0.0 |
| App | 敬业运输司机端 V2.3.4.3，`com.ibb.tizi` |
| 手机连接 | USB有线ADB；当前有线功能含MTP、手机助手及ADB |
| Appium | 3.5.2，UiAutomator2 driver 7.6.2 |
| ADB | Platform-tools 37.0.0 |
| Python | 3.12.10 |
| OpenClaw | 原生2026.10.5，已正式切换后的工作区基线 |

完整说明见 [USB基线及运行环境](docs/USB_BASELINE.md)、[当前已知问题](docs/KNOWN_ISSUES.md)、[导出脱敏说明](docs/REDACTION.md)。

## 配置与执行边界

仓库不含真实`.env`、邮件凭据、平台/OSS密钥、司机主数据、数据库、PDF、Excel、手机截图、安装包或会话日志。只提供原有`.env.example`供理解配置项；演示账号不能直接用于生产。

原有PowerShell入口保留了归档机器的绝对路径，执行前需逐项核对。克隆仓库或安装依赖不会启动业务；不要把归档副本的路径当作另一台机器的生产路径。

`requirements-observed.txt`是当时安装环境中直接依赖的观测版本，不是跨平台锁文件；本次只做静态解析、依赖和脱敏审查，没有重跑业务或对外发送邮件/上传业务数据。

## 后续分支

`main`保存本次USB基线；无线ADB尚未完成登录、传输完整性及长时间恢复验证。待验证完成后再建立独立无线分支保存适配代码与真实测试记录，不把未测试方案写为已可用。

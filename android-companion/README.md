# 敬业连接恢复助手 0.2-review：已部署修复版

本导出仅含显式允许的源码、离线测试与构建脚本，不含 APK、SDK 依赖、日志、诊断回执或签名材料。7 个 Manifest/Java 文件对应已审源码 aggregate `6a522773afb52f5a3520dd2bacf341ed1e3259833fb06e0406dbaac487866b5b`，31 项实际状态机测试通过。同证书 APK 已由主任务另行构建；已同包同签名升级并核验手机base.apk字节、可见启动与前台服务；本次已完成受控无线中断后的同机连接恢复、会话创建前捕获故障收口和两项电脑运行源部署。对应APK与校验文件通过仓库Release单独提供；这些有限验收不等同长期无人值守保证。本版修复早期复位后永久撤销监听的问题，改为只读等待外部开关恢复，不循环写开关，不批准未知网络。不承诺 24 小时常驻、手机重启恢复、系统强杀后恢复或未知网络自动确认。

包名 `com.lj343.jingye.connectionguard`，目标 API 35，最低 API 31，纯 Java，无 Gradle、AndroidX 或第三方库。Manifest 明确 `debuggable=false`，不使用调试包与 `run-as`。

## 作用和边界

用户在可见页面点击“开始 / 恢复一次”后，启动 `connectedDevice` 前台服务。服务观察 Wi-Fi 网络是否存在，以及唯一设置键 `Settings.Global.adb_wifi_enabled`。它不读取 SSID、BSSID、Wi-Fi 密码、ADB 钥匙、账号、文件内容或业务数据。

当无线调试关闭且存在 Wi-Fi，先等待 4 秒，再正常调用 `Settings.Global.putInt(..., "adb_wifi_enabled", 1)` 一次。Android 原有的接入点信任检查继续执行：新接入点如需系统确认，助手不点击、不批准、不改信任记录。`1` 只代表系统设置开关，不代表 TLS 端口已经可用，也不代表电脑已经连上。

写入后观察 12 秒；若仍关闭，或期间从开启被复位为关闭，进入只读 `WAIT_EXTERNAL_ON`。保留用户启动意愿、前台通知、监听以及本次已消费预算；不重复写开关，不自动批准接入点。只有实际观察到 Wi-Fi 可用且系统开关为 1，才开始 30 秒只读稳定观察；期间关闭或失去 Wi-Fi 就重新等待，网络失去/恢复不会清除本次预算。完整观察后清除已消费标记并进入 `OPEN_PENDING_PC`，电脑仍需独立核实连接。重复回调不重置或增殖计时器，等待不持唤醒锁。

正常一次写入后开关持续维持到写入后 30 秒，也可接受下一次独立关闭事件。`attemptConsumed=true` 和 `armed=true` 已持久化；服务重建会重新进入只读等待，即使当前开关已开启，也重新观察完整 30 秒，不补写。没有读取 BSSID，因此“稳定”仅指观察到的 Wi-Fi 可用及开关状态，不能证明一直关联同一接入点。系统通知延迟、服务被结束等仍需实机验证。

按系统时间计的任意 10 分钟最多写入 3 次，次数时间记录在私有数据中，服务重建、外部恢复重新布防和人工点击恢复均不清除；达到限制、权限缺失、设置写入被拒绝或系统接口异常仍硬暂停，不因开关后来开启而自动复活。须排查后由用户恢复。系统时钟回拨时保守保留未来时间记录；时钟前跳可能提前清除窗口，本版未验收调钟故障。不会通过自动反复恢复来批准未知网络。

服务初始化、启动、观察回调及延时任务均有 RuntimeException 边界。OEM 系统接口出错时先锁定禁止写入，再尽力持久化撤销恢复意愿、取消状态机与计时、分别撤销观察并释放锁，显示固定 `PLATFORM_EXCEPTION`，停止服务且不请求 sticky 重启。清理单项失败不妨碍其他项，异常文本不入日志。若存储接口本身故障，不能保证错误记录已落盘；诊断包含本进程最近写入结果和本进程观察到异常的标志。该标志为历史观察，不等同当前连接情况。

用户主动选择启用后，助手可能恢复手动关闭的无线调试。想保持调试关闭，必须先点击“暂停并停止助手”，或常驻通知的同名按钮。停止会撤销网络回调与设置观察者、取消计时、释放唤醒锁；停止后不写设置。暂停/停止不会主动把无线调试关闭，保留手机当前开关状态。

首次安装、仅打开页面、查询诊断状态都不会开始恢复。没有 BOOT receiver、开机自动启动或远程启动接口。`START_STICKY` 只请求系统在进程被结束后尝试重建，恢复之前保存的用户意愿及限频记录，不能保证荣耀系统永不结束进程。对端 ADB 断开而系统开关仍为 1 时，助手不会认为电脑已恢复，也不代替电脑侧发现、身份核实和 Appium 会话重建。

## 权限审阅

| 权限 | 用途 |
| --- | --- |
| `WRITE_SECURE_SETTINGS` | 开发权限，需要经用户单独批准后由 ADB 显式授予。代码唯一设置写入点仅写 `adb_wifi_enabled=1`。权限本身范围很大，必须只安装已审阅、可靠签名的本工具。 |
| `ACCESS_NETWORK_STATE` | 查看是否有 Wi-Fi transport，不读取网络身份。 |
| `CHANGE_NETWORK_STATE` | 满足 `connectedDevice` 前台服务类型的权限前提，代码没有改变网络的 API 调用。 |
| `FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_CONNECTED_DEVICE` | 用户主动启动后维持前台服务及常驻通知。 |
| `POST_NOTIFICATIONS` | 用户开始时经系统请求，保留可见状态与停止入口；拒绝时不启动。 |
| `WAKE_LOCK` | 只在 4 秒稳定等待及 12 秒观察窗口中使用；每次系统强制超时最多 25 秒，结束、暂停、断网或停止均释放。30 秒重新接纳间隔不持锁。 |

没有 INTERNET、定位、存储、账号、Accessibility、Root、USB 模式更改、网络信任修改、后台定位或电池豁免申请权限。系统设置页面入口只是打开本应用的常规信息页，不自动修改任何电池策略。

## 可见诊断

页面提供刷新和复制诊断；诊断只含固定状态码、时间、计数、最近 40 条状态变化。保存在本应用私有 SharedPreferences，不发送网络。复制会放入系统剪贴板，只在用户点击时执行。

另有只读 ContentProvider：`content://com.lj343.jingye.connectionguard.status/status`，读权限要求系统 `android.permission.DUMP`，用于已授权的 shell 检查。只接受没有 projection/selection/query 参数的 `/status` 查询；insert/update/delete 一律拒绝，查询不启停服务、不写设置、不触发恢复。普通第三方应用没有 DUMP 权限。状态来自最近一次上报，系统强杀后可能过时，所以字段以 `last_report` 命名，必须核对上报时间及实际设备连接，不能据此宣称进程存活。

## 构建与离线测试

`test-offline.ps1` 只编译并运行本候选实际 `RecoveryController` 与 `FailureBoundary` 的确定性事件测试，不依赖 Android SDK，也不连接手机。当前 31 项全部通过。除原有默认、停止、异常和限频测试，还覆盖早复位后外部开启完整 30 秒、长期关闭零额外写入、稳定期间断网/再次关闭、重复事件、硬暂停不自动复活，以及服务重建仍保留预算和限频。已观察到 OFF 或断网时先取消稳定计时，不让紧随的恢复读取抹除这次事件；计时到点的负向读也先重置状态，避免留下没有计时器的观察阶段。Android 封装层仍需独立源码审阅和实机故障测试，离线测试不模拟 OEM 接口。

`build-unsigned.ps1 -AndroidJar <已验证的 API35 android.jar>` 使用本机 JDK/build-tools 离线编译并生成未签名 APK。脚本没有下载、签名、ADB、安装或授予权限步骤。签名和验收另外进行，私钥不能进入仓库；后续更新必须复核同一签名及 APK 哈希。编译打包是否通过应以实际构建结果为准。

## 用户批准后才可进行的手机步骤

以下只是审阅清单，本目录准备阶段没有执行。用户本次已授权修复和发布 APK；具体签名、安装、手机故障测试和发布均由主任务核验执行，不能因源码离线通过就当作实机通过。

1. 核实 APK 源码、签名、SHA256、权限清单及手机身份，安装已签名候选；先确认安装和打开页面均不会改变无线调试。
2. 用户批准后，以明确选中的同一台设备执行 `pm grant com.lj343.jingye.connectionguard android.permission.WRITE_SECURE_SETTINGS`；通知权限在用户点击开始时经系统页面请求。不给其他系统权限。
3. 在可见页面点击开始，再受控模拟“开关被关闭 / 网络暂断后恢复 / 开关被系统立即复位 / 第二次独立故障 / 高频熔断 / 停止后不写 / 进程被结束及重建”。电脑端独立核实手机身份、TLS 连接和 Appium，不把开关 1 当验收成功。
4. 若需荣耀后台保活或电池策略调整，另列实际设置与影响，请用户明确批准后测试。仍不得宣称能免除关机、重启、系统强停、Wi-Fi 彻底断网等硬中断。
5. 手机侧通过后，才能与已有电脑侧最小修复候选做长任务和文件完整性验收，正式定时切换另有证据与回退方案。

## 暂停、撤销和卸载

先通过页面或常驻通知点击“暂停并停止助手”，核对状态为 `STOPPED / USER_STOPPED`，且实际开关保持用户选择。之后可在系统“应用信息”中强行停止或卸载。

如需保留安装但撤销开发权限，在已授权且已核对身份的 ADB 会话中执行 `pm revoke com.lj343.jingye.connectionguard android.permission.WRITE_SECURE_SETTINGS`；权限撤销本身不会主动关闭现有无线调试，开关仍由用户在系统设置中处理。不得为执行撤销而未经授权重新开启已关闭的 ADB。卸载会删除本应用私有诊断，不修改系统网络信任或业务数据。

## 依据

Android 15 的正常设置观察、adbd 启动和 BSSID 信任链来自官方 [AdbService](https://android.googlesource.com/platform/frameworks/base/+/refs/heads/android15-release/services/core/java/com/android/server/adb/AdbService.java) 与 [AdbDebuggingManager](https://android.googlesource.com/platform/frameworks/base/+/refs/heads/android15-release/services/core/java/com/android/server/adb/AdbDebuggingManager.java)。前台服务权限依据 [connectedDevice 类型说明](https://developer.android.com/develop/background-work/services/fgs/service-types#connected-device)。这些提供可试验的框架依据，荣耀设备的实际行为仍需实测。

新增只读等待及30秒计时边界经过31项作者和9项独立纯Java测试；本次未在荣耀实机单独重现这些边界，不将受控会话恢复冒充所有系统恢复情形通过。

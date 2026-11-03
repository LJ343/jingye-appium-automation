# Appium 启动、观察与关闭

这些是原 Appium 3.5.2 的外部运维入口，不修改业务技能、OpenClaw 源码或手机业务流程。

| 入口 | 用途 |
| --- | --- |
| `Appium-Start.cmd` | 后台启动，保留 `--allow-cors`，日志写入 `D:\openclaw_data\logs\appium`；已有4723监听则拒绝重复启动。 |
| `Appium-Status.cmd` | 检查一次：服务HTTP状态、进程、相关业务进程（含Python和启动器）、匹配本次启动的日志实际字节数。 |
| `Appium-Watch.cmd` | 每轮检查完成后等待5秒，再刷新同样的信息。关闭窗口或按 Ctrl+C 仅结束观察窗口。 |
| `Appium-Logs.cmd` | 显示本次启动的 stdout/stderr 日志末尾并继续追加显示。关闭窗口或按 Ctrl+C 仅结束日志查看，Appium继续运行。 |
| `Appium-Stop.cmd` | 空闲时人工关闭。存在相关业务进程或身份无法核实时拒绝；通过核验后仍需输入 `STOP-APPIUM-屏幕显示的PID`。 |

## 怎样判断状态

- `Server HTTP: 200; ready: True`：Appium服务可以响应；它不证明手机任务或业务数据处理成功。
- `Business process PIDs` 有数字：检测到相关业务程序或启动器还在；程序存在也不等于正在推进。
- `Log bytes` 在 Watch 多次刷新中增加：Appium仍有日志活动。日志里正常请求和响应、结合业务采集进度，才能判断业务是否在推进；仅日志增长也可能是重复失败或重试。
- 服务超时/报错、或长时间没有业务进展：保留现场，核对业务日志和Appium日志；不要仅根据安静时长自动杀进程或反复启动同一业务。
- `stderr.log` 为0可以是正常现象；常规Appium请求日志在 `stdout.log`。Status读取共享文件句柄的实际长度，避免只看目录信息误认为日志为0。

业务采集/上传/邮件最终是否完成，应以该业务原流程的结束状态和交付记录确认。长任务可以运行数小时；这些观察入口没有业务超时终止、自动重启或重跑动作。

## 关闭前的条件

只在手机采集和其他使用Appium的客户端任务全部结束、且即将到来的定时任务不会同时启动手机业务时，运行 `Appium-Stop.cmd`。不要在当前敬业补录运行期间关闭。

Stop先核对4723唯一监听、固定Node/Appium入口、程序创建时间和业务进程，发现相关Python仍在就拒绝，不会结束这些业务进程。通过初检后，用户还必须自行确认手机和第三方客户端已空闲，并按提示输入完整确认文字；随后再次核对，才仅终止原来固定的Appium进程，不杀进程树。

`stop_refused_business_python_is_active` 表示业务进程仍在，请等业务结束。`stop_refused_unknown_python_identity` 或 `stop_refused_unknown_launcher_identity` 表示当前权限不能核实某些进程，不能登记为空闲。2026-11-01 15:14现场仍有旧PowerShell PID9744，其命令身份不可读；不会为方便关闭而忽略。若业务已全部结束但仍遇此提示，可右键以管理员身份运行本次Stop入口以完成身份读取，或先自行核实并关闭已不用的旧命令窗口；不要在补录期间处理。管理员运行不是自动授权关闭，仍有身份核对及人工确认。

本入口没有查询或证明Appium设备会话为空，也没有为了查询会话而开放额外服务功能。关闭是用户确认后的单进程终止，不是优雅HTTP关机。取消确认不会关闭。`-CheckOnly`只检查，无确认、无终止。

Status/Watch/Logs是只读的。Stop没有自动执行机制，拒绝由OpenClaw普通exec环境直接调用实际关闭；这是一层运维防误用，不是操作系统权限隔离。手动启动及关闭入口均不要求扩大D盘ACL。

## 本次实际验证（2026-11-01）

14:57用户以新入口启动Appium，之后只读检查确认固定Node、Appium3.5.2及 `--allow-cors`。15:18服务仍返回HTTP200/ready=true，日志实际持续增长，新补录四个Python仍在，业务日志已有4次Appium会话可用、尚未完成。关闭入口仅做语法和 `Stop -CheckOnly` 校验，因业务仍在而正确拒绝；没有实际执行关闭或重启。Watch/Logs无限交互循环仅代码审阅，本次未在后台启动；它们均不含自动停机或重跑动作。旧任务的观察器已自然结束，不表示正在持续监控这轮新补录。有限验证见[本轮回执](../migration/review-20261101-jingye-investigation/appium-live-followup/operations-final-validation.json)。

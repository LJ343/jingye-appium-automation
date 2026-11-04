# 敬业数据补录 - 每周三执行，自动计算前一周日期范围
$env:PYTHONIOENCODING = 'utf-8'
$py = 'C:\Users\YOUR_USER\OpenClawNative\python-venv\Scripts\python.exe'
$script = 'C:\Users\YOUR_USER\.openclaw-migration\workspace\skills\jingye-platform-sync\scripts\jingye_pipeline.py'
$baseDir = 'D:\openclaw_data\data\jingyeapp'

# 执行当天日期
$today = Get-Date
# 昨天作为结束日期
$end = $today.AddDays(-1)
# 昨天往前推6天（共7天范围：上周同日到昨天）
$start = $end.AddDays(-6)

$purchaseStart = $start.ToString('yyyy-MM-dd')
$purchaseEnd = $end.ToString('yyyy-MM-dd')
$salesStart = $purchaseStart
$salesEnd = $purchaseEnd
$anchorDate = $today.ToString('yyyy-MM-dd')

$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$log = "$baseDir\backfill_log_$ts.txt"
$err = "$baseDir\backfill_err_$ts.txt"

$argString = "-X utf8 -u `"$script`" --base-dir `"$baseDir`" --anchor-date $anchorDate --execute backfill --purchase-start $purchaseStart --purchase-end $purchaseEnd --sales-start $salesStart --sales-end $salesEnd"

$proc = Start-Process -FilePath $py -ArgumentList $argString -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err -PassThru
Start-Sleep -Seconds 3
$proc.Refresh()
if ($proc.HasExited) { Write-Output "FAILED ExitCode=$($proc.ExitCode)" } else { Write-Output "STARTED PID=$($proc.Id) DATE_RANGE=$purchaseStart ~ $purchaseEnd" }

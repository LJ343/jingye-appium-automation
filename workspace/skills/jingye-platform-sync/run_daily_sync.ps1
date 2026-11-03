$env:PYTHONIOENCODING = 'utf-8'
$py = 'C:\Users\SXL\OpenClawNative\python-venv\Scripts\python.exe'
$script = 'C:\Users\SXL\.openclaw-migration\workspace\skills\jingye-platform-sync\scripts\jingye_pipeline.py'
$baseDir = 'D:\openclaw_data\data\jingyeapp'
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$log = "$baseDir\cron_log_$ts.txt"
$err = "$baseDir\cron_err_$ts.txt"

$proc = Start-Process -FilePath $py -ArgumentList "-X utf8 -u `"$script`" --base-dir `"$baseDir`" --execute sync" -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err -PassThru
Start-Sleep -Seconds 3
$proc.Refresh()
if ($proc.HasExited) { Write-Output "FAILED ExitCode=$($proc.ExitCode)" } else { Write-Output "STARTED PID=$($proc.Id)" }

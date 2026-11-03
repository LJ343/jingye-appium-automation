param(
    [ValidateSet('Status','Watch','Logs','Stop')][string]$Action='Status',
    [switch]$CheckOnly,
    [switch]$Json
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$Node='C:\Users\SXL\AppData\Local\nvm\v22.22.2\node.exe'
$Cli='C:\Users\SXL\AppData\Local\nvm\v22.22.2\node_modules\appium\index.js'
$LogRoot='D:\openclaw_data\logs\appium'
$NativePython='C:\Users\SXL\OpenClawNative\python-venv\Scripts\'
$SkillRoot=Join-Path (Split-Path $PSScriptRoot -Parent) 'workspace\skills\'

function Read-Server {
    $lines=@(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'Appium.ps1') -Action Status)
    if($LASTEXITCODE -ne 0){throw 'appium_status_query_failed'}
    ($lines -join [Environment]::NewLine) | ConvertFrom-Json
}
function Read-Business {
    $busy=@(); $unknown=0; $unknownLauncher=0
    foreach($item in @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='pypy.exe' OR Name='pypy3.exe' OR Name='powershell.exe' OR Name='pwsh.exe' OR Name='cmd.exe' OR Name='node.exe'" -ErrorAction Stop)){
        $image=([string]$item.ExecutablePath).Replace('/','\')
        $command=([string]$item.CommandLine).Replace('/','\')
        if([string]::IsNullOrWhiteSpace($image) -or [string]::IsNullOrWhiteSpace($command)){
            if($item.Name -in @('python.exe','pythonw.exe','pypy.exe','pypy3.exe')){$unknown++}else{$unknownLauncher++}
            continue
        }
        if($image.StartsWith($NativePython,[StringComparison]::OrdinalIgnoreCase) -or
           $command.IndexOf($SkillRoot,[StringComparison]::OrdinalIgnoreCase) -ge 0 -or
           $command.IndexOf('C:\Users\SXL\.qclaw\',[StringComparison]::OrdinalIgnoreCase) -ge 0){
            $busy += [int]$item.ProcessId
        }
    }
    [ordered]@{active_business_pids=@($busy);unknown_python_identities=$unknown;unknown_launcher_identities=$unknownLauncher}
}
function Read-LogFiles($Server){
    # A directory entry can report zero bytes while a file is still open.
    # Read the shared file handle length instead of trusting FileInfo.Length.
    $candidates=@()
    if($Server.owners.Count -eq 1 -and $Server.owners[0].created_utc -and (Test-Path -LiteralPath $LogRoot -PathType Container)){
        $born=[DateTime]::Parse($Server.owners[0].created_utc).ToUniversalTime()
        foreach($item in @(Get-ChildItem -LiteralPath $LogRoot -File -Filter '*.stdout.log')){
            if($item.Name -notmatch '^([0-9]{8}T[0-9]{9}Z)-[0-9a-f]{32}\.stdout\.log$'){continue}
            $at=[DateTime]::ParseExact($Matches[1],"yyyyMMdd'T'HHmmssfff'Z'",[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::AssumeUniversal).ToUniversalTime()
            if($at -lt $born.AddSeconds(-10) -or $at -gt $born.AddSeconds(2)){continue}
            $candidates += $item.FullName
        }
    }
    if($candidates.Count -ne 1){return @()}
    $paths=@($candidates[0],($candidates[0] -replace '\.stdout\.log$','.stderr.log'))
    $facts=@()
    foreach($path in $paths){
        $stream=$null
        try{
            $stream=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            $facts += [ordered]@{path=$path;bytes=$stream.Length;readable=$true}
        }catch{$facts += [ordered]@{path=$path;bytes=$null;readable=$false}}
        finally{if($stream){$stream.Dispose()}}
    }
    $facts
}
function Read-View {
    $server=Read-Server
    $business=Read-Business
    [ordered]@{observed_at=(Get-Date -Format o);server=$server;business=$business;logs=@(Read-LogFiles $server);
        device_sessions='not_queried_or_proven_empty';business_progress='not_verified_by_server_status'}
}
function Show-View($View){
    if($Json){$View | ConvertTo-Json -Depth 10;return}
    Write-Host ('Time: '+$View.observed_at)
    foreach($owner in $View.server.owners){Write-Host ('Appium PID: '+$owner.pid+'; created UTC: '+$owner.created_utc)}
    if($null -ne $View.server.health){Write-Host ('Server HTTP: '+$View.server.health.http_status+'; ready: '+$View.server.health.ready+'; error: '+$View.server.health.error_class)}
    else{Write-Host 'Server health: UNKNOWN (no verified single listener)'}
    Write-Host ('Business process PIDs: '+($View.business.active_business_pids -join ', '))
    Write-Host ('Unknown Python identities: '+$View.business.unknown_python_identities)
    Write-Host ('Unknown launcher identities: '+$View.business.unknown_launcher_identities)
    foreach($log in $View.logs){Write-Host ('Log bytes: '+$log.bytes+'; '+$log.path)}
    if($View.logs.Count -eq 0){Write-Host 'Matching startup log: UNKNOWN'}
    Write-Host 'HTTP 200 / ready=True verifies the server only, not the phone or business progress.'
    Write-Host 'A quiet log does not by itself prove a hang. No automatic stop or restart.'
}
function Assert-StopIdentity($View){
    if($View.server.owners.Count -ne 1){throw 'stop_refused_no_single_listener'}
    $owner=$View.server.owners[0]
    if(-not $owner.matches_fixed_node -or -not $View.server.dependencies.ok){throw 'stop_refused_appium_identity_or_dependency_drift'}
    if($View.business.active_business_pids.Count -gt 0){throw 'stop_refused_business_python_is_active'}
    if($View.business.unknown_python_identities -ne 0){throw 'stop_refused_unknown_python_identity'}
    if($View.business.unknown_launcher_identities -ne 0){throw 'stop_refused_unknown_launcher_identity'}
    $process=Get-CimInstance Win32_Process -Filter ('ProcessId='+$owner.pid) -ErrorAction Stop
    if($null -eq $process -or [string]::IsNullOrWhiteSpace([string]$process.CommandLine)){throw 'stop_refused_command_identity_unavailable'}
    if(([string]$process.CommandLine).IndexOf(('"'+$Cli+'"'),[StringComparison]::OrdinalIgnoreCase) -lt 0){throw 'stop_refused_not_fixed_appium_cli'}
    $actual=Get-Process -Id $owner.pid -ErrorAction Stop
    try{
        if($actual.StartTime.ToUniversalTime().Ticks -ne ([DateTime]::Parse($owner.created_utc).ToUniversalTime().Ticks)){throw 'stop_refused_process_birth_changed'}
    }finally{$actual.Dispose()}
}

if($Action -eq 'Status'){Show-View (Read-View);exit 0}
if($Action -eq 'Watch'){
    Write-Host 'Read-only watch. Ctrl+C closes this watch only; Appium keeps running.'
    while($true){Show-View (Read-View);Write-Host '';Start-Sleep -Seconds 5}
}
if($Action -eq 'Logs'){
    $view=Read-View;Show-View $view
    $logs=@($view.logs | Where-Object {$_.readable})
    if($logs.Count -eq 0){throw 'matching_readable_startup_log_unavailable'}
    Write-Host 'Read-only log view. Ctrl+C closes this view only; Appium keeps running.'
    $readers=@()
    try{
        foreach($log in $logs){
            $stream=[IO.File]::Open($log.path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            $offset=[Math]::Max(0,$stream.Length-16384);$stream.Seek($offset,[IO.SeekOrigin]::Begin) | Out-Null
            $reader=[IO.StreamReader]::new($stream,[Text.Encoding]::UTF8,$true)
            if($offset -gt 0){$reader.ReadLine() | Out-Null}
            $readers += $reader
            Write-Host ('--- '+$log.path+' ---')
            $text=$reader.ReadToEnd();if($text){[Console]::Write($text)}
        }
        while($true){
            foreach($reader in $readers){$text=$reader.ReadToEnd();if($text){[Console]::Write($text)}}
            Start-Sleep -Milliseconds 500
        }
    }finally{foreach($reader in $readers){$reader.Dispose()}}
}

# Only a human's independent Windows launcher may request a stop.
# CheckOnly never prompts or opens a terminate-capable process handle.
if($env:OPENCLAW_SHELL -eq 'exec' -and -not $CheckOnly){throw 'stop_from_openclaw_exec_refused_use_manual_launcher'}
$mutex=[Threading.Mutex]::new($false,'Local\OpenClaw-Appium-4723-ManualStart')
$held=$false;$target=$null
try{
    $held=$mutex.WaitOne(0);if(-not $held){throw 'appium_manual_operation_already_in_progress'}
    $view=Read-View;Show-View $view
    Assert-StopIdentity $view
    if($CheckOnly){Write-Host 'Stop precheck passed. No action taken. Phone/external clients are NOT proven idle.';exit 0}
    $owner=$view.server.owners[0]
    $target=Get-Process -Id $owner.pid -ErrorAction Stop
    # Hold the original process handle across the confirmation and final checks.
    $pinnedHandle=$target.Handle
    if($target.StartTime.ToUniversalTime().Ticks -ne ([DateTime]::Parse($owner.created_utc).ToUniversalTime().Ticks)){throw 'stop_refused_process_birth_changed'}
    Write-Host 'STOP WILL DISCONNECT APPIUM. It is a process stop, not a graceful HTTP shutdown.'
    Write-Host 'First confirm ALL phone automation and external clients are finished; sessions were not queried.'
    Write-Host 'Do not use while a scheduled phone job may start. This never kills Python or a process tree.'
    $required='STOP-APPIUM-'+$owner.pid
    $answer=Read-Host ('If everything is idle, type exactly '+$required+' (anything else cancels)')
    if($answer -cne $required){Write-Host 'Cancelled. Appium was not stopped.';exit 0}
    $current=Read-View
    Assert-StopIdentity $current
    if($current.server.owners[0].pid -ne $owner.pid -or $current.server.owners[0].created_utc -cne $owner.created_utc -or $target.HasExited){throw 'stop_refused_original_process_changed'}
    $target.Kill()
    if(-not $target.WaitForExit(3000)){throw 'stop_requested_exit_not_yet_observed_no_retry'}
    Write-Host ('Stopped only verified Appium PID '+$owner.pid+'. No automatic restart.')
}finally{if($target){$target.Dispose()};if($held){$mutex.ReleaseMutex()};$mutex.Dispose()}

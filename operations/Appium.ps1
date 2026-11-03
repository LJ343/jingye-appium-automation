param([ValidateSet('Status','Start')][string]$Action = 'Status')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Node = 'C:\Users\SXL\AppData\Local\nvm\v22.22.2\node.exe'
$Package = 'C:\Users\SXL\AppData\Local\nvm\v22.22.2\node_modules\appium\package.json'
$Cli = 'C:\Users\SXL\AppData\Local\nvm\v22.22.2\node_modules\appium\index.js'
$LogRoot = 'D:\openclaw_data\logs\appium'
$Pins = @{
    $Node = 'ae1a50511be58e987483fdbc12125407443926d2d394669ade2352776e920dd3'
    $Package = '5f149e4be5dd635b912e36d054d74d060cc079bfed9cc3add3a03b547e203327'
    $Cli = 'bd2d55f6f01af3eaa759ba5d44f242cf4e017eeaefc851c774a9d08699ebabed'
}
if (-not ('AppiumOpsIdentity' -as [type])) {
    Add-Type -TypeDefinition @'
using System; using System.Text; using System.Runtime.InteropServices;
public static class AppiumOpsIdentity {
 [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint a,bool b,uint p);
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool QueryFullProcessImageNameW(IntPtr h,uint f,StringBuilder b,ref uint n);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
 public static string Image(uint pid) {
  IntPtr h=OpenProcess(0x1000,false,pid); if(h==IntPtr.Zero)return null;
  try { var b=new StringBuilder(32768);uint n=32768;return QueryFullProcessImageNameW(h,0,b,ref n)?b.ToString():null; }
  finally { CloseHandle(h); }
 }
}
'@
}
function Get-Listeners {
    # A query failure is an error, never permission to start another server.
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object LocalPort -eq 4723)
}
function Get-Dependencies {
    $facts = @(); $ok = $true
    foreach ($path in @($Node,$Package,$Cli)) {
        $exists = Test-Path -LiteralPath $path -PathType Leaf
        $hash = $null
        if ($exists) { $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }
        $matches = $exists -and $hash -eq $Pins[$path]
        if (-not $matches) { $ok = $false }
        $facts += [ordered]@{path=$path;exists=$exists;sha256=$hash;matches_pin=$matches}
    }
    $version = $null
    if (Test-Path -LiteralPath $Package -PathType Leaf) {
        $version = (Get-Content -LiteralPath $Package -Raw -Encoding UTF8 | ConvertFrom-Json).version
    }
    [ordered]@{ok=($ok -and $version -eq '3.5.2');appium_version=$version;files=$facts}
}
function Get-Health {
    $result = [ordered]@{http_status=$null;ready=$null;timeout_ms=3000;error_class=$null}
    $request = $null; $response = $null; $stream = $null; $memory = $null
    try {
        $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:4723/status')
        $request.Proxy=$null; $request.AllowAutoRedirect=$false
        $request.Method='GET'; $request.Timeout=3000; $request.ReadWriteTimeout=3000; $request.KeepAlive=$false
        $response=$request.GetResponse(); $result.http_status=[int]$response.StatusCode
        $stream=$response.GetResponseStream(); $memory=[IO.MemoryStream]::new(); $buffer=[byte[]]::new(4096)
        while (($count=$stream.Read($buffer,0,$buffer.Length)) -gt 0) {
            if ($memory.Length+$count -gt 65536) { throw 'status_response_bound' }
            $memory.Write($buffer,0,$count)
        }
        $body=[Text.Encoding]::UTF8.GetString($memory.ToArray()) | ConvertFrom-Json
        if ($body.value.ready -is [bool]) { $result.ready=$body.value.ready }
    } catch { $result.error_class=$_.Exception.GetType().Name }
    finally {
        if ($memory) {$memory.Dispose()}; if ($stream) {$stream.Dispose()}
        if ($response) {$response.Close()}; if ($request) {$request.Abort()}
    }
    $result
}
function Get-Status {
    $listeners = @(Get-Listeners); $owners = @()
    foreach ($ownerId in @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
        $image=[AppiumOpsIdentity]::Image([uint32]$ownerId)
        $process=Get-Process -Id $ownerId -ErrorAction SilentlyContinue
        $created=$null; if ($process) {$created=$process.StartTime.ToUniversalTime().ToString('o')}
        $owners += [ordered]@{pid=[int]$ownerId;created_utc=$created;image=$image;matches_fixed_node=($image -ieq $Node)}
    }
    $health=$null
    if ($owners.Count -eq 1 -and $owners[0].matches_fixed_node) {$health=Get-Health}
    [ordered]@{action='Status';observed_at=(Get-Date -Format o);port=4723;listener_count=$listeners.Count;
        owners=$owners;health=$health;dependencies=(Get-Dependencies);log_root=$LogRoot;
        note='Status never starts, stops, or changes an Appium/device session.'}
}
if ($Action -eq 'Status') { Get-Status | ConvertTo-Json -Depth 8; exit 0 }

# Start is for a human's Windows launcher, never an OpenClaw exec tool lease.
if ($env:OPENCLAW_SHELL -eq 'exec') { throw 'start_from_openclaw_exec_refused_use_manual_windows_launcher' }
$mutex=[Threading.Mutex]::new($false,'Local\OpenClaw-Appium-4723-ManualStart')
$held=$false
try {
    $held=$mutex.WaitOne(0)
    if (-not $held) { throw 'another_appium_start_is_in_progress' }
    if (@(Get-Listeners).Count -gt 0) {
        Get-Status | ConvertTo-Json -Depth 8
        throw 'port_4723_already_listening_no_duplicate_start'
    }
    $dependencies=Get-Dependencies
    if (-not $dependencies.ok) { throw 'fixed_appium_3_5_2_dependency_drift_start_refused' }
    # This is the first filesystem mutation. Status does not create D directories.
    [IO.Directory]::CreateDirectory($LogRoot) | Out-Null
    $tag=(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')+'-'+[Guid]::NewGuid().ToString('N')
    $stdout=Join-Path $LogRoot ($tag+'.stdout.log'); $stderr=Join-Path $LogRoot ($tag+'.stderr.log')
    if ((Test-Path -LiteralPath $stdout) -or (Test-Path -LiteralPath $stderr)) { throw 'new_log_name_collision' }
    if (@(Get-Listeners).Count -gt 0) { Get-Status | ConvertTo-Json -Depth 8; throw 'listener_appeared_start_refused' }
    $arguments=@(('"'+$Cli+'"'),'--address','0.0.0.0','--port','4723','--allow-cors','--log-timestamp','--log-no-colors')
    $child=Start-Process -FilePath $Node -ArgumentList $arguments -WorkingDirectory (Split-Path $Cli -Parent) `
        -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    try {
        [ordered]@{action='Start';requested_at=(Get-Date -Format o);pid=$child.Id;
            node=$Node;cli=$Cli;appium_version='3.5.2';address='0.0.0.0';port=4723;allow_cors=$true;
            hidden=$true;log_timestamp=$true;log_no_colors=$true;stdout=$stdout;stderr=$stderr;
            ready_verified=$false;note='Run Appium-Status.cmd to inspect readiness. No stop/restart action is provided.'} | ConvertTo-Json -Depth 5
    } finally { $child.Dispose() }
} finally {
    if ($held) {$mutex.ReleaseMutex()}; $mutex.Dispose()
}

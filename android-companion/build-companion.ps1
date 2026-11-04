param(
    [switch]$Build,
    [string]$ExpectedSourceSha256
)
$ErrorActionPreference = 'Stop'
$pythonExe = 'C:\Users\YOUR_USER\OpenClawNative\python-venv\Scripts\python.exe'
$scriptPath = Join-Path $PSScriptRoot 'build_companion.py'
$buildArguments = @('-X', 'utf8', $scriptPath)
if ($Build) {
    if ($ExpectedSourceSha256 -notmatch '^[0-9a-fA-F]{64}$') {
        throw 'Build requires the reviewed source freeze SHA256.'
    }
    $buildArguments += @('--execute', '--expected-source-sha256', $ExpectedSourceSha256.ToLowerInvariant())
}
& $pythonExe @buildArguments
exit $LASTEXITCODE

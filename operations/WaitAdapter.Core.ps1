# Candidate only. This file never selects or starts a business entry by itself.
function Invoke-PinnedLauncherAndWait {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$LiteralLauncherPath,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [hashtable]$LauncherParameters = @{},
        [Parameter(Mandatory = $true)][hashtable]$Result
    )

    $jingyeAdapterState = @{
        MayHaveStarted = $false
        TerminalConfirmed = $false
        Process = $null
        SafeHandle = $null
        OriginalProcess = $null
        LaunchCalls = 0
        ScriptError = $null
    }
    $Result.ExitCode = 125
    # Allocate the emergency hold before any child can start. No timer or polling.
    $jingyeAdapterEmergencyHold = New-Object System.Threading.ManualResetEvent($false)
    $jingyeAdapterFileLock = $null
    try {
        if (-not [IO.Path]::IsPathRooted($LiteralLauncherPath)) {
            throw 'Launcher path must be absolute.'
        }
        # FileShare.Read prevents writes/replacement between hash verification and dot-source.
        $jingyeAdapterFileLock = [IO.File]::Open(
            $LiteralLauncherPath, [IO.FileMode]::Open,
            [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $jingyeAdapterHasher = [Security.Cryptography.SHA256]::Create()
        try {
            $jingyeAdapterActualHash = [BitConverter]::ToString(
                $jingyeAdapterHasher.ComputeHash($jingyeAdapterFileLock)).Replace('-', '')
        } finally {
            $jingyeAdapterHasher.Dispose()
        }
        if ($jingyeAdapterActualHash -cne $ExpectedSha256.ToUpperInvariant()) {
            throw 'Pinned launcher SHA256 mismatch. No launch was attempted.'
        }

        & {
            param($jingyeAdapterScopeState, $jingyeAdapterScopePath, $jingyeAdapterScopeParameters)
            # This deliberately supports only the six parameters in the pinned launcher.
            # The function exists only in this child scope, never globally or in Python.
            function Start-Process {
                [CmdletBinding()]
                param(
                    [string]$FilePath,
                    [string[]]$ArgumentList,
                    [System.Diagnostics.ProcessWindowStyle]$WindowStyle,
                    [string]$RedirectStandardOutput,
                    [string]$RedirectStandardError,
                    [switch]$PassThru
                )
                $jingyeAdapterExpectedKeys = @(
                    'ArgumentList', 'FilePath', 'PassThru',
                    'RedirectStandardError', 'RedirectStandardOutput', 'WindowStyle')
                $jingyeAdapterActualKeys = @($PSBoundParameters.Keys | Sort-Object)
                if (($jingyeAdapterActualKeys -join '|') -cne ($jingyeAdapterExpectedKeys -join '|')) {
                    throw 'Unsupported Start-Process parameter shape; no forwarding was attempted.'
                }
                if (-not $PassThru.IsPresent) {
                    throw 'The pinned launcher must request its original Process object.'
                }
                $jingyeAdapterScopeState.LaunchCalls++
                if ($jingyeAdapterScopeState.LaunchCalls -ne 1) {
                    throw 'A second launch is prohibited.'
                }
                # Clearly missing executables are rejected before calling Start-Process.
                if (-not [IO.Path]::IsPathRooted($FilePath) -or -not [IO.File]::Exists($FilePath)) {
                    throw 'Executable preflight failed. No launch was attempted.'
                }
                $jingyeAdapterScopeState.MayHaveStarted = $true
                # Forward every bound value unchanged to the original cmdlet.
                $jingyeAdapterStartedProcess = Microsoft.PowerShell.Management\Start-Process @PSBoundParameters
                if ($jingyeAdapterStartedProcess -isnot [System.Diagnostics.Process]) {
                    throw 'Start-Process did not return one trusted Process object.'
                }
                $jingyeAdapterScopeState.Process = $jingyeAdapterStartedProcess
                # Pin the original process handle immediately, before the original 3s sleep.
                # Do not recover identity by PID or cast a missing exit code to zero.
                $jingyeAdapterScopeState.SafeHandle = $jingyeAdapterStartedProcess.SafeHandle
                if ($jingyeAdapterScopeState.SafeHandle.IsInvalid -or $jingyeAdapterScopeState.SafeHandle.IsClosed) {
                    throw 'The original process handle could not be retained.'
                }
                Write-Output -NoEnumerate $jingyeAdapterStartedProcess
            }

            $proc = $null
            try {
                . $jingyeAdapterScopePath @jingyeAdapterScopeParameters
            } catch {
                $jingyeAdapterScopeState.ScriptError = $_
            } finally {
                $jingyeAdapterScopeState.OriginalProcess = $proc
            }
        } $jingyeAdapterState $LiteralLauncherPath $LauncherParameters

        # The pinned bytes have now been read/executed. Do not lock the source for the business lifetime.
        $jingyeAdapterFileLock.Dispose()
        $jingyeAdapterFileLock = $null
        if (-not $jingyeAdapterState.MayHaveStarted) {
            if ($null -ne $jingyeAdapterState.ScriptError) {
                throw $jingyeAdapterState.ScriptError
            }
            throw 'The pinned launcher did not call Start-Process.'
        }
        if (-not [object]::ReferenceEquals($jingyeAdapterState.Process, $jingyeAdapterState.OriginalProcess)) {
            throw 'The launcher Process object identity could not be confirmed.'
        }
        if ($null -eq $jingyeAdapterState.SafeHandle -or $jingyeAdapterState.SafeHandle.IsInvalid -or $jingyeAdapterState.SafeHandle.IsClosed) {
            throw 'The retained original process handle is unavailable.'
        }
        # The launcher may have thrown after starting. Its original Process still owns the wait.
        if ($null -ne $jingyeAdapterState.ScriptError) {
            try { [Console]::Error.WriteLine('Launcher reported an error after start; retaining the same process and waiting for its actual exit.') } catch {}
        }
        $jingyeAdapterState.Process.WaitForExit()
        if (-not $jingyeAdapterState.Process.HasExited) {
            throw 'WaitForExit returned without confirmed process exit.'
        }
        $jingyeAdapterExitCode = $jingyeAdapterState.Process.ExitCode
        if ($null -eq $jingyeAdapterExitCode -or $jingyeAdapterExitCode -isnot [int]) {
            throw 'No trustworthy integer exit code is available.'
        }
        $jingyeAdapterState.TerminalConfirmed = $true
        $Result.ExitCode = $jingyeAdapterExitCode
        [Console]::WriteLine(('WAIT_ADAPTER_EXIT UTC={0} ExitCode={1}' -f [DateTime]::UtcNow.ToString('o'), $jingyeAdapterExitCode))
    } catch {
        if ($jingyeAdapterState.MayHaveStarted -and -not $jingyeAdapterState.TerminalConfirmed) {
            try { [Console]::Error.WriteLine('WAIT_ADAPTER_UNCERTAIN: child may still exist. Holding this owner open without timeout, kill, relaunch, or PID lookup. Manual review is required.') } catch {}
        } else {
            try { [Console]::Error.WriteLine('WAIT_ADAPTER_REJECTED: {0}' -f $_.Exception.Message) } catch {}
        }
    } finally {
        # Any ordinary exception after possible launch must not release OpenClaw's owner.
        if ($jingyeAdapterState.MayHaveStarted -and -not $jingyeAdapterState.TerminalConfirmed) {
            $null = $jingyeAdapterEmergencyHold.WaitOne()
        }
        if ($null -ne $jingyeAdapterFileLock) { $jingyeAdapterFileLock.Dispose() }
        $jingyeAdapterEmergencyHold.Dispose()
    }
}

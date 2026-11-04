# Temporary outer wait adapter, authorized 2026-11-02 for two Jingye sync cron jobs.
# Restore original cron messages after root cause is fixed and verified; see integration record.
if ($args.Count -ne 0) {
    [Console]::Error.WriteLine('This fixed candidate accepts no arguments.')
    exit 125
}
. (Join-Path $PSScriptRoot 'WaitAdapter.Core.ps1')
$jingyeCandidateResult = @{ ExitCode = 125 }
Invoke-PinnedLauncherAndWait `
    -LiteralLauncherPath 'C:\Users\YOUR_USER\.openclaw-migration\workspace\skills\jingye-platform-sync\run_daily_sync.ps1' `
    -ExpectedSha256 '0F7BD68D7F3561BFA4DE2605D05C1BDA2C65245BA462A6A7DD383AB0EC1021EC' `
    -Result $jingyeCandidateResult
exit $jingyeCandidateResult.ExitCode

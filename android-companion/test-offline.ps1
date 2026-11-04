param([string]$JdkHome = 'C:\Program Files\Java\jdk-25.0.3')
$ErrorActionPreference = 'Stop'
$out = Join-Path $PSScriptRoot 'build\offline-tests'
New-Item -ItemType Directory -Path $out -Force | Out-Null
$source = Join-Path $PSScriptRoot 'src\com\lj343\jingye\connectionguard\RecoveryController.java'
$tests = Join-Path $PSScriptRoot 'tests\com\lj343\jingye\connectionguard\RecoveryControllerTest.java'
$boundary = Join-Path $PSScriptRoot 'src\com\lj343\jingye\connectionguard\FailureBoundary.java'
& (Join-Path $JdkHome 'bin\javac.exe') --release 17 -encoding UTF-8 -d $out $source $boundary $tests
if ($LASTEXITCODE -ne 0) { throw 'Offline test compilation failed.' }
& (Join-Path $JdkHome 'bin\java.exe') -cp $out com.lj343.jingye.connectionguard.RecoveryControllerTest
if ($LASTEXITCODE -ne 0) { throw 'Offline tests failed.' }

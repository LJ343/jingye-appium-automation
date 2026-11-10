param(
    [Parameter(Mandatory = $true)][string]$AndroidJar,
    [string]$BuildTools = 'C:\Users\YOUR_USER\adb\build-tools\35.0.0',
    [string]$JdkHome = 'C:\Program Files\Java\jdk-25.0.3',
    [string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
# Build only: no downloads, signing keys, installation, adb, permission grants or device calls.
$jar = (Resolve-Path -LiteralPath $AndroidJar).Path
$build = Join-Path $PSScriptRoot 'build\android'
if ($OutputDirectory) {
    $buildRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'build')) + [System.IO.Path]::DirectorySeparatorChar
    $build = [System.IO.Path]::GetFullPath($OutputDirectory)
    if (-not $build.StartsWith($buildRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'OutputDirectory must be an isolated child of this project build directory.'
    }
    if (Test-Path -LiteralPath $build) { throw 'OutputDirectory already exists; refusing to overwrite a prior build.' }
}
$classes = Join-Path $build 'classes'
$dex = Join-Path $build 'dex'
New-Item -ItemType Directory -Path $classes, $dex -Force | Out-Null
$sources = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'src') -Recurse -Filter '*.java' -File | ForEach-Object { $_.FullName })
& (Join-Path $JdkHome 'bin\javac.exe') --release 17 -encoding UTF-8 -classpath $jar -d $classes @sources
if ($LASTEXITCODE -ne 0) { throw 'Android Java compilation failed.' }
$classFiles = @(Get-ChildItem -LiteralPath $classes -Recurse -Filter '*.class' -File | ForEach-Object { $_.FullName })
& (Join-Path $JdkHome 'bin\java.exe') -cp (Join-Path $BuildTools 'lib\d8.jar') com.android.tools.r8.D8 --lib $jar --min-api 31 --output $dex @classFiles
if ($LASTEXITCODE -ne 0) { throw 'D8 failed.' }
$resourcesApk = Join-Path $build 'guard-resources-unsigned.apk'
$linkArgs = @('link','-o',$resourcesApk,'--manifest',(Join-Path $PSScriptRoot 'AndroidManifest.xml'),'-I',$jar,'--min-sdk-version','31','--target-sdk-version','35')
$res = Join-Path $PSScriptRoot 'res'
if (Test-Path -LiteralPath $res) {
    $compiledRes = Join-Path $build 'resources.zip'
    & (Join-Path $BuildTools 'aapt2.exe') compile --dir $res -o $compiledRes
    if ($LASTEXITCODE -ne 0) { throw 'Resource compilation failed.' }
    $linkArgs += $compiledRes
}
& (Join-Path $BuildTools 'aapt2.exe') @linkArgs
if ($LASTEXITCODE -ne 0) { throw 'Manifest/resource packaging failed.' }
Push-Location -LiteralPath $dex
try {
    & (Join-Path $BuildTools 'aapt.exe') add $resourcesApk classes.dex
    if ($LASTEXITCODE -ne 0) { throw 'DEX packaging failed.' }
} finally { Pop-Location }
$output = Join-Path $build 'jingye-connectionguard-unsigned.apk'
& (Join-Path $BuildTools 'zipalign.exe') -f -p 4 $resourcesApk $output
if ($LASTEXITCODE -ne 0) { throw 'ZIP alignment failed.' }
Get-Item -LiteralPath $output | Select-Object FullName, Length
Get-FileHash -LiteralPath $output -Algorithm SHA256

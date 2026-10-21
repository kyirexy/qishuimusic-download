#Requires -Version 7.0
[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$')][string]$Version = '1.0.0',
    [string]$PythonExecutable = '',
    [switch]$UseCurrentEnvironment,
    [switch]$UseInstalledNode,
    [switch]$SkipTests,
    [switch]$SkipRuntime,
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Set-Location -LiteralPath $PSScriptRoot
if (-not $IsWindows -or -not [Environment]::Is64BitProcess) { throw 'Windows x64 PowerShell 7 is required.' }
$versions = Get-Content -LiteralPath 'scripts\runtime-versions.json' -Raw | ConvertFrom-Json
if (-not $PythonExecutable) {
    $existingPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $PythonExecutable = if (Test-Path -LiteralPath $existingPython) { $existingPython } else { (Get-Command python.exe -ErrorAction Stop).Source }
}
$PythonExecutable = (Get-Command $PythonExecutable -ErrorAction Stop).Source
$pythonVersion = (& $PythonExecutable -c 'import platform,struct,sys; print(platform.python_version()+"/"+str(struct.calcsize("P")*8)); sys.exit(sys.implementation.name != "cpython")').Trim()
$pythonAbiPattern = '^' + [regex]::Escape($versions.pythonAbi) + '\.\d+/64$'
if ($LASTEXITCODE -ne 0 -or $pythonVersion -notmatch $pythonAbiPattern) {
    throw "CPython $($versions.pythonAbi).x x64 is required; found $pythonVersion."
}
if ($UseCurrentEnvironment) {
    $taskPython = $PythonExecutable
} else {
    $taskPython = Join-Path $PSScriptRoot '.venv-build\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $taskPython)) {
        & $PythonExecutable -m venv (Join-Path $PSScriptRoot '.venv-build')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated build environment.' }
    }
}
$buildPythonVersion = (& $taskPython -c 'import platform,struct,sys; print(platform.python_version()+"/"+str(struct.calcsize("P")*8)); sys.exit(sys.implementation.name != "cpython")').Trim()
if ($LASTEXITCODE -ne 0 -or $buildPythonVersion -notmatch $pythonAbiPattern) {
    throw 'Existing build environment uses a different Python ABI. Recreate .venv-build with CPython 3.11 x64.'
}
& $taskPython -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r requirements-build-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Pinned build dependency installation failed.' }
& $taskPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'The Python dependency environment is inconsistent.' }

$cacheRuntime = Join-Path $PSScriptRoot 'tools\cache-runtime'
if (-not $SkipRuntime) {
    & (Join-Path $PSScriptRoot 'scripts\prepare-cache-runtime.ps1') -UseInstalledNode:$UseInstalledNode
}
$runtimeNode = Join-Path $cacheRuntime 'node.exe'
if (-not (Test-Path -LiteralPath $runtimeNode)) { throw 'Cache runtime missing. Rebuild without -SkipRuntime.' }
$nodeVersion = (& $runtimeNode --version).Trim()
if ($LASTEXITCODE -ne 0 -or $nodeVersion -ne "v$($versions.node)") { throw 'The cached Node version does not match runtime-versions.json.' }
& $runtimeNode (Join-Path $PSScriptRoot 'scripts\verify-cache-runtime.cjs') $cacheRuntime
if ($LASTEXITCODE -ne 0) { throw 'The native cache runtime smoke test failed.' }
if (-not $SkipTests) {
    & $taskPython -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; packaging stopped.' }
}

New-Item -ItemType Directory -Force -Path 'build', 'dist', 'dist\release' | Out-Null
$buildDirectory = Join-Path $PSScriptRoot 'build'
$distDirectory = Join-Path $PSScriptRoot 'dist'
$versionFile = Join-Path $buildDirectory 'windows-version.txt'
$applicationSource = Join-Path $PSScriptRoot 'app.py'
& $taskPython (Join-Path $PSScriptRoot 'scripts\write-version-info.py') $Version $versionFile --app-source $applicationSource
if ($LASTEXITCODE -ne 0) { throw 'Could not generate the Windows version resource.' }
$pyInstallerArgs = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', 'SodaMusicDownloader', '--specpath', $buildDirectory, '--workpath', (Join-Path $buildDirectory 'pyinstaller'),
    '--distpath', $distDirectory, '--version-file', $versionFile,
    '--collect-all', 'customtkinter', '--icon', (Join-Path $PSScriptRoot 'assets\app.ico'),
    '--add-data', "$(Join-Path $PSScriptRoot 'assets');assets",
    '--add-data', "$(Join-Path $PSScriptRoot 'licenses');licenses",
    '--add-data', "$(Join-Path $PSScriptRoot 'THIRD_PARTY_NOTICES.txt');.",
    '--add-data', "$(Join-Path $PSScriptRoot 'soda_cache_reader.cjs');.",
    '--add-data', "$cacheRuntime;tools\cache-runtime",
    $applicationSource
)
& $taskPython @pyInstallerArgs
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
$executable = Join-Path $PSScriptRoot 'dist\SodaMusicDownloader.exe'
if (-not $SkipSmokeTest) {
    $smoke = Start-Process -FilePath $executable -ArgumentList '--smoke-test' -WindowStyle Hidden -PassThru
    if (-not $smoke.WaitForExit(30000)) {
        $smoke.Kill()
        throw 'Packaged GUI smoke test timed out.'
    }
    if ($smoke.ExitCode -ne 0) { throw "Packaged GUI smoke test failed with exit code $($smoke.ExitCode)." }
}

# Preserve local shortcuts when unlocked; a running old alias must not prevent
# the English release assets from being generated.
foreach ($aliasName in @('汽水音乐下载.exe', '汽水无损下载.exe')) {
    try { Copy-Item -LiteralPath $executable -Destination (Join-Path $PSScriptRoot "dist\$aliasName") -Force }
    catch { Write-Warning "Could not update local alias $aliasName (it may be running). English release packaging will continue." }
}
$stage = Join-Path $PSScriptRoot "build\release-stage\SodaMusicDownloader-v$Version-windows-x64"
$stage = [IO.Path]::GetFullPath($stage)
$stageParent = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'build\release-stage'))
if ([IO.Path]::GetDirectoryName($stage) -ne $stageParent -or [IO.Path]::GetFileName($stage) -ne "SodaMusicDownloader-v$Version-windows-x64") {
    throw 'Release staging path is outside the expected generated directory.'
}
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item -LiteralPath $executable -Destination (Join-Path $stage 'SodaMusicDownloader.exe') -Force
foreach ($name in @('README.md', 'THIRD_PARTY_NOTICES.txt', 'CHANGELOG.md', 'VERIFICATION.md', 'PRODUCT.md', 'DESIGN.md', 'LICENSE')) {
    if (Test-Path -LiteralPath $name) { Copy-Item -LiteralPath $name -Destination (Join-Path $stage $name) -Force }
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'licenses') -Destination (Join-Path $stage 'licenses') -Recurse -Force
$screenshotDirectory = Join-Path $PSScriptRoot 'docs\screenshots'
if (Test-Path -LiteralPath $screenshotDirectory -PathType Container) {
    $screenshots = Get-ChildItem -LiteralPath $screenshotDirectory -Recurse -File |
        Where-Object { $_.Extension -in @('.jpg', '.png') }
    foreach ($screenshot in $screenshots) {
        $relativeScreenshot = [IO.Path]::GetRelativePath($PSScriptRoot, $screenshot.FullName)
        $screenshotDestination = Join-Path $stage $relativeScreenshot
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $screenshotDestination) | Out-Null
        Copy-Item -LiteralPath $screenshot.FullName -Destination $screenshotDestination -Force
    }
}
@'
FFmpeg and FFprobe are required and are NOT bundled.

Install them separately and add their directory to PATH, or put ffmpeg.exe and
ffprobe.exe in a tools folder beside SodaMusicDownloader.exe.
Windows Package Manager example: winget install --id Gyan.FFmpeg --exact
Official project: https://ffmpeg.org/

The executable includes Python, CustomTkinter, Node.js, and the local cache reader.
It still requires the official Soda Music desktop application and a complete
local cache of a track the account can play. No music files are included.
'@ | Set-Content -LiteralPath (Join-Path $stage 'FFMPEG-REQUIRED.txt') -Encoding utf8NoBOM
$buildInfo = [ordered]@{
    version = $Version
    target = 'windows-x64'
    python = $buildPythonVersion.Split('/')[0]
    pythonAbi = $versions.pythonAbi
    ciPython = $versions.ciPython
    node = $versions.node
    customtkinter = '5.2.2'
    lmdb = $versions.lmdb
    commit = $env:GITHUB_SHA
    ffmpegBundled = $false
    testsSkipped = [bool]$SkipTests
    runtimePreparationSkipped = [bool]$SkipRuntime
    pythonLockSha256 = (Get-FileHash -LiteralPath 'requirements-build-lock.txt' -Algorithm SHA256).Hash.ToLowerInvariant()
    npmLockSha256 = (Get-FileHash -LiteralPath 'scripts\cache-runtime\package-lock.json' -Algorithm SHA256).Hash.ToLowerInvariant()
    actualRuntimeLockSha256 = (Get-FileHash -LiteralPath (Join-Path $cacheRuntime 'package-lock.json') -Algorithm SHA256).Hash.ToLowerInvariant()
}
$buildInfo | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stage 'BUILD-INFO.json') -Encoding utf8NoBOM
$filesForZip = Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object Name -ne 'checksums.txt' | Sort-Object FullName
$innerHashes = foreach ($file in $filesForZip) { "$((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant())  $([IO.Path]::GetRelativePath($stage, $file.FullName).Replace('\', '/'))" }
$innerHashes | Set-Content -LiteralPath (Join-Path $stage 'checksums.txt') -Encoding utf8NoBOM
$releaseExe = Join-Path $PSScriptRoot 'dist\release\SodaMusicDownloader.exe'
$releaseZip = Join-Path $PSScriptRoot "dist\release\SodaMusicDownloader-v$Version-windows-x64.zip"
Copy-Item -LiteralPath $executable -Destination $releaseExe -Force
Compress-Archive -LiteralPath $stage -DestinationPath $releaseZip -CompressionLevel Optimal -Force
$outerHashes = foreach ($file in @($releaseExe, $releaseZip)) { "$((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant())  $([IO.Path]::GetFileName($file))" }
$outerHashes | Set-Content -LiteralPath 'dist\release\checksums.txt' -Encoding utf8NoBOM
Copy-Item -LiteralPath (Join-Path $stage 'BUILD-INFO.json') -Destination 'dist\release\BUILD-INFO.json' -Force
Write-Output "Built v$Version in dist/release (FFmpeg/FFprobe are external dependencies)."

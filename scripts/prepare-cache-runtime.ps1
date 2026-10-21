#Requires -Version 7.0
[CmdletBinding()]
param([switch]$UseInstalledNode)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$versions = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime-versions.json') -Raw | ConvertFrom-Json
$runtime = Join-Path $repoRoot 'tools\cache-runtime'
$downloads = Join-Path $repoRoot 'build\downloads'
if (-not $IsWindows -or -not [Environment]::Is64BitProcess) { throw 'Windows x64 PowerShell 7 is required.' }

if ($UseInstalledNode) {
    $nodeCommand = Get-Command node.exe -ErrorAction Stop
    $sourceNode = $nodeCommand.Source
    $nodeFolder = Split-Path -Parent $sourceNode
    $npmCli = Join-Path $nodeFolder 'node_modules\npm\bin\npm-cli.js'
    $sourceLicense = Join-Path $nodeFolder 'LICENSE'
    if (-not (Test-Path -LiteralPath $sourceLicense)) {
        $sourceLicense = Join-Path $runtime 'NODE-LICENSE.txt'
    }
    if (-not (Test-Path -LiteralPath $npmCli) -or -not (Test-Path -LiteralPath $sourceLicense)) {
        throw 'Installed Node must include npm and its LICENSE. Omit -UseInstalledNode to download the verified distribution.'
    }
} else {
    New-Item -ItemType Directory -Force -Path $downloads | Out-Null
    $archiveName = "node-v$($versions.node)-win-x64.zip"
    $archive = Join-Path $downloads $archiveName
    $validArchive = (Test-Path -LiteralPath $archive) -and ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -eq $versions.nodeWinX64ZipSha256)
    if (-not $validArchive) {
        Invoke-WebRequest -Uri "https://nodejs.org/dist/v$($versions.node)/$archiveName" -OutFile $archive
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $versions.nodeWinX64ZipSha256) {
        throw 'Node.js archive SHA-256 does not match the pinned official release.'
    }
    # The verified Node archive contains thousands of npm files. The .NET
    # extractor avoids the per-file PowerShell overhead of Expand-Archive.
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $downloads, $true)
    $nodeFolder = Join-Path $downloads "node-v$($versions.node)-win-x64"
    $sourceNode = Join-Path $nodeFolder 'node.exe'
    $npmCli = Join-Path $nodeFolder 'node_modules\npm\bin\npm-cli.js'
    $sourceLicense = Join-Path $nodeFolder 'LICENSE'
}

$actualNode = (& $sourceNode --version).Trim()
if ($LASTEXITCODE -ne 0 -or $actualNode -ne "v$($versions.node)") {
    throw "Node v$($versions.node) is required; found $actualNode. Omit -UseInstalledNode to obtain the pinned version."
}
$architecture = (& $sourceNode -p 'process.platform + "/" + process.arch').Trim()
if ($LASTEXITCODE -ne 0 -or $architecture -ne 'win32/x64') { throw 'Node must be the Windows x64 build.' }
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
if ([IO.Path]::GetFullPath($sourceNode) -ne [IO.Path]::GetFullPath((Join-Path $runtime 'node.exe'))) {
    Copy-Item -LiteralPath $sourceNode -Destination (Join-Path $runtime 'node.exe') -Force
}
if ([IO.Path]::GetFullPath($sourceLicense) -ne [IO.Path]::GetFullPath((Join-Path $runtime 'NODE-LICENSE.txt'))) {
    Copy-Item -LiteralPath $sourceLicense -Destination (Join-Path $runtime 'NODE-LICENSE.txt') -Force
}
foreach ($name in @('package.json', 'package-lock.json')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "cache-runtime\$name") -Destination (Join-Path $runtime $name) -Force
}
# Prebuilt optional native packages are locked with integrity hashes. No npm
# lifecycle scripts or local native compilation are needed for Windows x64.
& $sourceNode $npmCli ci --prefix $runtime --omit=dev --include=optional --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org
if ($LASTEXITCODE -ne 0) { throw 'npm ci failed; the cache runtime was not prepared.' }
& (Join-Path $runtime 'node.exe') (Join-Path $PSScriptRoot 'verify-cache-runtime.cjs') $runtime
if ($LASTEXITCODE -ne 0) { throw 'Cache runtime verification failed.' }
$npmVersion = (& $sourceNode $npmCli --version).Trim()
$metadata = [ordered]@{
    node = $versions.node
    nodeArchiveSha256 = $versions.nodeWinX64ZipSha256
    nodeSource = $(if ($UseInstalledNode) { 'installed-version-checked' } else { 'official-archive-sha256-verified' })
    npm = $npmVersion
    lmdb = $versions.lmdb
    lockSha256 = (Get-FileHash -LiteralPath (Join-Path $runtime 'package-lock.json') -Algorithm SHA256).Hash.ToLowerInvariant()
}
$metadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtime 'runtime-build-info.json') -Encoding utf8NoBOM
Write-Output "Prepared Node $actualNode / LMDB $($versions.lmdb) in tools/cache-runtime."

# Windows build inputs

Use Windows x64, PowerShell 7, and CPython **3.11.x x64**. The normal build creates an isolated `.venv-build`, verifies every pinned Python wheel against `requirements-build-lock.txt`, obtains Node **22.19.0** from its official distribution, checks the pinned archive SHA-256, and installs the exact npm lock with `npm ci`. Windows CI uses Python **3.11.9**, which is available from `actions/setup-python`; a newer local 3.11 patch version is supported. The lock was validated locally with 3.11.15, and `BUILD-INFO.json` records the actual interpreter version used.

```powershell
pwsh -NoProfile -File .\build.ps1 -Version 1.0.0
```

The cache runtime can also be prepared for source execution:

```powershell
pwsh -NoProfile -File .\scripts\prepare-cache-runtime.ps1
```

`-UseInstalledNode` accepts an installed Windows x64 Node 22.19.0 distribution that includes npm and its license. The default downloads the hash-verified official archive. Committed npm inputs live in `scripts/cache-runtime/`; `tools/cache-runtime/` is generated and must not be committed.

For an already verified local environment, incremental packaging is available:

```powershell
pwsh -NoProfile -File .\build.ps1 -Version 1.0.0 `
  -PythonExecutable .\.venv\Scripts\python.exe -UseCurrentEnvironment `
  -SkipRuntime -SkipTests
```

The runtime native/codec smoke check and packaged GUI smoke check still run. `-SkipSmokeTest` exists for a build host without an interactive Windows desktop. A running Chinese compatibility executable may prevent its local alias from being refreshed; the script reports a warning and continues producing the English release assets.

Outputs:

- `dist/SodaMusicDownloader.exe`: the English named executable.
- `dist/汽水音乐下载.exe` and `dist/汽水无损下载.exe`: local compatibility copies.
- `dist/release/SodaMusicDownloader.exe`, `SodaMusicDownloader-v1.0.0-windows-x64.zip`, `checksums.txt`, and `BUILD-INFO.json`: release candidates.

Node, the cache reader, CustomTkinter themes/fonts, and the application icon are embedded in the executable. **FFmpeg and FFprobe are external dependencies and are never included.** Install them separately, add them to `PATH`, or place their executables in a `tools` directory beside the application. Tests that require FFmpeg report skipped when it is unavailable.

The workflow `.github/workflows/windows-build.yml` runs manually or for a `v*` version tag and uploads build artifacts. It does not publish a Release and has read-only repository permissions.

These pins make dependency inputs repeatable. Windows/PyInstaller timestamps and signing can still change executable bytes between builds; each build produces its own SHA-256 checksums. `BUILD-INFO.json` records the lock hashes and whether local incremental shortcuts were used. Do not commit `.venv*`, generated runtime binaries, build outputs, research data, or personal audio files.

# build.ps1 - Build dist\FolderLink\FolderLink.exe with PyInstaller (PowerShell version of Tools/build_exe.sh).
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\build.ps1 [-NoSmoke]
# Needs:  .venv (Tools/setup_env.sh) and vendor\syncthing\syncthing.exe (Tools/fetch_syncthing.sh).
# The smoke test starts the bundled engine with settings under _sandbox\exe_smoke and stops it again.
# Keep this file ASCII only (Windows PowerShell 5.1 misreads non-ASCII text without BOM).
param([switch]$NoSmoke)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw ".venv not found. Run Tools/setup_env.sh first." }
if (-not (Test-Path "vendor\syncthing\syncthing.exe")) { throw "vendor\syncthing\syncthing.exe not found. Run Tools/fetch_syncthing.sh first." }

$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = Join-Path $Root "src"
$env:QT_QPA_PLATFORM = "offscreen"
& $Py -m folderlink.devtools.make_icon "build\FolderLink.ico"
if ($LASTEXITCODE -ne 0) { throw "icon failed" }
Remove-Item Env:QT_QPA_PLATFORM

$excludes = @("tkinter", "pytest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
              "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtPdf")
$pyiArgs = @("-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN", "--name", "FolderLink", "--windowed",
          "--icon", (Join-Path $Root "build\FolderLink.ico"), "--paths", (Join-Path $Root "src"),
          "--distpath", "dist", "--workpath", "build\pyinstaller", "--specpath", "build",
          "--version-file", (Join-Path $Root "scripts\version_info.txt"))
foreach ($m in $excludes) { $pyiArgs += @("--exclude-module", $m) }
$pyiArgs += (Join-Path $Root "src\folderlink\__main__.py")
& $Py @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# Bundle Syncthing unmodified, with its license files (MPL-2.0)
New-Item -ItemType Directory -Force "dist\FolderLink\syncthing" | Out-Null
foreach ($f in @("syncthing.exe", "LICENSE.txt", "AUTHORS.txt", "README.txt", "VERSION.txt")) {
    if (Test-Path "vendor\syncthing\$f") { Copy-Item "vendor\syncthing\$f" "dist\FolderLink\syncthing\" -Force }
}
Copy-Item README.md "dist\FolderLink\README.md" -Force
if (Test-Path LICENSE) { Copy-Item LICENSE "dist\FolderLink\LICENSE.txt" -Force }
if (Test-Path THIRD_PARTY_NOTICES.md) { Copy-Item THIRD_PARTY_NOTICES.md "dist\FolderLink\THIRD_PARTY_NOTICES.md" -Force }
& $Py -m folderlink.devtools.collect_licenses "dist\FolderLink\licenses"
if ($LASTEXITCODE -ne 0) { throw "collecting licenses failed" }

if (-not $NoSmoke) {
    $smoke = Join-Path $Root "_sandbox\exe_smoke"
    if (Test-Path $smoke) { Remove-Item -Recurse -Force $smoke }
    New-Item -ItemType Directory -Force $smoke | Out-Null
    $env:QT_QPA_PLATFORM = "offscreen"
    $p = Start-Process -FilePath "dist\FolderLink\FolderLink.exe" -ArgumentList @("--smoke-test", "--isolated", "--data-dir", "`"$smoke\data`"") -Wait -PassThru -WindowStyle Hidden
    Remove-Item Env:QT_QPA_PLATFORM
    Write-Host "smoke test exit code: $($p.ExitCode)"
    if ($p.ExitCode -ne 0) { throw "smoke test failed" }
}
Write-Host "done: dist\FolderLink\FolderLink.exe"

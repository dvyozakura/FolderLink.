# package.ps1 - Make installer\Output\FolderLink_Setup.exe with Inno Setup 6 (PowerShell version of Tools/build_installer.sh).
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\package.ps1 [-Build]
#   -Build  run scripts\build.ps1 first
# This script only CREATES the setup program. It never runs it.
# Keep this file ASCII only (Windows PowerShell 5.1 misreads non-ASCII text without BOM).
param([switch]$Build)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
if ($Build) { & (Join-Path $PSScriptRoot "build.ps1") }
if (-not (Test-Path "dist\FolderLink\FolderLink.exe")) { throw "dist\FolderLink\FolderLink.exe not found. Run scripts\build.ps1 first." }

$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    foreach ($c in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                     "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $c) { $iscc = $c; break }
    }
}
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found. Install it from https://jrsoftware.org/isdl.php" }
& $iscc "installer\FolderLink.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
Get-Item "installer\Output\FolderLink_Setup.exe" | Format-List Name, Length, LastWriteTime
Write-Host "Created (not executed): installer\Output\FolderLink_Setup.exe"

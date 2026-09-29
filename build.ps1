# Builds the portable app folder (dist\BlueShelf) and the installer (dist\BlueShelfSetup-<version>.exe).
#
# Code signing is optional and off unless configured. Windows shows "Windows protected your PC" for
# an unsigned installer, which a signature removes once the certificate earns reputation. Set one of:
#
#   $env:BLUESHELF_SIGN_THUMBPRINT = "<thumbprint>"        a certificate in your Windows certificate store
#   $env:BLUESHELF_SIGN_DLIB       = "<path to Azure.CodeSigning.Dlib.dll>"   Azure Trusted Signing,
#   $env:BLUESHELF_SIGN_METADATA   = "<path to metadata.json>"               both needed together
#
# Signing happens twice: the app executable before it is packed, and the installer afterwards, so
# neither one warns.
#
#   .\build.ps1          the full build: every test first, then the app and the installer
#   .\build.ps1 -Fast    no tests - about three minutes instead of eight, for trying a change out
#                        straight away. Anything handed to anybody else gets the full build.
param([switch]$Fast)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Find-SignTool {
    $candidates = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue
    if (-not $candidates) { return $null }
    ($candidates | Sort-Object FullName)[-1].FullName
}

function Invoke-Signing {
    param([string]$Path)
    if (-not $env:BLUESHELF_SIGN_THUMBPRINT -and -not $env:BLUESHELF_SIGN_DLIB) { return }
    $signtool = Find-SignTool
    if (-not $signtool) { throw "signtool.exe not found - install the Windows SDK signing tools" }
    $stamp = if ($env:BLUESHELF_SIGN_TIMESTAMP) { $env:BLUESHELF_SIGN_TIMESTAMP } else { "http://timestamp.digicert.com" }
    # A timestamp is what keeps the signature valid after the certificate expires. Never skip it.
    $arguments = @("sign", "/fd", "SHA256", "/tr", $stamp, "/td", "SHA256")
    if ($env:BLUESHELF_SIGN_DLIB) {
        if (-not $env:BLUESHELF_SIGN_METADATA) { throw "BLUESHELF_SIGN_DLIB needs BLUESHELF_SIGN_METADATA as well" }
        $arguments += @("/dlib", $env:BLUESHELF_SIGN_DLIB, "/dmdf", $env:BLUESHELF_SIGN_METADATA)
    } else {
        $arguments += @("/sha1", $env:BLUESHELF_SIGN_THUMBPRINT)
    }
    $arguments += $Path
    Write-Host "Signing $Path"
    & $signtool @arguments
    if ($LASTEXITCODE -ne 0) { throw "signing failed for $Path" }
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if ($Fast) {
    Write-Host "Fast build: the tests were NOT run."
} else {
    & $python -m pytest -q tests
    if ($LASTEXITCODE -ne 0) { throw "tests failed" }
}

& $python build\make_icon.py
if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }

& $python build\make_version.py
if ($LASTEXITCODE -ne 0) { throw "version resource failed" }

# PyInstaller cannot overwrite dist\BlueShelf while a previously built copy is still running. Only copies
# started from this folder are closed; an installed BlueShelf the user is using is left alone.
Get-Process -Name BlueShelf -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "$PSScriptRoot\dist\*" } |
    ForEach-Object { Write-Host "Closing running build: $($_.Id)"; Stop-Process -Id $_.Id -Force }

# Antivirus scanning a freshly built exe can hold it open for a few seconds after the process exits,
# which makes PyInstaller fail with "Access is denied". Wait for the folder to become removable.
$deadline = (Get-Date).AddSeconds(45)
while (Test-Path "$PSScriptRoot\dist\BlueShelf") {
    try {
        Remove-Item -Recurse -Force "$PSScriptRoot\dist\BlueShelf" -ErrorAction Stop
        break
    } catch {
        if ((Get-Date) -gt $deadline) { throw "dist\BlueShelf is locked by another program: $($_.Exception.Message)" }
        Write-Host "dist\BlueShelf is in use, waiting..."
        Start-Sleep -Seconds 3
    }
}

& $python -m PyInstaller --noconfirm --clean blueshelf.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Invoke-Signing "$PSScriptRoot\dist\BlueShelf\BlueShelf.exe"

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 is not installed (winget install JRSoftware.InnoSetup)" }

# A leftover installer from an earlier build can still be locked (antivirus scans new executables),
# which makes Inno Setup fail with "EndUpdateResource failed". Clear it out first, and retry once.
Remove-Item "dist\BlueShelfSetup-*.exe" -Force -ErrorAction SilentlyContinue
& $iscc installer.iss
if ($LASTEXITCODE -ne 0) {
    Write-Host "Inno Setup failed, retrying once..."
    Start-Sleep -Seconds 5
    Remove-Item "dist\BlueShelfSetup-*.exe" -Force -ErrorAction SilentlyContinue
    & $iscc installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
}

$installer = (Get-ChildItem "dist\BlueShelfSetup-*.exe" | Select-Object -First 1).FullName
Invoke-Signing $installer
if (-not $env:BLUESHELF_SIGN_THUMBPRINT -and -not $env:BLUESHELF_SIGN_DLIB) {
    Write-Host "Not signed. Windows will warn about this installer - see the top of this script."
}
Write-Host ("SHA-256: " + (Get-FileHash $installer -Algorithm SHA256).Hash)

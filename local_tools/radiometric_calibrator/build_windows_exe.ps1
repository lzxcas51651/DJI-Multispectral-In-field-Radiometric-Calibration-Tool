param(
    [string]$Python = "py",
    [string]$PythonVersion = "-3.12"
)

$ErrorActionPreference = "Stop"
$ToolDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ToolDir "..\..")
$Launcher = Join-Path $RepoRoot "local_tools\dji_radiometric_calibrator.py"
$Requirements = Join-Path $ToolDir "requirements-windows.txt"
$VenvDir = Join-Path $ToolDir ".build-venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$DistDir = Join-Path $ToolDir "dist"
$BuildDir = Join-Path $ToolDir "build"
$SpecDir = $ToolDir

Push-Location $RepoRoot
try {
    # Always package from an isolated environment. In particular, the normal
    # opencv-python wheel can add another set of Qt DLLs and break PySide6 at
    # runtime with "DLL load failed while importing QtCore".
    if (-not (Test-Path $VenvPython)) {
        & $Python $PythonVersion -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "Failed to create isolated build environment." }
    }
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip in the build environment." }
    & $VenvPython -m pip install -r $Requirements
    if ($LASTEXITCODE -ne 0) { throw "Failed to install Windows build dependencies." }
    & $VenvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --onedir `
        --name "DJI_Radiometric_Calibration_Tool" `
        --distpath $DistDir `
        --workpath $BuildDir `
        --specpath $SpecDir `
        --paths (Join-Path $RepoRoot "local_tools") `
        --collect-all rasterio `
        --copy-metadata PySide6 `
        --copy-metadata PySide6-Essentials `
        --copy-metadata PySide6-Addons `
        --copy-metadata shiboken6 `
        --copy-metadata numpy `
        --copy-metadata opencv-python-headless `
        --copy-metadata Pillow `
        --copy-metadata rasterio `
        $Launcher
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed. No release was produced." }

    # Qt 6 uses the ICU supplied by Windows. PyInstaller can accidentally find
    # an unrelated unversioned ICU DLL on the parent process PATH (for example
    # Poppler's ICU 78) and place it beside the executable. That DLL then wins
    # the Windows loader search and QtCore fails with ERROR_PROC_NOT_FOUND.
    # Remove only those known accidental root-level files; rasterio's hashed
    # private libraries remain under rasterio.libs.
    $InternalDir = Join-Path $DistDir "DJI_Radiometric_Calibration_Tool\_internal"
    foreach ($UnintendedIcu in @("icuuc.dll", "icudt78.dll")) {
        $UnintendedIcuPath = Join-Path $InternalDir $UnintendedIcu
        if (Test-Path -LiteralPath $UnintendedIcuPath) {
            Remove-Item -LiteralPath $UnintendedIcuPath -Force
        }
    }
    Write-Host "Build completed: $DistDir\DJI_Radiometric_Calibration_Tool"
} finally {
    Pop-Location
}

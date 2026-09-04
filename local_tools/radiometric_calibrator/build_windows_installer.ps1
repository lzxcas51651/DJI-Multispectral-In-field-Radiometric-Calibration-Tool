param(
    [string]$Version = '1.0.1',
    [switch]$SkipExeBuild,
    [switch]$PrepareOnly,
    [switch]$UseStandaloneWix,
    [string]$WixExe,
    [string]$WixUIExtension
)

$ErrorActionPreference = 'Stop'
$ToolDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallerDir = Join-Path $ToolDir 'installer'
$PayloadDir = Join-Path $ToolDir 'dist\DJI_Radiometric_Calibration_Tool'
$GeneratedDir = Join-Path $InstallerDir 'generated'
$ReleaseDir = Join-Path $InstallerDir 'release'
$PythonExe = Join-Path $ToolDir '.build-venv\Scripts\python.exe'

# Windows Installer compares the first three version fields only.
if ($Version -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') {
    throw 'Version must have exactly three numeric fields, for example 1.0.1.'
}
$Parts = $Version.Split('.')
if ([long]$Parts[0] -gt 255 -or [long]$Parts[1] -gt 255 -or [long]$Parts[2] -gt 65535) {
    throw 'MSI version fields must be <= 255.255.65535.'
}
if (-not $PrepareOnly -and $UseStandaloneWix) {
    # Official, version-pinned NuGet packages; isolated to ignored build output.
    New-Item -ItemType Directory -Path $GeneratedDir -Force | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    foreach ($Package in @('wixtoolset.sdk', 'wixtoolset.ui.wixext')) {
        $Destination = Join-Path $GeneratedDir "$Package-4.0.6"
        $Marker = if ($Package -eq 'wixtoolset.sdk') {
            Join-Path $Destination 'tools\net472\x64\wix.exe'
        } else {
            Join-Path $Destination 'wixext4\WixToolset.UI.wixext.dll'
        }
        if (-not (Test-Path -LiteralPath $Marker)) {
            $Archive = Join-Path $GeneratedDir "$Package-4.0.6.zip"
            Invoke-WebRequest -UseBasicParsing `
                -Uri "https://api.nuget.org/v3-flatcontainer/$Package/4.0.6/$Package.4.0.6.nupkg" `
                -OutFile $Archive -TimeoutSec 60
            Expand-Archive -LiteralPath $Archive -DestinationPath $Destination -Force
        }
    }
    $WixExe = Join-Path $GeneratedDir 'wixtoolset.sdk-4.0.6\tools\net472\x64\wix.exe'
    $WixUIExtension = Join-Path $GeneratedDir 'wixtoolset.ui.wixext-4.0.6\wixext4\WixToolset.UI.wixext.dll'
}
if (-not $PrepareOnly -and $WixExe) {
    if (-not (Test-Path -LiteralPath $WixExe -PathType Leaf) -or
        -not $WixUIExtension -or -not (Test-Path -LiteralPath $WixUIExtension -PathType Leaf)) {
        throw 'Standalone build requires existing -WixExe and -WixUIExtension paths.'
    }
    $WixExe = (Resolve-Path -LiteralPath $WixExe).Path
    $WixUIExtension = (Resolve-Path -LiteralPath $WixUIExtension).Path
} elseif (-not $PrepareOnly) {
    if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
        throw 'Install the .NET 8 SDK (x64) on the BUILD computer, then reopen PowerShell.'
    }
    $Sdks = & dotnet --list-sdks
    if ($LASTEXITCODE -ne 0 -or -not ($Sdks | Where-Object { $_ -match '^(8|9|[1-9][0-9])\.' })) {
        throw 'Only the .NET runtime is installed. Install .NET 8 SDK x64. Use -PrepareOnly to validate the payload without building MSI.'
    }
}
if (-not $SkipExeBuild -and -not $PrepareOnly) {
    & (Join-Path $ToolDir 'build_windows_exe.ps1')
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw 'Build the EXE first using build_windows_exe.ps1 to create the isolated Python environment.'
}
New-Item -ItemType Directory -Path $GeneratedDir -Force | Out-Null
& $PythonExe (Join-Path $InstallerDir 'generate_payload.py') `
    --payload $PayloadDir --output (Join-Path $GeneratedDir 'Payload.wxs')
if ($LASTEXITCODE -ne 0) { throw 'Payload validation/generation failed.' }
if ($PrepareOnly) {
    Write-Host 'Payload prepared. No MSI has been compiled and nothing has been installed.'
    return
}

# Test the actual release, not the source interpreter. No installation occurs.
$DiagnosticPath = Join-Path $GeneratedDir ('startup-' + [guid]::NewGuid().ToString('N') + '.json')
$ExePath = Join-Path $PayloadDir 'DJI_Radiometric_Calibration_Tool.exe'
$Process = Start-Process -FilePath $ExePath `
    -ArgumentList "--diagnose-file `"$DiagnosticPath`"" -PassThru -WindowStyle Hidden
if (-not $Process.WaitForExit(30000)) {
    Stop-Process -Id $Process.Id -ErrorAction SilentlyContinue
    throw 'Release startup test timed out. MSI was not built.'
}
if ($Process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $DiagnosticPath)) {
    throw "Release startup test failed. Inspect $DiagnosticPath"
}
$Diagnostic = Get-Content -LiteralPath $DiagnosticPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Diagnostic.status -ne 'ok') { throw "Release startup test failed: $DiagnosticPath" }

New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null
Push-Location $InstallerDir
try {
    if ($WixExe) {
        & $WixExe build Package.wxs generated\Payload.wxs -arch x64 -culture zh-CN `
            -ext $WixUIExtension -d "ProductVersion=$Version" -d "PayloadDir=$PayloadDir" `
            -o (Join-Path $ReleaseDir "DJI_Radiometric_Calibrator_${Version}_x64.msi")
    } else {
        & dotnet build '.\RadiometricCalibrator.wixproj' --configuration Release `
            "-p:ProductVersion=$Version" "-p:PayloadDir=$PayloadDir" "-p:OutputPath=$ReleaseDir\"
    }
    if ($LASTEXITCODE -ne 0) { throw 'WiX build failed; do not distribute any old MSI as the new release.' }
} finally {
    Pop-Location
}
$MsiName = "DJI_Radiometric_Calibrator_${Version}_x64.msi"
$Msi = Get-ChildItem -LiteralPath $ReleaseDir -Recurse -File -Filter $MsiName |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Msi) { throw "Build returned successfully but $MsiName was not found." }
& (Join-Path $InstallerDir 'verify_msi.ps1') -MsiPath $Msi.FullName
Write-Host "MSI: $($Msi.FullName)"
Write-Host "SHA256: $((Get-FileHash -LiteralPath $Msi.FullName -Algorithm SHA256).Hash)"
Write-Host 'Build only: installation/repair/uninstallation still require testing on a disposable Windows VM.'

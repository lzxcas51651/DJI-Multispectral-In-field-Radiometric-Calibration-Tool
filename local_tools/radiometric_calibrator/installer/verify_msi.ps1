param([Parameter(Mandatory=$true)][string]$MsiPath)

# Read-only MSI database inspection. Never calls msiexec or installs anything.
$ErrorActionPreference = 'Stop'
$ResolvedMsi = (Resolve-Path -LiteralPath $MsiPath).Path
$Installer = New-Object -ComObject WindowsInstaller.Installer
$Database = $Installer.OpenDatabase($ResolvedMsi, 0)

function Read-MsiRows([string]$Sql, [int]$Columns) {
    $View = $Database.OpenView($Sql)
    [void]$View.Execute()
    try {
        while ($Record = $View.Fetch()) {
            $Values = @()
            for ($Index = 1; $Index -le $Columns; $Index++) {
                $Values += $Record.StringData($Index)
            }
            [pscustomobject]@{ Values = $Values }
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($Record)
        }
    } finally {
        [void]$View.Close()
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($View)
    }
}

try {
    $Files = @(Read-MsiRows 'SELECT `File` FROM `File`' 1)
    if (-not ($Files | Where-Object { $_.Values[0] -eq 'ApplicationExecutable' })) {
        throw 'MSI is missing the main executable.'
    }
    $Dialogs = @(Read-MsiRows 'SELECT `Dialog` FROM `Dialog`' 1)
    if (-not ($Dialogs | Where-Object { $_.Values[0] -eq 'MaintenanceTypeDlg' })) {
        throw 'MSI is missing the repair/remove maintenance dialog.'
    }
    $Media = @(Read-MsiRows 'SELECT `Cabinet` FROM `Media`' 1)
    if (-not $Media.Count -or ($Media | Where-Object { -not $_.Values[0].StartsWith('#') })) {
        throw 'MSI must embed every cabinet for single-file distribution.'
    }
    $Removals = @(Read-MsiRows 'SELECT `FileName` FROM `RemoveFile`' 1)
    if ($Removals | Where-Object { $_.Values[0] }) {
        throw 'Unexpected explicit file cleanup: inspect RemoveFile table before release.'
    }
    $Upgrades = @(Read-MsiRows 'SELECT `UpgradeCode` FROM `Upgrade`' 1)
    if (-not $Upgrades.Count) { throw 'Major upgrade/downgrade rules are missing.' }
    Write-Host "MSI database verified: $($Files.Count) files, embedded cabinets, maintenance UI, upgrade rules."
} finally {
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($Database)
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($Installer)
}

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework

$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Join-Path $env:LOCALAPPDATA 'PrintFlowCRM'
$InstallDir = Join-Path $Root 'App'
$BackupsDir = Join-Path $Root 'backups'
$WasInstalled = Test-Path (Join-Path $InstallDir 'PrintFlowCRM.pyw')

function Ask-YesNo([string]$Message, [string]$Title='PrintFlow CRM Setup') {
    $result = [System.Windows.MessageBox]::Show($Message, $Title, 'YesNo', 'Question')
    return $result -eq 'Yes'
}

function Find-PythonW {
    $candidates = @()
    foreach ($name in @('pyw.exe','pythonw.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates += $cmd.Source }
    }
    $patterns = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\pythonw.exe'),
        (Join-Path $env:ProgramFiles 'Python*\pythonw.exe')
    )
    if (${env:ProgramFiles(x86)}) { $patterns += (Join-Path ${env:ProgramFiles(x86)} 'Python*\pythonw.exe') }
    foreach ($pattern in $patterns) {
        try {
            $candidates += Get-ChildItem $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object FullName
        } catch {}
    }
    return $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

function Install-PythonWithWinget {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { return $false }
    foreach ($id in @('Python.Python.3.14','Python.Python.3.13','Python.Python.3.12')) {
        try {
            $args = @('install','-e','--id',$id,'--scope','user','--accept-package-agreements','--accept-source-agreements','--silent')
            $proc = Start-Process -FilePath $winget.Source -ArgumentList $args -Wait -PassThru
            if ($proc.ExitCode -eq 0 -or $proc.ExitCode -eq -1978335189) { return $true }
        } catch {}
    }
    return $false
}

function New-PrintFlowShortcut([string]$ShortcutPath, [string]$PythonExe) {
    $parent = Split-Path -Parent $ShortcutPath
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    if ([IO.Path]::GetFileName($PythonExe).ToLowerInvariant() -eq 'pyw.exe') {
        $shortcut.TargetPath = $PythonExe
        $shortcut.Arguments = '-3 "' + (Join-Path $InstallDir 'PrintFlowCRM.pyw') + '"'
    } else {
        $shortcut.TargetPath = $PythonExe
        $shortcut.Arguments = '"' + (Join-Path $InstallDir 'PrintFlowCRM.pyw') + '"'
    }
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.Description = 'PrintFlow CRM - 3D printing business workflow'
    $shortcut.Save()
}

New-Item -ItemType Directory -Force -Path $InstallDir,$BackupsDir | Out-Null

# Preserve the replaceable app folder before an upgrade. User data/database are already outside App.
if ($WasInstalled) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $appBackup = Join-Path $BackupsDir ("App-before-installer-" + $stamp)
    New-Item -ItemType Directory -Force -Path $appBackup | Out-Null
    Get-ChildItem $InstallDir -File -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $appBackup $_.Name) -Force
    }
}

foreach ($name in @('PrintFlowCRM.pyw','MessengerCapture.pyw','SetupWizard.pyw','update_manifest.json','requirements.txt')) {
    $src = Join-Path $Source $name
    if (-not (Test-Path $src)) { throw "Installer package is missing $name" }
    Copy-Item $src (Join-Path $InstallDir $name) -Force
}

$python = Find-PythonW
if (-not $python) {
    $auto = Ask-YesNo "Python 3 is required to run PrintFlow CRM, but it was not found.`n`nWould you like the installer to try installing Python automatically with Windows Package Manager (winget)?"
    if ($auto) {
        $ok = Install-PythonWithWinget
        Start-Sleep -Seconds 2
        $python = Find-PythonW
        if (-not $ok -or -not $python) {
            [System.Windows.MessageBox]::Show("Automatic Python installation did not complete. Install Python 3 from python.org, then run this installer again. Your PrintFlow data was not changed.", 'PrintFlow CRM', 'OK', 'Error') | Out-Null
            exit 1
        }
    } else {
        [System.Windows.MessageBox]::Show("Install Python 3 from python.org with the Python Launcher enabled, then run this installer again. Your PrintFlow data was not changed.", 'PrintFlow CRM', 'OK', 'Information') | Out-Null
        exit 1
    }
}

$desktop = [Environment]::GetFolderPath('Desktop')
$desktopShortcut = Join-Path $desktop 'PrintFlow CRM.lnk'
if (Ask-YesNo 'Create a PrintFlow CRM shortcut on the Desktop?') {
    New-PrintFlowShortcut $desktopShortcut $python
}

$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'PrintFlow CRM\PrintFlow CRM.lnk'
if (Ask-YesNo 'Add PrintFlow CRM to the Windows Start menu?') {
    New-PrintFlowShortcut $startMenu $python
}

$wizard = Join-Path $InstallDir 'SetupWizard.pyw'
$runWizardText = if ($WasInstalled) {
    'PrintFlow CRM was updated without touching your database or customer files.`n`nRun the guided setup wizard now to review BambuBuddy, VPN, packaging, and other settings?'
} else {
    'PrintFlow CRM is installed.`n`nRun the guided first-time setup now? This is recommended and will configure BambuBuddy, your printer, Tailscale or another VPN, packaging location, and optional features.'
}

if (Ask-YesNo $runWizardText) {
    if ([IO.Path]::GetFileName($python).ToLowerInvariant() -eq 'pyw.exe') {
        Start-Process -FilePath $python -ArgumentList @('-3', ('"' + $wizard + '"')) -WorkingDirectory $InstallDir
    } else {
        Start-Process -FilePath $python -ArgumentList ('"' + $wizard + '"') -WorkingDirectory $InstallDir
    }
} else {
    $app = Join-Path $InstallDir 'PrintFlowCRM.pyw'
    if ([IO.Path]::GetFileName($python).ToLowerInvariant() -eq 'pyw.exe') {
        Start-Process -FilePath $python -ArgumentList @('-3', ('"' + $app + '"')) -WorkingDirectory $InstallDir
    } else {
        Start-Process -FilePath $python -ArgumentList ('"' + $app + '"') -WorkingDirectory $InstallDir
    }
}

[System.Windows.MessageBox]::Show("PrintFlow CRM installation is complete.`n`nApp: $InstallDir`nData: $Root`n`nUpdates never replace the database/customer-data folder.", 'PrintFlow CRM', 'OK', 'Information') | Out-Null

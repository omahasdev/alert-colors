<#
.SYNOPSIS
    Build, install and maintain alert-colors.

.DESCRIPTION
    build      Make .venv from requirements-build.txt, run the tests, build dist\AlertColors
               with the modules of build.toml. -Lighting / -Sources override a list for
               one build: a comma list, or none (for example -Lighting none: API poller only).
    install    Build, then (elevated) copy to Program Files, register in Armoury Crate,
               add the logon task, start.
    update     Build, then (elevated) stop, copy, start. Use after a code change.
    restart    (elevated) Stop, copy config.toml, start. Use after a change in config.toml.
    uninstall  (elevated) Stop, remove the task, the Armoury Crate entry and the installed files.
    status     Show the process, the task, the Armoury Crate entry and the last log lines.
    token      Save or remove the token of an alert service (user environment, no
               config change), then restart the installed app.

    Security layout:
    - The app runs with admin rights. So it runs from $env:ProgramFiles\AlertColors,
      where only administrators can change files. Malware without admin rights
      cannot replace the exe, a DLL or the config there.
    - The build runs WITHOUT admin rights. The elevated part is this PowerShell
      only: it never runs Python, pip or the exe from the project folder.
    - The script never kills the app. It signals the stop event, and the app
      releases the LEDs and exits. A killed app leaves Armoury Crate without
      its devices until LightingService restarts.

.EXAMPLE
    setup.cmd
    Double-click in Explorer: shows a menu.

.EXAMPLE
    setup.cmd status
    Runs one action without the menu.
#>
param(
    [ValidateSet('', 'build', 'install', 'update', 'restart', 'uninstall', 'status', 'tune', 'region', 'token')]
    [string]$Action = '',
    [string]$Python = '',  # empty: find Python automatically (py launcher, then PATH)
    [string]$Lighting = '', # empty: build.toml; 'none'; or 'aura_sdk,razer_chroma'
    [string]$Sources = '',  # empty: build.toml; 'none'; or 'ubilling,alerts_in_ua'
    [int]$StopTimeoutSeconds = 120,  # > backend_close_timeout_s: an Aura release can need 44 s
    [int]$RestartCount = 3,
    [int]$RestartIntervalMinutes = 1,
    [int]$LogLines = 10,
    [switch]$Elevated,     # internal: the admin part of an action
    [switch]$WaitAtEnd     # keep the window open
)
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- paths ----
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root '.venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$Requirements = Join-Path $Root 'requirements-build.txt'
$Spec = Join-Path $Root 'packaging\AlertColors.spec'
$BuiltApp = Join-Path $Root 'dist\AlertColors'
$ProjectConfig = Join-Path $Root 'config.toml'
# The settings of this PC (for example its watched place) stay outside the project.
# If this file exists, it replaces config.toml of the project.
$MachineConfig = Join-Path $env:APPDATA 'AlertColors\config.toml'
$SourceConfig = if (Test-Path $MachineConfig) { $MachineConfig } else { $ProjectConfig }
$BuiltManifest = Join-Path $BuiltApp 'build-manifest.json'

$InstallDir = Join-Path $env:ProgramFiles 'AlertColors'
$InstallApp = Join-Path $InstallDir 'app'
$InstallExe = Join-Path $InstallApp 'AlertColors.exe'
$InstallConfig = Join-Path $InstallDir 'config.toml'
$InstallLog = Join-Path $InstallDir 'alert-colors.log'
# The configurator (normal rights) writes the tuning file here; the app only reads it.
$TuningDir = Join-Path $env:ProgramData 'AlertColors'

$ProcessList = Join-Path $env:ProgramData 'ASUS\ProcessList.ini'
$ProcessListBackup = "$ProcessList.alert-colors.bak"
$LegacyExe = Join-Path $BuiltApp 'AlertColors.exe'  # path of installs before 0.2
$TaskName = 'AlertColors'
$ProcessName = 'AlertColors'
$StopEventName = 'Local\AlertColors.Stop'  # = [service] stop_event in config.toml
$AdminActions = @('install', 'update', 'restart', 'uninstall')
$BuildActions = @('build', 'install', 'update')
$ConfigActions = @('install', 'update', 'restart')  # these copy config.toml: check it first
$PlaceUnchanged = 3  # exit code of the region and token commands: nothing to apply
$WantedPython = (Get-Content (Join-Path $Root '.python-version') -Raw).Trim()
$WingetPythonId = "Python.Python.$WantedPython"
$Menu = [ordered]@{
    '1' = @('install', 'Install: build, register in Armoury Crate, autostart, start')
    '2' = @('update', 'Update: build the current source, stop, copy, start')
    '3' = @('restart', 'Restart: apply config.toml (stop, copy config, start)')
    '4' = @('status', 'Status')
    '7' = @('tune', 'Configurator: tune the running app live (same as configure.cmd)')
    '5' = @('uninstall', 'Uninstall: stop, remove autostart, Armoury Crate entry and files')
    '6' = @('build', 'Build only')
    '8' = @('region', 'Watched place: choose the city, community, district or oblast')
    '9' = @('token', 'Tokens: save or remove the token of an alert service')
}

# -------------------------------------------------------------- helpers ----
function Test-Admin {
    $identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-Checked([string]$File, [string[]]$Arguments) {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

function Get-App { Get-Process -Name $ProcessName -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------- build ----
function Get-PythonVersion([string]$Exe) {
    try { return (& $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null) } catch { return $null }
}

function Find-Python {
    if ($Python) {
        if ((Get-PythonVersion $Python) -ne $WantedPython) { throw "'$Python' is not Python $WantedPython." }
        return $Python
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $path = & py "-$WantedPython" -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $path) { return $path.Trim() }
    }
    foreach ($candidate in @('python', 'python3')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command -and (Get-PythonVersion $command.Source) -eq $WantedPython) { return $command.Source }
    }
    return $null
}

function Install-Python {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Python $WantedPython is not installed, and winget is not available. Install it from python.org."
    }
    $answer = Read-Host "Python $WantedPython is not installed. Install it now with winget ($WingetPythonId)? [y/N]"
    if ($answer -notmatch '^[yY]') { throw "Python $WantedPython is necessary for the build." }
    Invoke-Checked winget @('install', '--id', $WingetPythonId, '--exact', '--silent', '--scope', 'user',
        '--accept-package-agreements', '--accept-source-agreements')
    # winget changes PATH for new processes only: read it again for this one.
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
}

function Initialize-Venv {
    if (-not (Test-Path $VenvPython)) {
        $base = Find-Python
        if (-not $base) { Install-Python; $base = Find-Python }
        if (-not $base) { throw "Python $WantedPython was installed, but not found. Open a new window and try again." }
        Write-Output "Making .venv with $base"
        Invoke-Checked $base @('-m', 'venv', $Venv)
    }
    if ((Get-PythonVersion $VenvPython) -ne $WantedPython) {
        throw ".venv does not have Python $WantedPython. Delete the .venv folder and run build again."
    }
    # --require-hashes: pip refuses any package file whose hash is not in the file.
    Invoke-Checked $VenvPython @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check',
        '--require-hashes', '--only-binary', ':all:', '-r', $Requirements)
}

function Invoke-Build {
    if (Test-Admin) { Write-Warning 'The build runs with admin rights. Start setup.cmd without "Run as administrator".' }
    Initialize-Venv
    Push-Location $Root
    try {
        Invoke-Checked $VenvPython @('-m', 'pytest', '-q', '-p', 'no:cacheprovider')
        $env:PYTHONHASHSEED = '0'  # stable bytecode order between builds
        # PowerShell deletes a variable that is set to '': so 'none' is a word.
        foreach ($pair in @(@('ALERT_COLORS_LIGHTING', $Lighting), @('ALERT_COLORS_SOURCES', $Sources))) {
            if ($pair[1]) { Set-Item "Env:$($pair[0])" $pair[1] }
            else { Remove-Item "Env:$($pair[0])" -ErrorAction SilentlyContinue }
        }
        Invoke-Checked $VenvPython @('-m', 'PyInstaller', '--noconfirm', '--clean',
            '--distpath', (Join-Path $Root 'dist'), '--workpath', (Join-Path $Root 'build'), $Spec)
    }
    finally { Pop-Location }
    Write-Output "Built $BuiltApp"
    Write-Output (Format-Manifest (Get-Content $BuiltManifest -Raw | ConvertFrom-Json))
}

function Format-Manifest($Manifest) {
    $light = if ($Manifest.lighting) { $Manifest.lighting -join ', ' } else { 'none (API poller only)' }
    $src = if ($Manifest.sources) { $Manifest.sources -join ', ' } else { 'json_http only' }
    return "Version $($Manifest.version). Lighting: $light. Sources: json_http, $src. Admin: $($Manifest.needs_admin)."
}

function Stop-AppAsUser {
    # The stop event lets the user who runs the app signal it, so this needs no
    # admin rights. A normal process cannot wait on an elevated one: poll instead.
    if (-not (Get-App)) { return $false }
    try {
        $event = [System.Threading.EventWaitHandle]::OpenExisting($StopEventName)
        [void]$event.Set()
        $event.Dispose()
    }
    catch { throw "AlertColors runs, but its stop event is missing. Do not kill it. Look at $InstallLog." }
    $deadline = (Get-Date).AddSeconds($StopTimeoutSeconds)
    while ((Get-App) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
    if (Get-App) { throw "AlertColors did not stop within $StopTimeoutSeconds s. Do not kill it. Look at $InstallLog." }
    Write-Output 'Stopped AlertColors for the check (LEDs released)'
    return $true
}

function Test-Config {
    # The gate before an install: a config that cannot fetch is never deployed,
    # so a running app is never replaced by a broken one. The running app is
    # stopped first: an API with a rate limit for each IP (siren.pp.ua: ~1
    # request/s) gives a second process only HTTP 429. If the check fails, the
    # old app starts again unchanged.
    if (-not (Test-Path $VenvPython)) { Initialize-Venv }
    $wasRunning = Stop-AppAsUser
    $env:PYTHONPATH = Join-Path $Root 'src'
    try { & $VenvPython -m alert_colors --config $SourceConfig check }
    finally { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    if ($LASTEXITCODE -ne 0) {
        if ($wasRunning) { Start-ScheduledTask -TaskName $TaskName; Write-Output 'Started the old app again.' }
        throw "config.toml failed the check. Nothing was installed; the old app runs unchanged."
    }
}

function Set-Token {
    # Saves or removes the token of one alert service in the user environment
    # (no admin rights, no config change). Returns $true when a token changed.
    if (-not (Test-Path $VenvPython)) { Initialize-Venv | Out-Host }
    $env:PYTHONPATH = Join-Path $Root 'src'
    try {
        $tool = Start-Process -FilePath $VenvPython -NoNewWindow -Wait -PassThru `
            -ArgumentList @('-m', 'alert_colors', '--config', "`"$SourceConfig`"", 'token')
    }
    finally { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    if ($tool.ExitCode -eq $PlaceUnchanged) { return $false }
    if ($tool.ExitCode -ne 0) { throw 'The token was not saved.' }
    return $true
}

function Select-Place {
    # Shows the watched place of config.toml and lets the user search the region
    # tree of the API (no admin rights). Returns $true when config.toml changed.
    # The choice goes to the config of this PC, never into the project.
    # Out-Host: messages in a function would become part of its return value.
    if (-not (Test-Path $VenvPython)) { Initialize-Venv | Out-Host }
    $created = -not (Test-Path $MachineConfig)
    if ($created) {
        New-Item -ItemType Directory -Force -Path (Split-Path $MachineConfig) | Out-Null
        Copy-Item $ProjectConfig $MachineConfig
    }
    $env:PYTHONPATH = Join-Path $Root 'src'
    # A direct console process: its questions show at once, and its output does
    # not become the return value of this function.
    try {
        $picker = Start-Process -FilePath $VenvPython -NoNewWindow -Wait -PassThru `
            -ArgumentList @('-m', 'alert_colors', '--config', "`"$MachineConfig`"", 'region')
    }
    finally { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    $code = $picker.ExitCode
    if ($code -ne 0 -and $created) { Remove-Item $MachineConfig }  # nothing chosen: keep the project config
    if ($code -eq $PlaceUnchanged) { return $false }
    if ($code -ne 0) { throw 'The place selection failed. No config changed.' }
    $script:SourceConfig = $MachineConfig
    return $true
}

function Get-Manifest {
    if (-not (Test-Path $BuiltManifest)) { throw "No build in $BuiltApp. Run build first." }
    return Get-Content $BuiltManifest -Raw | ConvertFrom-Json
}

# ---------------------------------------------------- elevated: process ----
function Stop-App {
    Write-Output "Config source: $SourceConfig"
    $app = Get-App
    if (-not $app) { return }
    try {
        $event = [System.Threading.EventWaitHandle]::OpenExisting($StopEventName)
        [void]$event.Set()
        $event.Dispose()
    }
    catch { throw "AlertColors runs, but its stop event is missing. Do not kill it. Look at $InstallLog." }
    $app | Wait-Process -Timeout $StopTimeoutSeconds -ErrorAction SilentlyContinue
    if (Get-App) { throw "AlertColors did not stop within $StopTimeoutSeconds s. Do not kill it. Look at $InstallLog." }
    Write-Output 'Stopped AlertColors (LEDs released)'
}

function Start-App {
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "Started task $TaskName. Aura devices follow after the first device scan (~30 s)."
}

# ------------------------------------------------------ elevated: files ----
function Copy-App {
    if (-not (Test-Path (Join-Path $BuiltApp 'AlertColors.exe'))) { throw "No build in $BuiltApp. Run build first." }
    New-Item -ItemType Directory -Force -Path $InstallApp | Out-Null
    # /MIR: the installed app is an exact copy of the build (old files are removed).
    robocopy $BuiltApp $InstallApp /MIR /NJH /NJS /NFL /NDL /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }
    $global:LASTEXITCODE = 0
    Write-Output "Copied the app to $InstallApp"
}

function Copy-Config {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item $SourceConfig $InstallConfig -Force
    Write-Output "Copied config.toml to $InstallConfig"
}

function Initialize-TuningDir {
    New-Item -ItemType Directory -Force -Path $TuningDir | Out-Null
    # Write rights for the installing user only; other users can only read.
    $user = "$env:USERDOMAIN\$env:USERNAME"
    icacls $TuningDir /grant "${user}:(OI)(CI)M" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed on $TuningDir" }
    Write-Output "Tuning folder: $TuningDir (writable by $user)"
}

function Remove-InstallDir {
    if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force }
    Write-Output "Removed $InstallDir"
    if (Test-Path $TuningDir) { Remove-Item $TuningDir -Recurse -Force; Write-Output "Removed $TuningDir" }
}

# ---------------------------------------------- elevated: Armoury Crate ----
# ProcessList.ini is UTF-16 LE with a BOM. Each "<exe>=0" line is an app in the
# Armoury Crate "Aura In-Game Priority" list.
function Read-ProcessList { [IO.File]::ReadAllLines($ProcessList, [Text.Encoding]::Unicode) }

function Write-ProcessList([string[]]$Lines) {
    if (-not (Test-Path $ProcessListBackup)) { Copy-Item $ProcessList $ProcessListBackup }
    [IO.File]::WriteAllLines($ProcessList, $Lines, [Text.UnicodeEncoding]::new($false, $true))
}

function Test-EntryOf([string]$Line, [string]$Exe) {
    return ($Line -split '=', 2)[0].Trim() -ieq $Exe
}

function Set-ArmouryCrateEntry([bool]$Present) {
    if (-not (Test-Path $ProcessList)) { Write-Warning "$ProcessList not found: is Armoury Crate installed?"; return }
    $lines = @(Read-ProcessList)
    $kept = @($lines | Where-Object { -not (Test-EntryOf $_ $InstallExe) -and -not (Test-EntryOf $_ $LegacyExe) })
    if ($Present) {
        $section = [array]::IndexOf($kept, '[ProcessList]')
        if ($section -lt 0) { throw "$ProcessList has no [ProcessList] section" }
        $kept = @($kept[0..$section]) + @("$InstallExe=0") + @($kept | Select-Object -Skip ($section + 1))
    }
    if (($kept -join "`n") -ne ($lines -join "`n")) { Write-ProcessList $kept }
    Write-Output "Armoury Crate entry: $(if ($Present) { $InstallExe } else { 'removed' })"
}

# ------------------------------------------------------- elevated: task ----
function Register-Task([bool]$NeedsAdmin) {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $runLevel = if ($NeedsAdmin) { 'Highest' } else { 'Limited' }
    $action = New-ScheduledTaskAction -Execute $InstallExe -Argument "--config `"$InstallConfig`" run" -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    # Highest only when a module needs it (aura_sdk: motherboard and ARGB headers).
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel $runLevel
    # No time limit: at the limit, Task Scheduler kills the process. Restart after a crash.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
        -RestartCount $RestartCount -RestartInterval (New-TimeSpan -Minutes $RestartIntervalMinutes)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description 'Air-raid alert status on RGB lighting' -Force | Out-Null
    Write-Output "Registered task $TaskName -> $InstallExe (run level: $runLevel)"
}

# --------------------------------------------------------------- status ----
function Show-Status {
    Write-Output "Config source: $SourceConfig"
    $app = Get-App
    if ($app) { Write-Output "Process: running, PID $($app.Id), started $($app.StartTime)" }
    else { Write-Output 'Process: not running' }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Write-Output "Task: $($task.State) -> $($task.Actions[0].Execute)" } else { Write-Output 'Task: not registered' }
    $registered = (Test-Path $ProcessList) -and (@(Read-ProcessList) | Where-Object { Test-EntryOf $_ $InstallExe })
    Write-Output "Armoury Crate entry: $(if ($registered) { 'registered' } else { 'not registered' })"
    Write-Output "Installed: $(if (Test-Path $InstallExe) { $InstallDir } else { 'no' })"
    $installedManifest = Join-Path $InstallApp 'build-manifest.json'
    if (Test-Path $installedManifest) {
        Write-Output (Format-Manifest (Get-Content $installedManifest -Raw | ConvertFrom-Json))
    }
    if (Test-Path $InstallLog) { Write-Output '--- log'; Get-Content $InstallLog -Tail $LogLines }
}

# ----------------------------------------------------------------- main ----
function Invoke-AdminPart([string]$Name) {
    switch ($Name) {
        'install' {
            $manifest = Get-Manifest
            Stop-App; Copy-App; Copy-Config; Initialize-TuningDir
            Set-ArmouryCrateEntry ([bool]$manifest.needs_armoury_crate)
            Register-Task ([bool]$manifest.needs_admin)
            Start-App
        }
        'update' {
            # The modules can change between builds: update the entry and the task too.
            $manifest = Get-Manifest
            Stop-App; Copy-App; Copy-Config; Initialize-TuningDir
            Set-ArmouryCrateEntry ([bool]$manifest.needs_armoury_crate)
            Register-Task ([bool]$manifest.needs_admin)
            Start-App
        }
        'restart' { Stop-App; Copy-Config; Start-App }
        'uninstall' {
            Stop-App
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
            Write-Output "Removed task $TaskName"
            Set-ArmouryCrateEntry $false
            Remove-InstallDir
        }
    }
}

$interactive = -not $Action
$failed = $false
try {
    if ($interactive) {
        Write-Output 'alert-colors setup'
        foreach ($key in $Menu.Keys) { Write-Output "  $key. $($Menu[$key][1])" }
        $choice = Read-Host 'Choose a number'
        if (-not $Menu.Contains($choice)) { Write-Output 'No action.'; return }
        $Action = $Menu[$choice][0]
    }

    if ($Action -eq 'status') { Show-Status; return }
    if ($Action -eq 'tune') { & (Join-Path $Root 'configure.cmd'); return }
    if ($Action -eq 'token') {
        if (-not (Set-Token)) { return }
        if (-not (Test-Path $InstallExe)) { Write-Output 'Saved. The app uses it after the install.'; return }
        $Action = 'restart'  # the app orders its sources at the start
    }
    if ($Action -eq 'region') {
        if (-not (Select-Place)) { return }
        if (-not (Test-Path $InstallExe)) { Write-Output 'Saved. Choose Install to start the app.'; return }
        $Action = 'restart'  # apply the new place to the installed app
    }
    if ($ConfigActions -contains $Action) { Write-Output "Config: $SourceConfig" }
    if (-not $Elevated -and $BuildActions -contains $Action) { Invoke-Build }
    # The first install proposes the place. After `setup.cmd region`, the config of this PC
    # exists, and the install does not ask again.
    if (-not $Elevated -and $Action -eq 'install' -and -not (Test-Path $MachineConfig)) { [void](Select-Place) }
    if (-not $Elevated -and $ConfigActions -contains $Action) { Test-Config }
    if ($AdminActions -contains $Action) {
        if (Test-Admin) { Invoke-AdminPart $Action }
        else {
            $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
                '-Action', $Action, '-Elevated', '-WaitAtEnd', '-StopTimeoutSeconds', $StopTimeoutSeconds)
            Start-Process powershell -Verb RunAs -ArgumentList $arguments
            Write-Output 'The admin part continues in an elevated window.'
        }
    }
}
catch {
    Write-Output "ERROR: $($_.Exception.Message)"
    $failed = $true
}
finally {
    if ($interactive -or $WaitAtEnd) { Read-Host 'Press Enter to close' | Out-Null }
}
if ($failed) { exit 1 }

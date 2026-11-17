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
    tune       Open the configurator in the browser to change the look of each state.
    region     Choose the watched place in the region tree of the alert service.

    Security layout:
    - The app runs with admin rights. So it runs from Program Files\AlertColors,
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
    [int]$StopTimeoutSeconds = 120,  # > backend_close_timeout_s: an Aura release can need tens of seconds
    [int]$RestartCount = 3,
    [int]$RestartIntervalMinutes = 1,
    [int]$LogLines = 10,
    [switch]$Elevated,     # internal: the admin part of an action
    [string]$InstallUser = '', # internal: the account that started setup.cmd (the task and the tuning folder)
    [string]$InstallConfigSource = '', # internal: the config that the part without admin rights chose
    [switch]$WaitAtEnd     # keep the window open
)
$ErrorActionPreference = 'Stop'
# Cmdlets load their modules from PSModulePath, and the user can put a folder first in
# it. This script uses only the modules of Windows PowerShell itself.
$env:PSModulePath = Join-Path $PSHOME 'Modules'
# The account that the app is for. UAC can elevate with another admin account, so the
# elevated part gets the account that started setup.cmd (-InstallUser).
$TargetUser = if ($InstallUser) { $InstallUser } else { [Security.Principal.WindowsIdentity]::GetCurrent().Name }  # from Windows, not the environment

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
# In the elevated part, %APPDATA% can be of the admin account that approved UAC:
# the part without admin rights gives the config it chose.
if ($InstallConfigSource) { $SourceConfig = $InstallConfigSource }
$BuiltManifest = Join-Path $BuiltApp 'build-manifest.json'

# The machine folders come from Windows, not from the environment: the elevated
# part runs with the environment of the user, who can change it.
$ProgramFilesDir = [Environment]::GetFolderPath('ProgramFiles')
$ProgramDataDir = [Environment]::GetFolderPath('CommonApplicationData')
$InstallDir = Join-Path $ProgramFilesDir 'AlertColors'
$InstallApp = Join-Path $InstallDir 'app'
$InstallExe = Join-Path $InstallApp 'AlertColors.exe'
$InstallConfig = Join-Path $InstallDir 'config.toml'
$InstallLog = Join-Path $InstallDir 'alert-colors.log'
# The configurator (normal rights) writes the tuning file here; the app only reads it.
$TuningDir = Join-Path $ProgramDataDir 'AlertColors'
# Well-known SIDs: the same on every Windows. Names such as "Users" change with
# the Windows language, so icacls gets the SIDs (a leading * marks a SID).
$SidSystem = 'S-1-5-18'            # NT AUTHORITY\SYSTEM
$SidAdministrators = 'S-1-5-32-544' # BUILTIN\Administrators
$SidUsers = 'S-1-5-32-545'          # BUILTIN\Users
$SidTrustedInstaller = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'  # NT SERVICE\TrustedInstaller
# icacls rights: (OI)(CI) = also for the files and subfolders in the folder.
# The rights of the installing user in the tuning folder. On the folder itself: list it
# and add files, but never delete or rename it (Modify would allow that). On each file
# in it (inherit-only): Modify, for the configurator's .tmp file and its replace.
$UserFolderRights = '(RX,WD)'
$UserFileRights = '(OI)(IO)M'

$ProcessList = Join-Path $ProgramDataDir 'ASUS\ProcessList.ini'
$ProcessListBackup = "$ProcessList.alert-colors.bak"
$ProcessListMaxBytes = 1048576  # the priority list of Armoury Crate is a few KiB
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
    '5' = @('uninstall', 'Uninstall: stop, remove autostart, Armoury Crate entry and files')
    '6' = @('build', 'Build only')
    '7' = @('tune', 'Configurator: tune the running app live (same as configure.cmd)')
    '8' = @('region', 'Watched place: choose the city, community, district or oblast')
    '9' = @('token', 'Tokens: save or remove the token of an alert service')
}

# -------------------------------------------------------------- helpers ----
function Test-Admin {
    $identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-NoAdminPython([string]$Step) {
    # Python, pip, the tests and the build run from the project folder, which programs
    # without admin rights can change. With admin rights they would run that code as admin.
    if (Test-Admin) {
        throw "$Step does not run with admin rights. Close this window and start setup.cmd from a normal " +
              'console, without "Run as administrator". The setup asks for admin rights (UAC) only for the ' +
              'copy to Program Files. The setup needs UAC turned on, and an account that is not the ' +
              'built-in Administrator.'
    }
}

function Invoke-Checked([string]$File, [string[]]$Arguments) {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

function Get-App {
    # The app of this logon session only: another account can run a process with this name.
    $session = [Diagnostics.Process]::GetCurrentProcess().SessionId
    Get-Process -Name $ProcessName -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $session }
}

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
        try { $path = & py "-$WantedPython" -c "import sys; print(sys.executable)" 2>$null } catch { $path = $null }
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
    Assert-NoAdminPython 'The Python setup'
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
    Assert-NoAdminPython 'The build'
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
        $script:StoppedForCheck = $true  # from now on, a failed setup starts the app again
    }
    catch { throw "AlertColors runs, but its stop event is missing. Do not kill it. Look at $InstallLog." }
    $deadline = (Get-Date).AddSeconds($StopTimeoutSeconds)
    while ((Get-App) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
    if (Get-App) { throw "AlertColors did not stop within $StopTimeoutSeconds s. Do not kill it. Look at $InstallLog." }
    Write-Host 'Stopped AlertColors for the check (LEDs released)'
    return $true
}

function Test-Config {
    Assert-NoAdminPython 'The config check'
    # The gate before an install: a config that cannot fetch is never deployed,
    # so a running app is never replaced by a broken one. The running app is
    # stopped first: an API with a rate limit for each IP (siren.pp.ua: ~1
    # request/s) gives a second process only HTTP 429. If the check fails, the
    # old app starts again unchanged.
    if (-not (Test-Path $VenvPython)) { Initialize-Venv }
    $wasRunning = Stop-AppAsUser
    $script:StoppedForCheck = $wasRunning  # the admin part starts it again, else Restore-StoppedApp
    $env:PYTHONPATH = Join-Path $Root 'src'
    try { & $VenvPython -m alert_colors --config $SourceConfig check }
    finally { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    if ($LASTEXITCODE -ne 0) {
        $failed = 'config.toml failed the check. Nothing was installed'
        if ($wasRunning) {
            try {
                Start-ScheduledTask -TaskName $TaskName
                $script:RestoredApp = $true
                Write-Output 'Started the old app again.'
            }
            catch { throw "$failed, and the old app did not start again ($($_.Exception.Message))." }
        }
        throw "$failed; the old app runs unchanged."
    }
}

function Restore-StoppedApp {
    # The config check stopped the app, and the setup ends without an install (the UAC prompt
    # was canceled, or a step failed): start the old app again, unchanged. The app can still
    # release the LEDs after a stop that timed out: wait for it to exit first.
    if (-not $script:StoppedForCheck -or $script:RestoredApp -or $script:RestoreGaveUp) { return }
    if (Get-App) { Write-Output "Waiting up to $StopTimeoutSeconds s for AlertColors to exit, then it starts again..." }
    $deadline = (Get-Date).AddSeconds($StopTimeoutSeconds)
    while ((Get-App) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
    if (Get-App) {
        Write-Warning 'AlertColors still stops. When it has stopped, run setup.cmd restart.'
        $script:RestoreGaveUp = $true
        return
    }
    Start-ScheduledTask -TaskName $TaskName
    $script:RestoredApp = $true
    Write-Output 'Started the old app again: nothing was installed.'
}

function Set-Token {
    Assert-NoAdminPython 'The token tool'
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
    Assert-NoAdminPython 'The place picker'
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
    Write-Output "Started task $TaskName. Aura devices follow after the first device scan."
}

# ------------------------------------------------------ elevated: files ----
function Copy-App {
    if (-not (Test-Path (Join-Path $BuiltApp 'AlertColors.exe'))) { throw "No build in $BuiltApp. Run build first." }
    New-Item -ItemType Directory -Force -Path $InstallApp | Out-Null
    # /MIR: the installed app is an exact copy of the build (old files are removed).
    # /XJ: a junction inside the build folder is not followed. The build folder itself is in the
    # limit of SETUP.md ("Before the UAC prompt"): your account can change it until the copy ends.
    # /R and /W: without them robocopy retries a locked file a million times, 30 s apart. Another
    # account can run the installed exe or hold a DLL open, so bound the wait.
    robocopy $BuiltApp $InstallApp /MIR /XJ /R:$RobocopyRetries /W:$RobocopyWaitSeconds /NJH /NJS /NFL /NDL /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }
    $global:LASTEXITCODE = 0
    Write-Output "Copied the app to $InstallApp"
}

function Copy-Config {
    # A plain copy: the config can be in a OneDrive folder (its files are reparse points).
    # Your account can change this file and the build anyway (see SETUP.md, "Before the UAC
    # prompt"), so a check here would protect nothing more.
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item -LiteralPath $SourceConfig -Destination $InstallConfig -Force
    Write-Output "Copied config.toml to $InstallConfig"
}

function Test-ReparsePoint([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return [bool]($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))
}

function Assert-NoLinks([string]$Dir) {
    # The tuning folder holds files only: a link or any subfolder stops the setup.
    # -ErrorAction Stop: an entry that admins cannot read also stops it.
    if (Test-ReparsePoint $Dir) { throw "$Dir is a link (reparse point), not a folder. Delete it, then run setup.cmd again." }
    try {
        $entries = @(Get-ChildItem -LiteralPath $Dir -Force -Recurse -ErrorAction Stop)
    } catch {
        throw "Cannot check $Dir ($($_.Exception.Message)). Delete the folder, then run setup.cmd again."
    }
    # A hard link (a file with more than one name) is a link too: the rights of the
    # file below would then go to the file that it shares its data with.
    $bad = @($entries | Where-Object {
        $_.PSIsContainer -or ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -or ($_.LinkType -eq 'HardLink')
    })
    if ($bad.Count -gt 0) {
        throw "$Dir contains folders or links: $($bad.FullName -join ', '). Delete them, then run setup.cmd again."
    }
    # LinkType is empty when another process holds the file open. fsutil reads the
    # names of a file also then; a file it cannot read stops the setup too.
    $trusted = @($SidAdministrators, $SidSystem, (Get-TargetSid))
    foreach ($file in @($entries | Where-Object { -not $_.PSIsContainer })) {
        # A file of another account (also one whose owner cannot be read) is not read here:
        # its owner can refuse the read. Replace-Files deletes it after the lock.
        if ((Get-OwnerSid $file.FullName) -notin $trusted) { continue }
        try { $names = @(fsutil hardlink list $file.FullName 2>$null | Where-Object { $_.Trim() }) }
        catch { $names = $null }  # an error line of fsutil: the check below stops the setup
        if ($null -eq $names -or $LASTEXITCODE -ne 0) {
            throw "Cannot check $($file.FullName) for links. Delete it, then run setup.cmd again."
        }
        if ($names.Count -gt 1) {
            throw "$($file.FullName) is a hard link ($($names.Count) names). Delete it, then run setup.cmd again."
        }
    }
}

function New-LockedSecurity {
    # The whole access list: no inherited rules (ProgramData lets every user create
    # files in its subfolders) and no earlier explicit rules, also not of the installing
    # user. SYSTEM and Administrators: full control. No rule for Users: another account
    # cannot open the folder, so it cannot hold it open and stop the setup. The installing
    # user gets its own rights later (Grant-UserWrite).
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagate = [System.Security.AccessControl.PropagationFlags]::None
    foreach ($rule in @(@($SidSystem, 'FullControl'), @($SidAdministrators, 'FullControl'))) {
        $sid = New-Object System.Security.Principal.SecurityIdentifier($rule[0])
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $sid, $rule[1], $inherit, $propagate, 'Allow')))
    }
    return $acl
}

function Set-LockedRights([string]$Dir) {
    [System.IO.Directory]::SetAccessControl($Dir, (New-LockedSecurity))
}

function Grant-UserWrite([string]$Dir, [string]$Account) {
    icacls $Dir /L /grant "${Account}:${UserFolderRights}" "${Account}:${UserFileRights}" | Out-Null
    return $LASTEXITCODE -eq 0
}

function Remove-LinkAtName([string]$Path) {
    # Remove a link (a junction, a directory or file symbolic link) at Path: the link only, never
    # its target. A directory link needs Directory.Delete, a file link needs File.Delete.
    try { [IO.Directory]::Delete($Path) } catch [IO.IOException] { [IO.File]::Delete($Path) }
    Write-Output "Removed the link $Path"
}

function Remove-PlainFile([string]$Path) {
    # Not Remove-Item -Force: it first changes the attributes, and the owner of a file can
    # deny that. One handle with only the right to delete (from the delete-child right of the
    # folder, which admins have) deletes the name. A read-only file whose owner also denies the
    # change of its attributes cannot be deleted so: Replace-Files renames such a file.
    Use-FolderGuardType
    [AlertColorsFolderGuard]::DeleteFile($Path)
}

function Replace-Files([string]$Dir) {
    # Each file becomes a new file with the same content: owned by this admin process,
    # with only the rights it inherits from the locked folder. A handle that another
    # program opened on the old file keeps no rights on the new one. A file that another
    # program holds open cannot be deleted, and that stops the setup.
    # The new file comes first, under a new name: if a later step fails, the content
    # stays on disk.
    # Only files of Administrators, SYSTEM or the installing user: an old install did not lock
    # the folder, so another account could add a file to it. Such a file is deleted.
    $trusted = @($SidAdministrators, $SidSystem, (Get-TargetSid))
    Use-FolderGuardType
    foreach ($file in @(Get-ChildItem -LiteralPath $Dir -Force -File)) {
        if ($file.Name -like 'foreign-*') { continue }  # an aside from an earlier run: the app never reads it
        if ((Get-OwnerSid $file.FullName) -notin $trusted) {
            # A file of another account (an old install did not lock the folder). The delete uses the
            # delete-child right of the folder, so its own access list does not stop it. A read-only
            # file that also denies the change of its attributes cannot be deleted: rename it aside,
            # which needs only the same delete right. The aside name is a short fixed stem, so a long
            # original name cannot push it past the 255-character limit. The app reads only tuning.json.
            try { Remove-PlainFile $file.FullName; Write-Warning "Deleted $($file.FullName): a file of another account." }
            catch {
                $aside = Join-Path (Split-Path $file.FullName -Parent) "foreign-$([guid]::NewGuid().ToString('N'))"
                try { [AlertColorsFolderGuard]::RenameAside($file.FullName, $aside, $false) }
                catch {
                    throw "Cannot delete or rename $($file.FullName), a file of another account " +
                          "($($_.Exception.Message)). Close the programs of that account, then run setup.cmd again."
                }
                Write-Warning "Renamed $($file.FullName) aside: a file of another account that cannot be deleted."
            }
            continue
        }
        $new = "$($file.FullName).new-$([guid]::NewGuid().ToString('N'))"
        try {
            # The owner of a file can give itself rights again and turn it into a link: read
            # it through one handle that refuses a link, a second name and a large file.
            $content = [AlertColorsFolderGuard]::ReadPlainFile($file.FullName, $TuningMaxBytes)
            if ($null -eq $content) { throw 'not a plain file (a link, a file with more names, or too large)' }
            Write-NewFile $new $content
            Remove-PlainFile $file.FullName
            Rename-Item -LiteralPath $new -NewName $file.Name -ErrorAction Stop
        } catch {
            throw "Cannot renew $($file.FullName) ($($_.Exception.Message)). Its content is also in $new " +
                  'if that file exists. Close the configurator and other programs, then run setup.cmd again.'
        }
    }
}

function Assert-AdminOwner([string]$Dir) {
    # The owner of a folder can always change its rights, also through a handle that it
    # opened before the setup locked them. So the folder must belong to the machine.
    if (-not (Test-AdminOwner $Dir)) {
        throw "$Dir belongs to another account, not to Administrators. Delete the folder, " +
              'then run setup.cmd again: the setup then makes it new.'
    }
}

$TuningMaxBytes = 262144  # MAX_FILE_BYTES of tuning.py: the app reads no larger tuning file
$RobocopyRetries = 3
$RobocopyWaitSeconds = 2
$FolderGuardSource = @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class AlertColorsFolderGuard {
    // FILE_LIST_DIRECTORY is data access: only a handle with data access takes part in the
    // share checks, so without it the missing delete sharing would block nothing.
    const uint READ_CONTROL = 0x00020000, FILE_READ_ATTRIBUTES = 0x0080, FILE_LIST_DIRECTORY = 0x0001;
    const uint FILE_SHARE_READ = 0x1, FILE_SHARE_WRITE = 0x2;  // no FILE_SHARE_DELETE
    const uint OPEN_EXISTING = 3;
    const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000, FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000;
    const uint FILE_ATTRIBUTE_DIRECTORY = 0x10, FILE_ATTRIBUTE_REPARSE_POINT = 0x400;

    [StructLayout(LayoutKind.Sequential, Pack = 4)]  // FILETIME is 4-byte aligned
    struct FileInfo {
        public uint Attributes;
        public long Created, Accessed, Written;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security,
                                             uint disposition, uint flags, IntPtr template);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInfo info);

    // The folder itself (never the target of a link), with no delete sharing: while the
    // handle is open, nobody can rename or delete the folder, and the open fails when
    // another program holds it with delete access.
    public static SafeFileHandle Open(string path) {
        SafeFileHandle handle = CreateFileW(path, FILE_LIST_DIRECTORY | READ_CONTROL | FILE_READ_ATTRIBUTES,
            FILE_SHARE_READ | FILE_SHARE_WRITE, IntPtr.Zero, OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, IntPtr.Zero);
        if (handle.IsInvalid) { throw new Win32Exception(Marshal.GetLastWin32Error()); }
        FileInfo info;
        if (!GetFileInformationByHandle(handle, out info)) {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(error);
        }
        if ((info.Attributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0 || (info.Attributes & FILE_ATTRIBUTE_DIRECTORY) == 0) {
            handle.Dispose();
            throw new InvalidOperationException("not a real folder (a link or a file)");
        }
        return handle;
    }

    const uint GENERIC_READ = 0x80000000, GENERIC_WRITE = 0x40000000, CREATE_NEW = 1;
    const uint DELETE = 0x00010000, FILE_SHARE_DELETE = 0x4;
    const int FileDispositionInfoEx = 21;
    const uint FILE_DISPOSITION_DELETE = 0x1, FILE_DISPOSITION_POSIX_SEMANTICS = 0x2,
        FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE = 0x10;

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetFileInformationByHandle(SafeFileHandle handle, int infoClass, ref uint info, uint size);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetFileInformationByHandle(SafeFileHandle handle, int infoClass, IntPtr info, uint size);

    [StructLayout(LayoutKind.Sequential)]
    struct UnicodeString { public ushort Length, MaximumLength; public IntPtr Buffer; }
    [StructLayout(LayoutKind.Sequential)]
    struct ObjectAttributes {
        public int Length; public IntPtr Root, Name; public uint Attributes; public IntPtr Sd, Qos;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct IoStatusBlock { public IntPtr Status, Information; }

    [DllImport("ntdll.dll")]
    static extern int NtOpenFile(out IntPtr handle, uint access, ref ObjectAttributes attributes,
        ref IoStatusBlock io, uint share, uint options);

    const uint OBJ_CASE_INSENSITIVE = 0x40;
    const uint FILE_DIRECTORY_FILE = 0x1, FILE_NON_DIRECTORY_FILE = 0x40;
    const int FileRenameInfo = 3;

    // A handle with only the right to delete or rename the name at path (a link itself, never its
    // target). The right comes from the delete-child right of the folder, so an access list on the
    // object that refuses the admin every read, and its ReadOnly attribute, do not stop this.
    static SafeFileHandle OpenForDelete(string path, uint kind) {
        UnicodeString name = new UnicodeString();
        string ntPath = @"\??\" + path;
        name.Buffer = Marshal.StringToHGlobalUni(ntPath);
        name.Length = (ushort)(ntPath.Length * 2);
        name.MaximumLength = (ushort)(name.Length + 2);
        IntPtr namePtr = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeString)));
        Marshal.StructureToPtr(name, namePtr, false);
        ObjectAttributes oa = new ObjectAttributes();
        oa.Length = Marshal.SizeOf(typeof(ObjectAttributes));
        oa.Name = namePtr;
        oa.Attributes = OBJ_CASE_INSENSITIVE;
        IoStatusBlock io = new IoStatusBlock();
        IntPtr handle;
        int status = NtOpenFile(out handle, DELETE, ref oa, ref io,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, FILE_FLAG_OPEN_REPARSE_POINT | kind);
        Marshal.FreeHGlobal(name.Buffer);
        Marshal.FreeHGlobal(namePtr);
        if (status < 0) {  // NT_SUCCESS is Status >= 0; a negative NTSTATUS is a failure
            throw new Win32Exception("the object cannot be opened to delete it (NtOpenFile 0x" + status.ToString("X8") + ")");
        }
        return new SafeFileHandle(handle, true);
    }

    // Delete the file at path, also when it is read-only or when its access list refuses the
    // admin every read. A file that another program holds open with no delete sharing is refused.
    public static void DeleteFile(string path) {
        using (SafeFileHandle handle = OpenForDelete(path, FILE_NON_DIRECTORY_FILE)) {
            uint flags = FILE_DISPOSITION_DELETE | FILE_DISPOSITION_POSIX_SEMANTICS
                | FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE;
            if (!SetFileInformationByHandle(handle, FileDispositionInfoEx, ref flags, 4)) {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
        }
    }

    // Rename the file or folder at path to destination (a full path on the same volume), also
    // when its access list refuses the admin every read.
    public static void RenameAside(string path, string destination, bool isDirectory) {
        uint kind = isDirectory ? FILE_DIRECTORY_FILE : FILE_NON_DIRECTORY_FILE;
        using (SafeFileHandle handle = OpenForDelete(path, kind)) {
            byte[] name = System.Text.Encoding.Unicode.GetBytes(@"\??\" + destination);
            // FILE_RENAME_INFO: flags (0), root directory (0), name length in bytes, then the name
            // and a NUL terminator. The whole buffer is zeroed first, so no driver that reads the
            // name as a C string past its length picks up uninitialized heap bytes.
            int size = 22 + name.Length;
            IntPtr info = Marshal.AllocHGlobal(size);
            try {
                for (int i = 0; i < size; i++) { Marshal.WriteByte(info, i, 0); }
                Marshal.WriteInt32(info, 16, name.Length);
                Marshal.Copy(name, 0, (IntPtr)(info.ToInt64() + 20), name.Length);
                if (!SetFileInformationByHandle(handle, FileRenameInfo, info, (uint)size)) {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
            } finally { Marshal.FreeHGlobal(info); }
        }
    }

    // A new file at path. With FILE_FLAG_OPEN_REPARSE_POINT a link at path is the name
    // itself: CREATE_NEW then fails, also for a link whose target does not exist.
    public static void WriteNewFile(string path, byte[] content) {
        SafeFileHandle handle = CreateFileW(path, GENERIC_WRITE, 0, IntPtr.Zero, CREATE_NEW,
            FILE_FLAG_OPEN_REPARSE_POINT, IntPtr.Zero);
        if (handle.IsInvalid) { throw new Win32Exception(Marshal.GetLastWin32Error()); }
        using (var stream = new System.IO.FileStream(handle, System.IO.FileAccess.Write)) {
            stream.Write(content, 0, content.Length);
        }
    }

    // The content of a plain file (not a link, not a folder, one name only), at most
    // maxBytes. Null for anything else. One handle: the checks and the read see one file.
    public static byte[] ReadPlainFile(string path, int maxBytes) {
        SafeFileHandle handle = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, IntPtr.Zero, OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT, IntPtr.Zero);
        if (handle.IsInvalid) { return null; }
        using (handle) {
            FileInfo info;
            if (!GetFileInformationByHandle(handle, out info)) { return null; }
            long size = ((long)info.SizeHigh << 32) | info.SizeLow;
            if ((info.Attributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)) != 0
                    || info.Links != 1 || size > maxBytes) {
                return null;
            }
            using (var stream = new System.IO.FileStream(handle, System.IO.FileAccess.Read)) {
                var content = new byte[size];
                int done = 0, count;
                while (done < content.Length && (count = stream.Read(content, done, content.Length - done)) > 0) {
                    done += count;
                }
                if (done != content.Length) { return null; }
                return content;
            }
        }
    }
}
"@

function Use-AdminTemp {
    # Add-Type compiles in TEMP, and the TEMP of the user is writable without admin rights:
    # a program could change the source or the DLL there before it loads. The elevated part
    # compiles in a new folder that only admins can write.
    if ($script:AdminTemp) { return }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null  # in Program Files: admins only
    $dir = Join-Path $InstallDir "setup-temp-$([guid]::NewGuid().ToString('N'))"
    New-AdminFolder $dir
    $env:TEMP = $dir
    $env:TMP = $dir
    $script:AdminTemp = $dir
}

function Remove-AdminTemp {
    if ($script:AdminTemp) {
        Remove-Item -LiteralPath $script:AdminTemp -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $script:AdminTemp) {
            Write-Warning "Cannot remove $($script:AdminTemp): delete it later (it holds only files of the setup)."
        }
        $script:AdminTemp = $null
    }
}

function Use-FolderGuardType {
    if (-not ('AlertColorsFolderGuard' -as [type])) {
        if ([IntPtr]::Size -ne 8) { throw 'AlertColors needs 64-bit PowerShell (the default on 64-bit Windows).' }
        if (Test-Admin) { Use-AdminTemp }
        Add-Type -TypeDefinition $FolderGuardSource
    }
}

function Open-FolderGuard([string]$Dir) {
    # Hold the returned handle until the last step that works on paths in $Dir.
    Use-FolderGuardType
    try {
        return [AlertColorsFolderGuard]::Open($Dir)
    } catch {
        throw "Cannot hold $Dir for the setup ($($_.Exception.InnerException.Message)). Another program can " +
              'hold it open: close the configurator and other programs. Or delete the folder. Then run ' +
              'setup.cmd again.'
    }
}

function Test-AdminOwner([string]$Dir) {
    return (Get-OwnerSid $Dir) -in @($SidAdministrators, $SidSystem)
}

function Test-TuningDirReady {
    # The checks that can refuse the folder, before the app is stopped: a refused folder
    # then stops the setup while the old app still runs. Initialize-TuningDir checks again.
    if (-not (Test-Path -LiteralPath $TuningDir)) { return }
    if (Test-ReparsePoint $TuningDir) {
        Remove-LinkAtName $TuningDir  # any account can put a link here: remove the link, not its target
        return
    }
    # A folder that Administrators do not own goes aside as it is: its content is not checked.
    if (-not (Test-AdminOwner $TuningDir)) { return }
    try { (Open-FolderGuard $TuningDir).Dispose() }
    catch {
        # Held open by another program (the lock of an older install let every account open
        # it): the app update goes on, and the tuning step reports the folder at the end.
        Write-Warning "$($_.Exception.Message)"
        return
    }
    Assert-NoLinks $TuningDir
}

function New-AdminFolder([string]$Dir) {
    # A new folder, locked and owned by Administrators from the start: no other account
    # can put a file or a link in it before the lock, and a policy that makes the admin
    # account the owner of new objects changes nothing. Fails if the folder exists.
    if (Test-Path -LiteralPath $Dir) { throw "$Dir exists already. Run setup.cmd again." }
    $acl = New-LockedSecurity
    $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier($SidAdministrators)))
    [IO.Directory]::CreateDirectory($Dir, $acl) | Out-Null
    if (-not (Test-AdminOwner $Dir)) {
        throw "$Dir was made by another program first. Delete it, then run setup.cmd again."
    }
}

function Write-NewFile([string]$Path, [byte[]]$Content) {
    # A new file, never through a link (also not a link to a file that does not exist yet).
    Use-FolderGuardType
    [AlertColorsFolderGuard]::WriteNewFile($Path, $Content)
    if (Test-Admin) {
        # Owned by Administrators, not by the admin account that approved the prompt: a policy
        # can make that account the owner of new objects, and a later setup checks owners.
        $acl = [IO.File]::GetAccessControl($Path)
        $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier($SidAdministrators)))
        [IO.File]::SetAccessControl($Path, $acl)
    }
}

function Get-OwnerSid([string]$Path) {
    # .NET, not Get-Acl: it needs no module. $null when the owner cannot be read: the owner of
    # an object can refuse even Administrators that read, so such an object counts as one of
    # another account.
    try {
        $acl = if ([IO.Directory]::Exists($Path)) { [IO.Directory]::GetAccessControl($Path) }
               else { [IO.File]::GetAccessControl($Path) }
        return $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    } catch { return $null }
}

function Get-TargetSid {
    # The account that the app is for (-InstallUser), as a SID.
    if (-not $script:TargetSid) {
        try {
            $script:TargetSid = ([Security.Principal.NTAccount]$TargetUser).Translate(
                [Security.Principal.SecurityIdentifier]).Value
        } catch {
            throw "Cannot find the account $TargetUser on this PC ($($_.Exception.Message))."
        }
    }
    return $script:TargetSid
}

function Move-ForeignTuningDir {
    # A folder of another account (for example made by the configurator before the first
    # install): its owner can always change its rights. So it goes aside, and a new folder
    # of this admin process, locked at once, takes its place with a copy of tuning.json.
    $null = Get-TargetSid  # an unknown account stops here, before any folder is made
    $id = [guid]::NewGuid().ToString('N')
    $fresh = "$TuningDir.new-$id"
    $aside = "$TuningDir.old-$id"
    New-AdminFolder $fresh
    Set-LockedRights $fresh
    $content = $null
    $old = Join-Path $TuningDir 'tuning.json'
    # Only the look of the installing user: another account can make this folder or this file
    # before the first install. Other accounts can add files to a folder of the user, but
    # cannot replace a file that the user owns. A folder of another account is not opened at
    # all (its owner can refuse the admin a read): it only goes aside.
    if ((Get-OwnerSid $TuningDir) -eq (Get-TargetSid)) {
        $oldGuard = Open-FolderGuard $TuningDir  # the read sees this folder, not a swapped one
        try {
            if ((Test-Path -LiteralPath $old) -and (Get-OwnerSid $old) -eq (Get-TargetSid)) {
                $content = [AlertColorsFolderGuard]::ReadPlainFile($old, $TuningMaxBytes)
            }
        } finally {
            $oldGuard.Dispose()  # the rename below needs the folder free
        }
    }
    if ($null -ne $content) { Write-NewFile (Join-Path $fresh 'tuning.json') $content }
    Use-FolderGuardType
    try {
        # The foreign folder may refuse the admin every read: move it aside through a handle that
        # has only the delete right (from the delete-child right of ProgramData).
        [AlertColorsFolderGuard]::RenameAside($TuningDir, $aside, $true)
        Rename-Item -LiteralPath $fresh -NewName (Split-Path $TuningDir -Leaf) -ErrorAction Stop
    } catch {
        Remove-Item -LiteralPath $fresh -Recurse -Force -ErrorAction SilentlyContinue  # the folder we made
        throw "Cannot replace $TuningDir, which is not owned by Administrators ($($_.Exception.Message)). " +
              'Close the configurator and other programs, or delete the folder, then run setup.cmd again.'
    }
    if ($null -ne $content) {
        Write-Output "Tuning folder not owned by Administrators moved to $aside. The new folder has its tuning.json"
    } else {
        Write-Output ("Tuning folder not owned by Administrators moved to $aside. Its tuning.json was not copied " +
                      '(missing, of another account, open, a link, or too large): save the tuning again in ' +
                      'the configurator')
    }
}

function Initialize-TuningDir {
    # The rights below are set with admin rights: the folder must be a real
    # folder, owned by Administrators, with no links in it. Order:
    # 1. Check, then lock the folder: users can no longer write it, and it has no subfolder
    #    whose owner could give itself rights again.
    # 2. Check again, then give the installing user write rights.
    if (Test-ReparsePoint $TuningDir) {
        Remove-LinkAtName $TuningDir  # any account can put a link here: remove the link, not its target
    }
    if (-not (Test-Path -LiteralPath $TuningDir)) {
        New-AdminFolder $TuningDir  # fails if another program makes it first: run the setup again
    }
    if (-not (Test-AdminOwner $TuningDir)) { Move-ForeignTuningDir }
    $guard = Open-FolderGuard $TuningDir  # from now on, $TuningDir stays this real folder
    $locked, $linksChecked, $granted = $false, $false, $false
    try {
    Assert-AdminOwner $TuningDir
    Assert-NoLinks $TuningDir
    icacls $TuningDir /L /setowner "*$SidAdministrators" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls /setowner failed on $TuningDir" }
    Set-LockedRights $TuningDir
    $locked = $true
    Assert-NoLinks $TuningDir
    $linksChecked = $true
    Replace-Files $TuningDir
    $user = $TargetUser
    if (-not (Grant-UserWrite $TuningDir $user)) { throw "icacls failed to give $user write rights on $TuningDir" }
    $granted = $true
    Write-Output "Tuning folder: $TuningDir (writable by $user)"
    } finally {
        if ($locked -and $linksChecked -and -not $granted) {
            # A later step failed: the configurator must still be able to save. Not after a
            # failed link check: the right would also reach the file at the other end.
            Grant-UserWrite $TuningDir $TargetUser | Out-Null
        }
        $guard.Dispose()
    }
}

function Remove-InstallDir {
    if (Test-Path -LiteralPath $TuningDir) {
        try {
            if (Test-ReparsePoint $TuningDir) {
                # Any account can put a link at this name: remove the link, never its target.
                [IO.Directory]::Delete($TuningDir)
                Write-Output "Removed the link $TuningDir"
            } elseif (-not (Test-AdminOwner $TuningDir)) {
                Write-Warning "$TuningDir does not belong to Administrators: not removed. Delete it by hand."
            } else {
                # A real folder of Administrators: users cannot rename it, so these deletes stay
                # in it. Only files are in it (the setup refuses subfolders and links).
                foreach ($file in @(Get-ChildItem -LiteralPath $TuningDir -Force -File)) { Remove-PlainFile $file.FullName }
                [IO.Directory]::Delete($TuningDir)
                Write-Output "Removed $TuningDir"
            }
        } catch {
            Write-Warning "Cannot remove $TuningDir ($($_.Exception.Message)): delete it by hand."
        }
    }
    # The install folder last: the delete of a tuning file can compile the guard in it.
    if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force }
    Write-Output "Removed $InstallDir"
    # The copies that an install left (a folder of another account moved aside, or a new
    # folder that was not renamed). Never a link: only real folders of this name.
    $parent = Split-Path -Parent $TuningDir
    $leaf = Split-Path -Leaf $TuningDir
    foreach ($copy in @(Get-ChildItem -LiteralPath $parent -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "$leaf.old-*" -or $_.Name -like "$leaf.new-*" })) {
        if ($copy.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        try {
            Remove-Item -LiteralPath $copy.FullName -Recurse -Force -ErrorAction Stop
            Write-Output "Removed $($copy.FullName)"
        } catch {
            Write-Warning "Cannot remove $($copy.FullName) ($($_.Exception.Message)): delete it by hand."
        }
    }
}

# ---------------------------------------------- elevated: Armoury Crate ----
# ProcessList.ini is UTF-16 LE with a BOM. Each "<exe>=0" line is an app in the
# Armoury Crate "Aura In-Game Priority" list.
function Test-TrustedProcessList {
    # Any account can make C:\ProgramData\ASUS on a PC without Armoury Crate, and put a link
    # in it. The admin part reads and writes only a file of Armoury Crate: not a link, one
    # name, and the folder and the file owned by Administrators, SYSTEM or TrustedInstaller.
    $trusted = @($SidAdministrators, $SidSystem, $SidTrustedInstaller)
    $folder = Split-Path -Parent $ProcessList
    if ((Test-ReparsePoint $folder) -or (Test-ReparsePoint $ProcessList)) { return $false }
    return ((Get-OwnerSid $folder) -in $trusted) -and ((Get-OwnerSid $ProcessList) -in $trusted)
}

function Read-ProcessList {
    Use-FolderGuardType
    $bytes = [AlertColorsFolderGuard]::ReadPlainFile($ProcessList, $ProcessListMaxBytes)
    if ($null -eq $bytes) {
        throw "$ProcessList cannot be read now (open in another program, a link, more names or too large)"
    }
    $text = [Text.Encoding]::Unicode.GetString($bytes).TrimStart([char]0xFEFF)
    $lines = @($text -split "`r`n|`n|`r")
    if ($lines.Count -gt 0 -and $lines[-1] -eq '') { $lines = @($lines | Select-Object -SkipLast 1) }  # as ReadAllLines
    return $lines
}

function Write-ProcessList([string[]]$Lines) {
    if (-not (Test-Path -LiteralPath $ProcessListBackup)) {
        # Users can create files in this ASUS folder: never follow a name that came first.
        try { Write-NewFile $ProcessListBackup ([IO.File]::ReadAllBytes($ProcessList)) }
        catch { Write-Warning "No backup of $ProcessList ($($_.Exception.Message))." }
    } elseif ((Get-OwnerSid $ProcessListBackup) -notin @($SidAdministrators, $SidSystem)) {
        Write-Warning "$ProcessListBackup is a file of another account, not a backup: no backup is made."
    }
    [IO.File]::WriteAllLines($ProcessList, $Lines, [Text.UnicodeEncoding]::new($false, $true))
}

function Test-EntryOf([string]$Line, [string]$Exe) {
    return ($Line -split '=', 2)[0].Trim() -ieq $Exe
}

function Update-ArmouryCrateEntry([bool]$Present) {
    # Another account can hold ProcessList.ini open: a warning, never a stopped install or
    # uninstall (the app is registered and started anyway).
    try { Set-ArmouryCrateEntry $Present }
    catch { Write-Warning "Armoury Crate entry not changed: $($_.Exception.Message). Run the setup again later." }
}

function Set-ArmouryCrateEntry([bool]$Present) {
    if (-not (Test-Path $ProcessList)) { Write-Warning "$ProcessList not found: is Armoury Crate installed?"; return }
    if (-not (Test-TrustedProcessList)) {
        Write-Warning "$ProcessList is not a file of Armoury Crate (a link, or another owner): its entry is not changed."
        return
    }
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
    # A standard user gets the task too: Highest then runs with that user's rights.
    $user = $TargetUser
    $runLevel = if ($NeedsAdmin) { 'Highest' } else { 'Limited' }
    $action = New-ScheduledTaskAction -Execute $InstallExe -Argument "--config `"$InstallConfig`" run" -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    # Highest only when a module needs it (aura_sdk: motherboard and ARGB headers).
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel $runLevel
    # No time limit: at the limit, Task Scheduler kills the process. Restart after a crash.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
        -RestartCount $RestartCount -RestartInterval (New-TimeSpan -Minutes $RestartIntervalMinutes)
    # Task Scheduler starts a task at priority 7 (below normal) by default. A below-normal process
    # is denied CPU under normal desktop load. Then its threads freeze for seconds, the heartbeat to
    # the Razer SDK is late, and the session drops. Priority 5 is the normal process priority class.
    $NormalProcessPriority = 5
    $settings.Priority = $NormalProcessPriority
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
    # Only shown, never written: a plain read (no guard, no temp folder for its compile).
    $entry = 'not registered'
    try {
        if ((Test-Path -LiteralPath $ProcessList -PathType Leaf) -and
                (Get-Item -LiteralPath $ProcessList -Force).Length -le $ProcessListMaxBytes -and
                (@([IO.File]::ReadAllLines($ProcessList, [Text.Encoding]::Unicode)) | Where-Object { Test-EntryOf $_ $InstallExe })) {
            $entry = 'registered'
        }
    } catch { $entry = "unknown ($($_.Exception.Message))" }
    Write-Output "Armoury Crate entry: $entry"
    Write-Output "Installed: $(if (Test-Path $InstallExe) { $InstallDir } else { 'no' })"
    $installedManifest = Join-Path $InstallApp 'build-manifest.json'
    if (Test-Path $installedManifest) {
        Write-Output (Format-Manifest (Get-Content $installedManifest -Raw | ConvertFrom-Json))
    }
    if (Test-Path $InstallLog) { Write-Output '--- log'; Get-Content $InstallLog -Tail $LogLines }
}

# ----------------------------------------------------------------- main ----
function Invoke-TuningStep {
    # The app needs no tuning folder: without a tuning file it uses the look of the
    # config. So a failure here does not keep the app stopped: the setup finishes,
    # then reports it.
    # Out-Host: the messages of the step go to the window, not into the return value.
    try { Initialize-TuningDir | Out-Host; return $null } catch { return $_ }
}

function Install-App {
    $manifest = Get-Manifest
    Test-TuningDirReady
    Stop-App; Copy-App; Copy-Config
    $tuningError = Invoke-TuningStep
    Update-ArmouryCrateEntry ([bool]$manifest.needs_armoury_crate)
    Register-Task ([bool]$manifest.needs_admin)
    Start-App
    if ($tuningError) { throw $tuningError }
}

function Invoke-KeepingAppUp([scriptblock]$Steps) {
    # The part without admin rights stops the app for the config check. If a step here
    # fails, the installed app (old or new) starts again: a failed setup never leaves
    # the LEDs without the app.
    try { & $Steps }
    catch {
        if ((Test-Path -LiteralPath $InstallExe) -and -not (Get-App) -and
                (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
            try { Start-App } catch { Write-Output "Cannot start the app again: $($_.Exception.Message)" }
        }
        throw
    }
}

function Invoke-AdminPart([string]$Name) {
    switch ($Name) {
        'install' { Invoke-KeepingAppUp { Install-App } }
        'update' {
            # The modules can change between builds: update the entry and the task too.
            Invoke-KeepingAppUp { Install-App }
        }
        'restart' { Invoke-KeepingAppUp { Stop-App; Copy-Config; Start-App } }
        'uninstall' {
            Stop-App
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
            Write-Output "Removed task $TaskName"
            Update-ArmouryCrateEntry $false
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
    if (-not $Elevated -and $BuildActions -contains $Action) { Invoke-Build }
    # The first install proposes the place. After `setup.cmd region`, the config of this PC
    # exists, and the install does not ask again.
    if (-not $Elevated -and $Action -eq 'install' -and -not (Test-Path $MachineConfig)) { [void](Select-Place) }
    if ($ConfigActions -contains $Action) { Write-Output "Config: $SourceConfig" }  # after Select-Place settled it
    if (-not $Elevated -and $ConfigActions -contains $Action) { Test-Config }
    if ($AdminActions -contains $Action) {
        if ($Elevated -and -not (Test-Admin)) {
            # With UAC turned off, "Run as administrator" gives no admin rights: never ask again in a loop.
            # The part without admin rights stopped the app for the check: start it again.
            if ((Test-Path -LiteralPath $InstallExe) -and -not (Get-App)) { Start-ScheduledTask -TaskName $TaskName }
            throw 'The admin part did not get admin rights. The setup needs UAC turned on (the default of Windows).'
        }
        if (Test-Admin) {
            try { Invoke-AdminPart $Action } finally { Remove-AdminTemp }
        }
        else {
            $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
                '-Action', $Action, '-Elevated', '-WaitAtEnd', '-StopTimeoutSeconds', $StopTimeoutSeconds,
                '-InstallUser', "`"$TargetUser`"", '-InstallConfigSource', "`"$SourceConfig`"",
                '-RestartCount', $RestartCount, '-RestartIntervalMinutes', $RestartIntervalMinutes)
            try {
                Start-Process (Join-Path $PSHOME 'powershell.exe') -Verb RunAs -ArgumentList $arguments
            } catch {
                Restore-StoppedApp  # for example, the UAC prompt was canceled
                throw
            }
            Write-Output 'The admin part continues in an elevated window.'
        }
    }
}
catch {
    Write-Output "ERROR: $($_.Exception.Message)"
    $failed = $true
    if (-not $Elevated) {  # the part without admin rights stopped the app for the check
        try { Restore-StoppedApp } catch { Write-Output "Cannot start the old app again: $($_.Exception.Message)" }
    }
}
finally {
    if ($interactive -or $WaitAtEnd) { Read-Host 'Press Enter to close' | Out-Null }
}
if ($failed) { exit 1 }

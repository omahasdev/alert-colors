"""The setup flow around the tuning folder: an early check before the app stops, the app
starts also after a failed tuning step, the write right after a failed step, and a new
folder for a folder of another owner."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from alert_colors.tuning import MAX_FILE_BYTES

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "manage.ps1").read_text(encoding="utf-8")


def ps_function(name):
    start = SCRIPT.index(f"function {name}")
    return SCRIPT[start:SCRIPT.index("\n}", start) + 2]


def uninstall_code(install, tuning_dir, *extra, admin_owned="$true"):
    """Remove-InstallDir with what it needs. The test account stands in for Administrators as
    the owner of the tuning folder (only an admin can make that owner)."""
    return "\n".join([
        "$ErrorActionPreference = 'Stop'", f"$InstallDir = '{install}'", f"$TuningDir = '{tuning_dir}'",
        guard_code(), f"function Test-AdminOwner([string]$Dir) {{ {admin_owned} }}",
        ps_function("Test-ReparsePoint"), ps_function("Remove-PlainFile"), *extra,
        ps_function("Remove-InstallDir"), "Remove-InstallDir | Out-Null", "'uninstall done'",
    ])


def owner_code(target="[Security.Principal.WindowsIdentity]::GetCurrent().Name"):
    """The owner helpers, with the installing user (by default this test account)."""
    return "\n".join([f"$TargetUser = {target}", ps_function("Get-OwnerSid"), ps_function("Get-TargetSid")])


def guard_code():
    start = SCRIPT.index('$FolderGuardSource = @"')
    return "\n".join([SCRIPT[start:SCRIPT.index('"@', start) + 2], "function Test-Admin { $false }",
                      ps_function("Use-FolderGuardType"), ps_function("Open-FolderGuard")])


def powershell(code):
    """Stop at the first error, as the script does: a missing helper fails the test."""
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                           "$ErrorActionPreference = 'Stop'\n" + code],
                          capture_output=True, text=True, check=False)


def function_body(name):
    body = SCRIPT[SCRIPT.index(f"function {name}"):]
    return body[:body.index("\n}\n")]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_folder_of_another_account_is_replaced_and_keeps_its_tuning(tmp_path):
    """The real Move-ForeignTuningDir. Stubs: only an admin can make a folder that
    Administrators own, and lock it."""
    tuning_dir = tmp_path / "AlertColors"
    tuning_dir.mkdir()
    (tuning_dir / "tuning.json").write_text('{"display": {"brightness": 0.4}}', encoding="utf-8")
    code = "\n".join([
        f"$TuningDir = '{tuning_dir}'", f"$TuningMaxBytes = {MAX_FILE_BYTES}",
        "function New-AdminFolder([string]$Dir) {",
        "    New-Item -ItemType Directory -Path $Dir -ErrorAction Stop | Out-Null",
        "}",
        "function Set-LockedRights([string]$Dir) { }",
        guard_code(), ps_function("Write-NewFile"), owner_code(), ps_function("Move-ForeignTuningDir"),
        "Move-ForeignTuningDir | Out-Null",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert (tuning_dir / "tuning.json").read_text(encoding="utf-8") == '{"display": {"brightness": 0.4}}'
    names = [p.name for p in tmp_path.iterdir()]
    assert sum(name.startswith("AlertColors.old-") for name in names) == 1  # the old folder is kept
    assert not [name for name in names if name.startswith("AlertColors.new-")]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
@pytest.mark.parametrize(("make", "ok"), [("missing", True), ("plain", True), ("subfolder", False)])
def test_the_early_check_refuses_a_bad_folder(tmp_path, make, ok):
    tuning_dir = tmp_path / "AlertColors"
    if make != "missing":
        tuning_dir.mkdir()
        (tuning_dir / "tuning.json").write_text("{}", encoding="utf-8")
    if make == "subfolder":
        (tuning_dir / "sub").mkdir()
    code = "\n".join([
        f"$TuningDir = '{tuning_dir}'",
        "function Test-AdminOwner([string]$Dir) { $true }",  # as a folder of Administrators
        "\n".join(line for line in SCRIPT.splitlines() if line.startswith("$Sid") and " = '" in line),
        owner_code(),  # the files belong to this test account, the installing user
        guard_code(), ps_function("Test-ReparsePoint"), ps_function("Assert-NoLinks"),
        ps_function("Test-TuningDirReady"),
        "Test-TuningDirReady",
    ])
    result = powershell(code)
    assert (result.returncode == 0) == ok, result.stderr


def test_install_and_update_check_the_folder_first_and_start_the_app_after_a_failed_tuning_step():
    part = ps_function("Install-App")
    steps = ["Test-TuningDirReady", "Stop-App", "$tuningError = Invoke-TuningStep", "Register-Task", "Start-App",
             "if ($tuningError) { throw $tuningError }"]
    places = [part.index(step) for step in steps]
    assert places == sorted(places)
    body = function_body("Invoke-AdminPart")
    for action in ("'install' {", "'update' {"):
        branch = body[body.index(action):]
        assert branch.index("Invoke-KeepingAppUp { Install-App }") < branch.index("\n        '")
    assert "'restart' { Invoke-KeepingAppUp {" in body


def keep_up_code(tmp_path, steps):
    exe = tmp_path / "AlertColors.exe"
    exe.write_text("", encoding="ascii")
    return "\n".join([
        f"$InstallExe = '{exe}'", "$TaskName = 'AlertColors'", "$script:started = 0",
        "function Get-App { $null }",
        "function Get-ScheduledTask { 'the task' }",
        "function Start-App { $script:started += 1 }",
        ps_function("Invoke-KeepingAppUp"),
        f"try {{ Invoke-KeepingAppUp {{ {steps} }} }} catch {{ \"error: $($_.Exception.Message)\" }}",
        '"started $script:started"',
    ])


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_failed_admin_step_starts_the_installed_app_again_and_reports_the_error(tmp_path):
    result = powershell(keep_up_code(tmp_path, "throw 'the copy failed'"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["error: the copy failed", "started 1"]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_admin_steps_that_succeed_start_nothing_more(tmp_path):
    result = powershell(keep_up_code(tmp_path, "'done'"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["done", "started 0"]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
@pytest.mark.parametrize(("stopped", "started"), [("$true", "1"), ("$false", "0")])
def test_a_canceled_uac_prompt_starts_the_app_that_the_check_stopped(stopped, started):
    code = "\n".join([
        f"$script:StoppedForCheck = {stopped}", "$TaskName = 'AlertColors'", "$script:started = 0",
        "function Get-App { $null }",
        "function Start-ScheduledTask([string]$TaskName) { $script:started += 1 }",
        ps_function("Restore-StoppedApp"),
        "Restore-StoppedApp | Out-Null",
        '"started $script:started"',
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"started {started}"


def test_the_admin_window_that_does_not_start_restores_the_app():
    start = SCRIPT.index("Start-Process (Join-Path $PSHOME 'powershell.exe') -Verb RunAs")
    handler = SCRIPT[start:SCRIPT.index("Write-Output 'The admin part continues", start)]
    assert "} catch {" in handler and "Restore-StoppedApp" in handler and "throw" in handler
    check = ps_function("Test-Config")
    assert check.index("$wasRunning = Stop-AppAsUser") < check.index("$script:StoppedForCheck = $wasRunning")


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_failed_tuning_step_is_returned_not_thrown():
    code = "\n".join([
        "function Initialize-TuningDir { throw 'the folder is held' }",
        ps_function("Invoke-TuningStep"),
        "$e = Invoke-TuningStep",
        "'after the step'",
        '"$e"',
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[:2] == ["after the step", "the folder is held"]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_tuning_step_that_writes_messages_and_succeeds_gives_no_error():
    """The messages of the step show in the window, and the setup does not take them for an error."""
    code = "\n".join([
        "function Initialize-TuningDir { Write-Output 'Tuning folder: X (writable by U)' }",
        ps_function("Invoke-TuningStep"),
        "$e = Invoke-TuningStep",
        "if ($e) { 'an error' } else { 'no error' }",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["Tuning folder: X (writable by U)", "no error"]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_tuning_json_that_is_not_copied_is_named_in_the_message(tmp_path):
    tuning_dir = tmp_path / "AlertColors"
    tuning_dir.mkdir()
    (tuning_dir / "tuning.json").write_text("{}", encoding="utf-8")
    os.link(tuning_dir / "tuning.json", tmp_path / "second-name.json")  # a file with two names is not copied
    code = "\n".join([
        f"$TuningDir = '{tuning_dir}'", f"$TuningMaxBytes = {MAX_FILE_BYTES}",
        "function New-AdminFolder([string]$Dir) {",
        "    New-Item -ItemType Directory -Path $Dir -ErrorAction Stop | Out-Null",
        "}",
        "function Set-LockedRights([string]$Dir) { }",
        guard_code(), ps_function("Write-NewFile"), owner_code(), ps_function("Move-ForeignTuningDir"),
        "Move-ForeignTuningDir",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert "was not copied" in result.stdout
    assert not (tuning_dir / "tuning.json").exists()


def test_the_write_right_comes_back_after_a_failed_step_only_when_the_links_were_checked():
    body = function_body("Initialize-TuningDir")
    final = body[body.index("} finally {"):]
    assert "if ($locked -and $linksChecked -and -not $granted)" in final
    assert 'Grant-UserWrite $TuningDir $TargetUser' in final
    lock, checked = body.index("$locked = $true"), body.index("$linksChecked = $true")
    assert body.index("Set-LockedRights $TuningDir") < lock < body.index("Assert-NoLinks $TuningDir", lock) < checked


def test_a_folder_of_another_account_is_replaced_before_the_guard():
    body = function_body("Initialize-TuningDir")
    assert body.index("Move-ForeignTuningDir") < body.index("$guard = Open-FolderGuard $TuningDir") < \
        body.index("Assert-AdminOwner $TuningDir")


# ---- the copy of tuning.json from a folder of another account ---------------------------------
def read_plain(path, max_bytes=MAX_FILE_BYTES):
    code = "\n".join([
        guard_code(),
        "if (-not ('AlertColorsFolderGuard' -as [type])) { Add-Type -TypeDefinition $FolderGuardSource }",
        f"$c = [AlertColorsFolderGuard]::ReadPlainFile('{path}', {max_bytes})",
        "if ($null -eq $c) { 'none' } else { [Text.Encoding]::UTF8.GetString($c) }",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows files")
def test_only_a_plain_small_file_is_copied(tmp_path):
    plain = tmp_path / "plain.json"
    plain.write_text('{"a": 1}', encoding="utf-8")
    assert read_plain(plain) == '{"a": 1}'
    assert read_plain(plain, max_bytes=3) == "none"  # larger than the limit of the app
    assert read_plain(tmp_path) == "none"  # a folder
    linked = tmp_path / "linked.json"
    linked.write_text("{}", encoding="utf-8")
    os.link(linked, tmp_path / "second-name.json")
    assert read_plain(linked) == "none"  # a file with a second name can be another file of the PC
    assert read_plain(tmp_path / "missing.json") == "none"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows links")
def test_a_symbolic_link_is_not_copied(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"a": 1}', encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this account cannot make symbolic links")
    assert read_plain(link) == "none"


def test_the_size_limit_of_the_setup_is_the_one_of_the_app():
    assert f"$TuningMaxBytes = {MAX_FILE_BYTES} " in SCRIPT


def test_a_folder_that_the_setup_makes_is_locked_and_owned_by_administrators_from_the_start():
    function = ps_function("New-AdminFolder")
    # The owner and the lock are part of the creation: no moment with the rights of ProgramData.
    assert function.index("$acl = New-LockedSecurity") < function.index("$acl.SetOwner(") < \
        function.index("[IO.Directory]::CreateDirectory($Dir, $acl)") < function.index("Test-AdminOwner $Dir")
    assert "New-AdminFolder $TuningDir" in function_body("Initialize-TuningDir")
    assert "New-AdminFolder $fresh" in ps_function("Move-ForeignTuningDir")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rights")
def test_a_folder_that_the_setup_makes_has_no_inherited_or_write_rules(tmp_path):
    """The real New-AdminFolder. Only an admin can make Administrators the owner, so the test
    account stands in for Administrators here."""
    # A parent that lets everyone write in its subfolders, as ProgramData does for Users.
    subprocess.run(["icacls", str(tmp_path), "/grant", "*S-1-1-0:(OI)(CI)M"], capture_output=True, check=True)
    folder = tmp_path / "AlertColors"
    sids = "\n".join(line for line in SCRIPT.splitlines() if line.startswith("$Sid") and " = '" in line)
    code = "\n".join([
        "$ErrorActionPreference = 'Stop'", sids,
        "$SidAdministrators = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
        "function Test-AdminOwner([string]$Dir) { $true }",
        ps_function("New-LockedSecurity"), ps_function("New-AdminFolder"),
        f"New-AdminFolder '{folder}'",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    rights = subprocess.run(["icacls", str(folder)], capture_output=True, text=True, check=False).stdout
    assert "(I)" not in rights  # nothing from the parent
    assert "(M)" not in rights and "(W)" not in rights and "Everyone" not in rights
    again = powershell(code)
    assert again.returncode != 0 and "exists already" in again.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows files")
def test_a_new_file_is_never_written_through_a_name_that_exists(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("old", encoding="utf-8")
    names = [tmp_path / "exists.txt"]
    names[0].write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    dangling = tmp_path / "dangling.txt"  # a link to a file that does not exist yet
    try:
        link.symlink_to(target)
        dangling.symlink_to(tmp_path / "not-yet.txt")
        names += [link, dangling]
    except OSError:
        pass  # this account cannot make symbolic links: the plain name is still tested
    for name in names:
        code = "\n".join(["$ErrorActionPreference = 'Stop'", guard_code(), ps_function("Write-NewFile"),
                          f"Write-NewFile '{name}' ([byte[]](65, 66))"])
        assert powershell(code).returncode != 0
    assert target.read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "not-yet.txt").exists()  # never created through the link
    fresh = tmp_path / "fresh.txt"
    code = "\n".join(["$ErrorActionPreference = 'Stop'", guard_code(), ps_function("Write-NewFile"),
                      f"Write-NewFile '{fresh}' ([byte[]](65, 66))"])
    assert powershell(code).returncode == 0
    assert fresh.read_text(encoding="utf-8") == "AB"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handles")
def test_the_early_check_warns_about_a_folder_that_another_program_holds(tmp_path):
    """The lock of an older install let every account open the folder: the app update goes on,
    and the tuning step reports the folder at the end."""
    import ctypes
    from ctypes import wintypes

    folder = tmp_path / "AlertColors"
    folder.mkdir()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    delete, share_all, open_existing, backup_semantics = 0x00010000, 0x7, 3, 0x02000000
    handle = kernel32.CreateFileW(str(folder), delete, share_all, None, open_existing, backup_semantics, None)
    assert handle not in (None, wintypes.HANDLE(-1).value)
    code = "\n".join([f"$TuningDir = '{folder}'", "function Test-AdminOwner([string]$Dir) { $true }",
                      guard_code(), ps_function("Test-ReparsePoint"), ps_function("Assert-NoLinks"),
                      ps_function("Test-TuningDirReady"), "Test-TuningDirReady"])
    try:
        result = powershell(code)
    finally:
        kernel32.CloseHandle(handle)
    assert result.returncode == 0, result.stderr
    assert "Cannot hold" in " ".join((result.stdout + result.stderr).split())


def test_the_script_uses_only_the_modules_of_windows_powershell():
    """A module folder of the user first in PSModulePath could replace a cmdlet of the admin part."""
    first_cmdlet = SCRIPT.index("$TargetUser = ")
    assert SCRIPT.index("$env:PSModulePath = Join-Path $PSHOME 'Modules'") < first_cmdlet
    assert "Start-Process (Join-Path $PSHOME 'powershell.exe') -Verb RunAs" in SCRIPT


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_py_launcher_without_the_wanted_python_does_not_stop_the_search(tmp_path):
    """In PowerShell 5.1 the launcher's error line is a terminating error under Stop."""
    fake = tmp_path / "py.cmd"
    fake.write_text("@echo No suitable Python runtime found 1>&2\r\n@exit /b 103\r\n", encoding="ascii")
    code = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$env:PATH = '{tmp_path};' + $env:PATH",
        "$Python = ''", "$WantedPython = '9.99'",
        ps_function("Get-PythonVersion"), ps_function("Find-Python"),
        "$found = Find-Python",
        "'searched on'",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("searched on")


# ---- the config check and the stop for it ----------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_failed_config_check_starts_the_old_app_again(tmp_path):
    failing = tmp_path / "python.cmd"
    failing.write_text("@exit /b 1\r\n", encoding="ascii")
    code = "\n".join([
        f"$VenvPython = '{failing}'", f"$Root = '{tmp_path}'", "$SourceConfig = 'config.toml'",
        "$TaskName = 'AlertColors'", "$script:started = 0",
        "function Assert-NoAdminPython([string]$What) { }",
        "function Stop-AppAsUser { $true }",
        "function Start-ScheduledTask([string]$TaskName) { $script:started += 1 }",
        ps_function("Test-Config"),
        "try { Test-Config | Out-Null } catch { \"error: $($_.Exception.Message)\" }",
        '"started $script:started"',
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[-1] == "started 1" and lines[-2].startswith("error: config.toml failed the check")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows events")
def test_the_stop_for_the_check_returns_only_true_and_shows_its_message():
    """The message goes to the window: the return value is a plain $true, not an array."""
    name = rf"Local\AlertColors.Test.{os.getpid()}"
    code = "\n".join([
        f"$StopEventName = '{name}'", "$StopTimeoutSeconds = 5", "$InstallLog = 'log'",
        "$stopEvent = New-Object System.Threading.EventWaitHandle($false, 'ManualReset', $StopEventName)",
        "$script:running = $true",
        "function Get-App { if ($script:running) { $script:running = $stopEvent.WaitOne(0) -eq $false; 'app' } }",
        ps_function("Stop-AppAsUser"),
        "$result = Stop-AppAsUser",
        '"result: $($result.GetType().Name) $result"',
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "result: Boolean True"
    assert "Stopped AlertColors for the check" in result.stdout  # shown in the window


@pytest.mark.skipif(sys.platform != "win32", reason="Windows links")
def test_a_tuning_file_that_became_a_link_is_never_read_by_the_setup(tmp_path):
    folder = tmp_path / "tuning"
    folder.mkdir()
    secret = tmp_path / "admin-only.txt"
    secret.write_text("not for the tuning folder", encoding="utf-8")
    try:
        (folder / "tuning.json").symlink_to(secret)
    except OSError:
        pytest.skip("this account cannot make symbolic links")
    size = next(line for line in SCRIPT.splitlines() if line.startswith("$TuningMaxBytes = "))
    sids = "\n".join(line for line in SCRIPT.splitlines() if line.startswith("$Sid") and " = '" in line)
    code = "\n".join([size, sids, guard_code(), "Add-Type -TypeDefinition $FolderGuardSource",
                      owner_code(), ps_function("Write-NewFile"), ps_function("Remove-PlainFile"),
                      ps_function("Replace-Files"),
                      f"Replace-Files '{folder}'"])
    result = powershell(code)
    assert result.returncode != 0 and "not a plain file" in result.stderr
    copies = [p for p in folder.iterdir() if p.is_file() and not p.is_symlink()]
    assert all("not for the tuning folder" not in p.read_text(encoding="utf-8") for p in copies)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_the_elevated_part_compiles_in_a_temp_folder_of_admins_and_removes_it(tmp_path):
    install = tmp_path / "AlertColors"
    code = "\n".join([
        f"$InstallDir = '{install}'", "$script:AdminTemp = $null",
        "function New-AdminFolder([string]$Dir) { New-Item -ItemType Directory -Path $Dir | Out-Null }",
        ps_function("Use-AdminTemp"), ps_function("Remove-AdminTemp"),
        "Use-AdminTemp",
        "$made = $script:AdminTemp",
        '"temp: $($env:TEMP -eq $made) $($env:TMP -eq $made) $(Test-Path $made)"',
        "Remove-AdminTemp",
        '"after: $(Test-Path $made)"',
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-2:] == ["temp: True True True", "after: False"]
    guard = ps_function("Use-FolderGuardType")
    assert guard.index("if (Test-Admin) { Use-AdminTemp }") < guard.index("Add-Type -TypeDefinition")
    assert "Use-FolderGuardType" in ps_function("Open-FolderGuard")
    assert "try { Invoke-AdminPart $Action } finally { Remove-AdminTemp }" in SCRIPT
    assert 'Join-Path $InstallDir "setup-temp-' in ps_function("Use-AdminTemp")  # in Program Files


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_failed_restart_after_a_failed_check_still_names_the_check(tmp_path):
    failing = tmp_path / "python.cmd"
    failing.write_text("@exit /b 1\r\n", encoding="ascii")
    code = "\n".join([
        f"$VenvPython = '{failing}'", f"$Root = '{tmp_path}'", "$SourceConfig = 'config.toml'",
        "$TaskName = 'AlertColors'",
        "function Assert-NoAdminPython([string]$What) { }",
        "function Stop-AppAsUser { $true }",
        "function Start-ScheduledTask([string]$TaskName) { throw 'the task is missing' }",
        ps_function("Test-Config"),
        "try { Test-Config | Out-Null } catch { \"error: $($_.Exception.Message)\" }",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    last = result.stdout.splitlines()[-1]
    assert "failed the check" in last and "did not start again" in last and "the task is missing" in last


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_the_uninstall_removes_the_old_tuning_copies_but_no_link(tmp_path):
    tuning_dir = tmp_path / "AlertColors"
    tuning_dir.mkdir()
    old_copy = tmp_path / "AlertColors.old-1"
    old_copy.mkdir()
    (old_copy / "tuning.json").write_text("{}", encoding="utf-8")
    (tmp_path / "AlertColors.new-2").mkdir()
    keep = tmp_path / "elsewhere"
    keep.mkdir()
    (keep / "file.txt").write_text("keep", encoding="utf-8")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "AlertColors.old-3"), str(keep)],
                          capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    other = tmp_path / "Other.old-4"
    other.mkdir()
    result = powershell(uninstall_code(tmp_path / "not-installed", tuning_dir))
    assert result.returncode == 0, result.stderr
    assert not tuning_dir.exists() and not old_copy.exists() and not (tmp_path / "AlertColors.new-2").exists()
    assert (keep / "file.txt").read_text(encoding="utf-8") == "keep"  # the junction was not followed
    assert (tmp_path / "AlertColors.old-3").exists() and other.exists()


# ---- the elevated copies ---------------------------------------------------------------
def copy_config_code(source, install):
    return "\n".join([
        "$ErrorActionPreference = 'Stop'", f"$SourceConfig = '{source}'", f"$InstallDir = '{install}'",
        f"$InstallConfig = '{install / 'config.toml'}'", ps_function("Copy-Config"), "Copy-Config",
    ])


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_the_config_is_copied_to_the_install_folder(tmp_path):
    source = tmp_path / "config.toml"
    source.write_text("[source]\n", encoding="utf-8")
    result = powershell(copy_config_code(source, tmp_path / "install"))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "install" / "config.toml").read_text(encoding="utf-8") == "[source]\n"


def test_the_app_copy_is_told_to_skip_junctions_inside_the_build():
    assert " /MIR /XJ " in ps_function("Copy-App")


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_copy_that_cannot_be_removed_does_not_stop_the_uninstall(tmp_path):
    tuning_dir = tmp_path / "AlertColors"
    for name in ("AlertColors.old-1", "AlertColors.old-2"):
        (tmp_path / name).mkdir()
    protected = "\n".join([  # as a folder that another account protects against a delete by admins
        "function Remove-Item { param([string]$LiteralPath, [switch]$Recurse, [switch]$Force, $ErrorAction)",
        "  if ($LiteralPath -like '*old-1') { throw 'Access is denied' }",
        "  Microsoft.PowerShell.Management\\Remove-Item -LiteralPath $LiteralPath -Recurse -Force }",
    ])
    result = powershell(uninstall_code(tmp_path / "not-installed", tuning_dir, protected))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("uninstall done")
    assert "Cannot remove" in result.stderr or "Cannot remove" in result.stdout
    assert (tmp_path / "AlertColors.old-1").exists() and not (tmp_path / "AlertColors.old-2").exists()



@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_the_tuning_of_another_account_is_not_copied_into_the_new_folder(tmp_path):
    """Another account can make the folder before the first install: its look must not pass."""
    tuning_dir = tmp_path / "AlertColors"
    tuning_dir.mkdir()
    (tuning_dir / "tuning.json").write_text('{"display": {"brightness": 0.4}}', encoding="utf-8")
    code = "\n".join([
        f"$TuningDir = '{tuning_dir}'", f"$TuningMaxBytes = {MAX_FILE_BYTES}",
        "function New-AdminFolder([string]$Dir) {",
        "    New-Item -ItemType Directory -Path $Dir -ErrorAction Stop | Out-Null",
        "}",
        "function Set-LockedRights([string]$Dir) { }",
        guard_code(), ps_function("Write-NewFile"), owner_code("'NT AUTHORITY\\SYSTEM'"),
        ps_function("Move-ForeignTuningDir"),
        "Move-ForeignTuningDir",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert "was not copied" in result.stdout
    assert not (tuning_dir / "tuning.json").exists()


def test_the_admin_part_gets_the_restart_options():
    start = SCRIPT.index("$arguments = @('-NoProfile'")
    arguments = SCRIPT[start:SCRIPT.index("Start-Process", start)]
    assert "'-RestartCount', $RestartCount" in arguments
    assert "'-RestartIntervalMinutes', $RestartIntervalMinutes" in arguments


def restore_code(app_checks_until_exit):
    """Restore-StoppedApp with an app that still runs for this many checks (None: it never exits)."""
    running = "$true" if app_checks_until_exit is None else f"($script:checks++ -lt {app_checks_until_exit})"
    return "\n".join([
        "$script:StoppedForCheck = $true", "$TaskName = 'AlertColors'", "$StopTimeoutSeconds = 3",
        "$script:started = 0", "$script:checks = 0",
        f"function Get-App {{ if ({running}) {{ 'the app' }} }}",
        "function Start-Sleep { param($Seconds) }",
        "function Start-ScheduledTask([string]$TaskName) { $script:started += 1 }",
        ps_function("Restore-StoppedApp"),
        "Restore-StoppedApp | Out-Null",
        "Restore-StoppedApp | Out-Null",  # a second call (the outer catch) starts nothing more
        '"started $script:started"',
    ])


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_check_stop_that_timed_out_starts_the_app_when_it_has_exited():
    """The app still released the LEDs when the setup gave up: it starts again after its exit."""
    result = powershell(restore_code(app_checks_until_exit=2))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "started 1"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_an_app_that_does_not_exit_is_not_started_twice_and_the_user_is_told():
    result = powershell(restore_code(app_checks_until_exit=None))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "started 0"
    assert "setup.cmd restart" in " ".join((result.stdout + result.stderr).split())


def test_every_failed_run_without_admin_rights_restores_the_app():
    end = SCRIPT[SCRIPT.rindex("\ncatch {"):]
    handler = end[:end.index("\n}")]
    assert "if (-not $Elevated)" in handler and "Restore-StoppedApp" in handler
    assert "$script:RestoredApp = $true" in ps_function("Test-Config")  # its own start counts


# ---- the owners of a folder that goes aside ----------------------------------------------
def foreign_move_code(tuning_dir, owners):
    """Move-ForeignTuningDir with fake owners: {path: "me" | "other"}."""
    me, other = "S-1-5-21-1-1-1-1001", "S-1-5-21-1-1-1-1002"
    table = "; ".join(f"'{path}' = '{me if who == 'me' else other}'" for path, who in owners.items())
    return "\n".join([
        f"$TuningDir = '{tuning_dir}'", f"$TuningMaxBytes = {MAX_FILE_BYTES}",
        "function New-AdminFolder([string]$Dir) {",
        "    New-Item -ItemType Directory -Path $Dir -ErrorAction Stop | Out-Null",
        "}",
        "function Set-LockedRights([string]$Dir) { }",
        f"$owners = @{{ {table} }}",
        "function Get-OwnerSid([string]$Path) { $owners[$Path] }",
        f"function Get-TargetSid {{ '{me}' }}",
        guard_code(), ps_function("Write-NewFile"), ps_function("Move-ForeignTuningDir"),
        "Move-ForeignTuningDir",
    ])


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
@pytest.mark.parametrize(("folder", "file", "copied"), [
    ("me", "me", True), ("me", "other", False), ("other", "me", False), ("other", "other", False),
])
def test_tuning_json_is_copied_only_when_the_folder_and_the_file_are_of_the_installing_user(
        tmp_path, folder, file, copied):
    """Other accounts can add a file to a folder of the user: its owner decides."""
    tuning_dir = tmp_path / "AlertColors"
    tuning_dir.mkdir()
    (tuning_dir / "tuning.json").write_text('{"display": {"brightness": 0.4}}', encoding="utf-8")
    owners = {str(tuning_dir): folder, str(tuning_dir / "tuning.json"): file}
    result = powershell(foreign_move_code(tuning_dir, owners))
    assert result.returncode == 0, result.stderr
    assert (tuning_dir / "tuning.json").exists() is copied


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_an_unknown_installing_account_stops_before_any_folder_is_made(tmp_path):
    tuning_dir = tmp_path / "AlertColors"
    tuning_dir.mkdir()
    code = "\n".join([
        "$ErrorActionPreference = 'Stop'", f"$TuningDir = '{tuning_dir}'",
        "$TargetUser = 'NO-SUCH-PC\\no-such-account'",
        "function New-AdminFolder([string]$Dir) { throw 'made a folder' }",
        ps_function("Get-TargetSid"), ps_function("Move-ForeignTuningDir"), "Move-ForeignTuningDir",
    ])
    result = powershell(code)
    assert result.returncode != 0 and "Cannot find the account" in result.stderr
    assert "made a folder" not in result.stderr
    assert [p.name for p in tmp_path.iterdir()] == ["AlertColors"]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_the_early_check_does_not_refuse_a_folder_that_goes_aside(tmp_path):
    """A subfolder in a folder that Administrators do not own: the folder goes aside as it is."""
    tuning_dir = tmp_path / "AlertColors"
    (tuning_dir / "sub").mkdir(parents=True)
    code = "\n".join([
        f"$TuningDir = '{tuning_dir}'", "function Test-AdminOwner([string]$Dir) { $false }",
        guard_code(), ps_function("Test-ReparsePoint"), ps_function("Assert-NoLinks"),
        ps_function("Test-TuningDirReady"), "Test-TuningDirReady", "'ready'",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ready")


# ---- the Armoury Crate file --------------------------------------------------------------
def process_list_code(folder, owner_sid):
    sids = "\n".join(line for line in SCRIPT.splitlines() if line.startswith("$Sid") and " = '" in line)
    return "\n".join([
        "$ErrorActionPreference = 'Stop'", sids,
        f"$ProcessList = '{folder / 'ProcessList.ini'}'", "$ProcessListMaxBytes = 1048576",
        f"function Get-OwnerSid([string]$Path) {{ '{owner_sid}' }}",
        guard_code(), ps_function("Test-ReparsePoint"), ps_function("Test-TrustedProcessList"),
        ps_function("Read-ProcessList"),
    ])


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
@pytest.mark.parametrize(("owner", "trusted"), [
    ("S-1-5-32-544", True), ("S-1-5-18", True), ("S-1-5-21-1-1-1-1002", False),
    ("S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464", True),  # TrustedInstaller
])
def test_only_a_process_list_of_armoury_crate_is_changed(tmp_path, owner, trusted):
    folder = tmp_path / "ASUS"
    folder.mkdir()
    (folder / "ProcessList.ini").write_text("[ProcessList]\r\n", encoding="utf-16")
    result = powershell(process_list_code(folder, owner) + "\nTest-TrustedProcessList")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(trusted)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows links")
def test_a_process_list_that_is_a_link_is_not_trusted(tmp_path):
    folder = tmp_path / "ASUS"
    folder.mkdir()
    target = tmp_path / "elsewhere.ini"
    target.write_text("[ProcessList]\r\n", encoding="utf-16")
    try:
        (folder / "ProcessList.ini").symlink_to(target)
    except OSError:
        pytest.skip("this account cannot make symbolic links")
    result = powershell(process_list_code(folder, "S-1-5-32-544") + "\nTest-TrustedProcessList")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_the_process_list_reads_back_the_same_lines(tmp_path):
    folder = tmp_path / "ASUS"
    folder.mkdir()
    (folder / "ProcessList.ini").write_text("[ProcessList]\r\nA.exe=0\r\nB.exe=0\r\n", encoding="utf-16", newline="")
    check = "\n".join([
        "$new = @(Read-ProcessList)",
        "$old = @([IO.File]::ReadAllLines($ProcessList, [Text.Encoding]::Unicode))",
        "$same = ($new -join '|') -eq ($old -join '|')",
        '"same: $same $($new -join "|")"',
    ])
    result = powershell(process_list_code(folder, "S-1-5-32-544") + "\n" + check)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "same: True [ProcessList]|A.exe=0|B.exe=0"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_process_list_in_a_folder_of_another_owner_is_not_trusted(tmp_path):
    """The file itself belongs to Administrators, but another account made the folder."""
    folder = tmp_path / "ASUS"
    folder.mkdir()
    (folder / "ProcessList.ini").write_text("[ProcessList]\r\n", encoding="utf-16")
    code = process_list_code(folder, "unused") + "\n" + "\n".join([
        f"function Get-OwnerSid([string]$Path) {{ if ($Path -eq '{folder}') {{ 'S-1-5-21-1-1-1-1002' }} "
        "else { 'S-1-5-32-544' } }",
        "Test-TrustedProcessList",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def armoury_code(folder, trusted, backup_owner="S-1-5-32-544"):
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "\n".join(line for line in SCRIPT.splitlines() if line.startswith("$Sid") and " = '" in line),
        f"$ProcessList = '{folder / 'ProcessList.ini'}'", f"$ProcessListBackup = '{folder / 'ProcessList.ini.bak'}'",
        "$ProcessListMaxBytes = 1048576", "$InstallExe = 'C:\\Program Files\\AlertColors\\app\\AlertColors.exe'",
        "$LegacyExe = 'AlertColors.exe'",
        f"function Test-TrustedProcessList {{ ${'true' if trusted else 'false'} }}",
        f"function Get-OwnerSid([string]$Path) {{ '{backup_owner}' }}",
        guard_code(), ps_function("Write-NewFile"), ps_function("Read-ProcessList"), ps_function("Write-ProcessList"),
        ps_function("Test-EntryOf"), ps_function("Set-ArmouryCrateEntry"),
        "Set-ArmouryCrateEntry $true",
    ])


ORIGINAL_LIST = "[ProcessList]\r\nOther.exe=0\r\n"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
@pytest.mark.parametrize("trusted", [True, False])
def test_the_entry_is_written_only_into_a_trusted_process_list(tmp_path, trusted):
    folder = tmp_path / "ASUS"
    folder.mkdir()
    (folder / "ProcessList.ini").write_text(ORIGINAL_LIST, encoding="utf-16", newline="")
    result = powershell(armoury_code(folder, trusted))
    assert result.returncode == 0, result.stderr
    content = (folder / "ProcessList.ini").read_text(encoding="utf-16")
    assert ("AlertColors.exe=0" in content) is trusted
    assert (folder / "ProcessList.ini.bak").exists() is trusted  # the backup of the first change
    if not trusted:
        assert "is not a file of Armoury Crate" in " ".join((result.stdout + result.stderr).split())


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_file_of_another_account_with_the_backup_name_is_left_as_it_is(tmp_path):
    folder = tmp_path / "ASUS"
    folder.mkdir()
    (folder / "ProcessList.ini").write_text(ORIGINAL_LIST, encoding="utf-16", newline="")
    (folder / "ProcessList.ini.bak").write_text("planted", encoding="utf-8")
    result = powershell(armoury_code(folder, trusted=True, backup_owner="S-1-5-21-1-1-1-1002"))
    assert result.returncode == 0, result.stderr
    assert "not a backup" in " ".join((result.stdout + result.stderr).split())
    assert (folder / "ProcessList.ini.bak").read_text(encoding="utf-8") == "planted"
    assert "AlertColors.exe=0" in (folder / "ProcessList.ini").read_text(encoding="utf-16")


# ---- the app of this session, and the flag of the check stop -----------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_the_app_of_another_session_is_not_this_app():
    code = "\n".join([
        "$ProcessName = 'AlertColors'",
        "$session = [Diagnostics.Process]::GetCurrentProcess().SessionId",
        "function Get-Process { param($Name, $ErrorAction)",
        "  @([pscustomobject]@{ Id = 1; SessionId = $session + 1 },",
        "    [pscustomobject]@{ Id = 2; SessionId = $session }) }",
        ps_function("Get-App"),
        "@(Get-App).Id",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["2"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows events")
def test_the_check_stop_counts_as_a_stop_also_when_the_wait_fails():
    """The app can exit later: a failed setup must still start it again."""
    name = rf"Local\AlertColors.Test.{os.getpid()}.flag"
    code = "\n".join([
        f"$StopEventName = '{name}'", "$StopTimeoutSeconds = 1", "$InstallLog = 'log'",
        "$stopEvent = New-Object System.Threading.EventWaitHandle($false, 'ManualReset', $StopEventName)",
        "function Get-App { 'still running' }",
        "function Start-Sleep { param($Seconds) }",
        ps_function("Stop-AppAsUser"),
        "try { Stop-AppAsUser | Out-Null } catch { 'did not stop' }",
        '"flag: $script:StoppedForCheck"',
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-2:] == ["did not stop", "flag: True"]


# ---- the steps that another account can make fail ----------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_file_of_another_account_that_cannot_be_deleted_stops_the_step_with_a_clear_message(tmp_path):
    folder = tmp_path / "tuning"
    folder.mkdir()
    (folder / "tuning.json").write_text("{}", encoding="utf-8")
    sids = "\n".join(line for line in SCRIPT.splitlines() if line.startswith("$Sid") and " = '" in line)
    code = "\n".join([
        sids, "function Get-TargetSid { 'S-1-5-21-1-1-1-1001' }",
        "function Get-OwnerSid([string]$Path) { 'S-1-5-21-1-1-1-1002' }",
        "function Use-FolderGuardType { }",
        "function Remove-PlainFile([string]$Path) { throw 'held open' }",  # a real sharing violation
        "Add-Type -TypeDefinition 'public static class AlertColorsFolderGuard { "
        "public static void RenameAside(string a, string b, bool c) { throw new System.Exception(\"held open\"); } }'",
        ps_function("Replace-Files"), f"Replace-Files '{folder}'",
    ])
    result = powershell(code)
    joined = " ".join(result.stderr.split())
    assert result.returncode != 0 and "a file of another account" in joined and "held open" in joined


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_backup_that_cannot_be_written_does_not_stop_the_entry(tmp_path):
    folder = tmp_path / "ASUS"
    folder.mkdir()
    (folder / "ProcessList.ini").write_text(ORIGINAL_LIST, encoding="utf-16", newline="")
    code = armoury_code(folder, trusted=True).replace(
        ps_function("Write-NewFile"), "function Write-NewFile($Path, $Content) { throw 'the name appeared' }")
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert "No backup" in " ".join((result.stdout + result.stderr).split())
    assert "AlertColors.exe=0" in (folder / "ProcessList.ini").read_text(encoding="utf-16")


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_status_shows_the_entry_also_when_a_folder_has_the_name_of_the_list(tmp_path):
    (tmp_path / "ASUS" / "ProcessList.ini").mkdir(parents=True)  # planted: a folder, not a file
    start = SCRIPT.index("    # Only shown, never written: a plain read")
    block = SCRIPT[start:SCRIPT.index('Write-Output "Armoury Crate entry: $entry"', start)]
    code = "\n".join([
        f"$ProcessList = '{tmp_path / 'ASUS' / 'ProcessList.ini'}'", "$ProcessListMaxBytes = 1048576",
        "$InstallExe = 'x'", ps_function("Test-EntryOf"), block, '"entry: $entry"',
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "entry: not registered"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_process_list_of_another_account_in_a_trusted_folder_is_not_trusted(tmp_path):
    folder = tmp_path / "ASUS"
    folder.mkdir()
    (folder / "ProcessList.ini").write_text("[ProcessList]\r\n", encoding="utf-16")
    code = process_list_code(folder, "unused") + "\n" + "\n".join([
        f"function Get-OwnerSid([string]$Path) {{ if ($Path -eq '{folder}') {{ 'S-1-5-32-544' }} "
        "else { 'S-1-5-21-1-1-1-1002' } }",
        "Test-TrustedProcessList",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owners")
@pytest.mark.parametrize("admin", [True, False])
def test_a_new_file_of_the_setup_is_given_to_administrators(admin):
    """A policy can make the approving admin account the owner of new objects. Only an admin can
    give a file to Administrators (on NTFS): for this test account the real call fails, which
    shows that it is made; without admin rights the setup does not try."""
    import tempfile

    sids = "\n".join(line for line in SCRIPT.splitlines() if line.startswith("$Sid") and " = '" in line)
    with tempfile.TemporaryDirectory(dir=os.environ["LOCALAPPDATA"]) as folder:
        path = Path(folder) / "new.txt"
        code = "\n".join([sids, guard_code(), f"function Test-Admin {{ ${'true' if admin else 'false'} }}",
                          "function Use-AdminTemp { }",  # the compile folder of the elevated part
                          ps_function("Write-NewFile"), f"Write-NewFile '{path}' ([byte[]](65, 66))", "'written'"])
        result = powershell(code)
        assert path.read_text(encoding="utf-8") == "AB"  # the file comes first
    if admin:
        assert result.returncode != 0  # the owner change was refused (the message depends on the language)
    else:
        assert result.returncode == 0 and result.stdout.strip().endswith("written")


# ---- owners that cannot be read ----------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_an_owner_that_cannot_be_read_is_no_owner_and_no_error(tmp_path):
    """The owner of an object can refuse even Administrators the read: no error stops the setup."""
    code = "\n".join([ps_function("Get-OwnerSid"), f"$o = Get-OwnerSid '{tmp_path / 'missing.txt'}'",
                      "if ($null -eq $o) { 'no owner' } else { $o }"])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "no owner"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_folder_of_another_account_is_moved_aside_without_being_opened(tmp_path):
    """Its owner can refuse the admin the open that the folder guard needs."""
    tuning_dir = tmp_path / "AlertColors"
    tuning_dir.mkdir()
    (tuning_dir / "tuning.json").write_text("{}", encoding="utf-8")
    code = "\n".join([
        f"$TuningDir = '{tuning_dir}'", f"$TuningMaxBytes = {MAX_FILE_BYTES}",
        "function New-AdminFolder([string]$Dir) {",
        "    New-Item -ItemType Directory -Path $Dir -ErrorAction Stop | Out-Null",
        "}",
        "function Set-LockedRights([string]$Dir) { }",
        "function Get-OwnerSid([string]$Path) { $null }",  # the owner cannot be read
        "function Get-TargetSid { 'S-1-5-21-1-1-1-1001' }",
        "function Open-FolderGuard([string]$Dir) { throw 'the folder was opened' }",
        guard_code().replace(ps_function("Open-FolderGuard"), ""),
        ps_function("Write-NewFile"), ps_function("Move-ForeignTuningDir"), "Move-ForeignTuningDir",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert "was not copied" in result.stdout
    assert sum(p.name.startswith("AlertColors.old-") for p in tmp_path.iterdir()) == 1


# ---- files that deny the change of their attributes -----------------------------------------
def write_attributes_denied(folder):
    """A file whose access list denies the change of its attributes (as another account can
    give its own file); the folder still lets its owner delete entries."""
    path = folder / "tuning.json"
    path.write_text("{}", encoding="utf-8")
    subprocess.run(["icacls", str(path), "/deny", "*S-1-1-0:(WA)"], capture_output=True, check=True)
    return path


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rights")
def test_a_file_that_denies_the_change_of_its_attributes_is_still_deleted():
    """Remove-Item -Force changes the attributes first and fails here; a plain delete works."""
    import tempfile

    with tempfile.TemporaryDirectory(dir=os.environ["LOCALAPPDATA"]) as folder:
        path = write_attributes_denied(Path(folder))
        forced = powershell(f"Remove-Item -LiteralPath '{path}' -Force -ErrorAction Stop")
        assert forced.returncode != 0 and path.exists()  # the trap
        result = powershell("\n".join([guard_code(), ps_function("Remove-PlainFile"), f"Remove-PlainFile '{path}'"]))
        assert result.returncode == 0, result.stderr
        assert not path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rights")
def test_the_uninstall_removes_a_tuning_folder_with_such_a_file():
    import tempfile

    with tempfile.TemporaryDirectory(dir=os.environ["LOCALAPPDATA"]) as parent:
        tuning_dir = Path(parent) / "AlertColors"
        tuning_dir.mkdir()
        write_attributes_denied(tuning_dir)
        result = powershell(uninstall_code(Path(parent) / "not-installed", tuning_dir))
        assert result.returncode == 0, result.stderr
        assert not tuning_dir.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_restore_that_gave_up_does_not_wait_again():
    """The UAC catch and the outer catch both call it: the user waits one time."""
    result = powershell(restore_code(app_checks_until_exit=None))  # it calls the restore two times
    assert result.returncode == 0, result.stderr
    assert " ".join((result.stdout + result.stderr).split()).count("still stops") == 1  # one wait, one warning



# ---- the uninstall and links and folders of other accounts --------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows links")
def test_the_uninstall_removes_a_link_at_the_tuning_folder_name_but_not_its_target(tmp_path):
    """Any account can put a junction at the free name: the files of its target stay."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    tuning_dir = tmp_path / "AlertColors"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(tuning_dir), str(target)], capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    result = powershell(uninstall_code(tmp_path / "not-installed", tuning_dir))
    assert result.returncode == 0, result.stderr
    assert not tuning_dir.exists() and (target / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_the_uninstall_leaves_a_tuning_folder_of_another_account_alone(tmp_path):
    tuning_dir = tmp_path / "AlertColors"
    tuning_dir.mkdir()
    (tuning_dir / "theirs.txt").write_text("theirs", encoding="utf-8")
    result = powershell(uninstall_code(tmp_path / "not-installed", tuning_dir, admin_owned="$false"))
    assert result.returncode == 0, result.stderr
    assert (tuning_dir / "theirs.txt").exists()
    assert "does not belong to Administrators" in " ".join((result.stdout + result.stderr).split())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rights")
@pytest.mark.parametrize(("protect", "renamed"), [("deny-read", False), ("readonly-deny-attributes", True)])
def test_a_file_of_another_account_that_blocks_a_plain_delete_is_removed_from_the_name(protect, renamed):
    """The admin refused every read: the file is deleted through the delete-child right. Read-only
    with the attribute change denied cannot be deleted by name, so it is renamed aside to a short
    fixed-length stem, so a long original name cannot push the aside past the 255-character limit.
    Either way tuning.json is free, and the app reads only tuning.json."""
    import re
    import tempfile

    with tempfile.TemporaryDirectory(dir=os.environ["LOCALAPPDATA"]) as name:
        folder = Path(name)
        path = folder / ("deny" + "x" * 140 + ".json")  # a long foreign name: the aside must stay a short stem
        path.write_text("{}", encoding="utf-8")
        if protect == "readonly-deny-attributes":
            subprocess.run(["attrib", "+R", str(path)], capture_output=True, check=True)
            subprocess.run(["icacls", str(path), "/deny", "*S-1-1-0:(WA)"], capture_output=True, check=True)
        else:
            subprocess.run(["icacls", str(path), "/deny", "*S-1-1-0:(R)"], capture_output=True, check=True)
        sids = "\n".join(line for line in SCRIPT.splitlines() if line.startswith("$Sid") and " = '" in line)
        code = "\n".join([sids, guard_code(), "function Get-TargetSid { 'S-1-5-21-1-1-1-1001' }",
                          "function Get-OwnerSid([string]$Path) { 'S-1-5-21-1-1-1-1002' }",  # another account
                          ps_function("Write-NewFile"), ps_function("Remove-PlainFile"), ps_function("Replace-Files"),
                          f"$TuningMaxBytes = {MAX_FILE_BYTES}", f"Replace-Files '{folder}'"])
        try:
            result = powershell(code)
            assert result.returncode == 0, result.stderr
            assert not path.exists()  # the long foreign name is free
            aside = [p for p in folder.iterdir() if p.name.startswith("foreign-")]
            assert len(aside) == (1 if renamed else 0)
            if renamed:
                # The aside name is a short fixed-length stem, independent of the source name.
                assert re.fullmatch(r"foreign-[0-9a-f]{32}", aside[0].name)
                # A second run leaves the aside file as it is: nothing new is created.
                assert powershell(code).returncode == 0
                assert [p.name for p in folder.iterdir() if p.name.startswith("foreign-")] == [aside[0].name]
        finally:
            for leftover in folder.iterdir():
                subprocess.run(["icacls", str(leftover), "/remove:d", "*S-1-1-0"], capture_output=True)
                subprocess.run(["attrib", "-R", str(leftover)], capture_output=True)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_process_list_that_cannot_be_read_only_gives_a_warning():
    """Another account can hold ProcessList.ini open: the install and the uninstall go on."""
    code = "\n".join([
        "function Set-ArmouryCrateEntry([bool]$Present) { throw 'the file is open in another program' }",
        ps_function("Update-ArmouryCrateEntry"), "Update-ArmouryCrateEntry $true", "'went on'",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("went on")
    assert "Armoury Crate entry not changed" in " ".join((result.stdout + result.stderr).split())
    body = function_body("Install-App") + ps_function("Invoke-AdminPart")
    assert "Set-ArmouryCrateEntry" not in body and body.count("Update-ArmouryCrateEntry") == 2


# ---- deleting and moving an object through a delete-right handle ---------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows links")
def test_remove_plain_file_deletes_a_link_not_its_target(tmp_path):
    """The delete opens the link itself (OPEN_REPARSE_POINT), so the target stays."""
    target = tmp_path / "secret.txt"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "tuning.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this account cannot make symbolic links")
    result = powershell("\n".join([guard_code(), ps_function("Remove-PlainFile"), f"Remove-PlainFile '{link}'"]))
    assert result.returncode == 0, result.stderr
    assert not link.exists() and target.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rights")
def test_a_foreign_tuning_folder_that_refuses_the_admin_read_is_moved_aside(tmp_path):
    """Move-ForeignTuningDir renames the whole folder through a delete-right handle, so a folder
    whose access list refuses every read still goes aside and frees the name."""
    tuning_dir = tmp_path / "AlertColors"
    tuning_dir.mkdir()
    (tuning_dir / "tuning.json").write_text("{}", encoding="utf-8")
    subprocess.run(["icacls", str(tuning_dir), "/deny", "*S-1-1-0:(R)"], capture_output=True, check=True)
    code = "\n".join([
        f"$TuningDir = '{tuning_dir}'", f"$TuningMaxBytes = {MAX_FILE_BYTES}",
        "function New-AdminFolder([string]$Dir) { New-Item -ItemType Directory -Path $Dir -EA Stop | Out-Null }",
        "function Set-LockedRights([string]$Dir) { }",
        "function Get-OwnerSid([string]$Path) { 'S-1-5-21-1-1-1-1002' }",  # not the installing user
        "function Get-TargetSid { 'S-1-5-21-1-1-1-1001' }",
        guard_code(), ps_function("Write-NewFile"), ps_function("Move-ForeignTuningDir"), "Move-ForeignTuningDir",
    ])
    try:
        result = powershell(code)
        assert result.returncode == 0, result.stderr
        assert tuning_dir.is_dir()  # the new admin folder took the name
        aside = [p for p in tmp_path.iterdir() if p.name.startswith("AlertColors.old-")]
        assert len(aside) == 1
    finally:
        for folder in tmp_path.iterdir():  # the test account owns them: clear the deny so pytest can delete
            subprocess.run(["icacls", str(folder), "/remove:d", "*S-1-1-0"], capture_output=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows links")
def test_the_early_check_removes_a_link_at_the_tuning_folder_name_not_its_target(tmp_path):
    """Any account can put a junction at the free name: the setup removes the link and goes on."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    tuning_dir = tmp_path / "AlertColors"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(tuning_dir), str(target)], capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    code = "\n".join([
        f"$TuningDir = '{tuning_dir}'", "function Test-AdminOwner([string]$Dir) { $true }",
        ps_function("Test-ReparsePoint"), ps_function("Remove-LinkAtName"), ps_function("Assert-NoLinks"),
        guard_code(), ps_function("Test-TuningDirReady"), "Test-TuningDirReady", "'ready'",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ready")
    assert not tuning_dir.exists() and (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_the_app_copy_bounds_the_robocopy_retries():
    copy_app = ps_function("Copy-App")
    assert "/R:$RobocopyRetries" in copy_app and "/W:$RobocopyWaitSeconds" in copy_app
    assert "$RobocopyRetries = 3" in SCRIPT and "$RobocopyWaitSeconds = 2" in SCRIPT


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rename")
def test_the_native_rename_builds_a_valid_rename_record_for_any_name_length():
    """The rename record is zeroed and the name is terminated, so no driver reads past the name.
    Renames of growing name lengths exercise names that an un-terminated record would corrupt."""
    import tempfile

    with tempfile.TemporaryDirectory(dir=os.environ["LOCALAPPDATA"]) as root:
        code = "\n".join([guard_code(), "Use-FolderGuardType", f"$root = '{root}'", "$fail = 0",
                          "for ($i = 0; $i -lt 60; $i++) {",
                          "  $len = 5 + ($i % 40)",
                          "  $d = Join-Path $root ('d' + ('x' * $len) + $i)",
                          "  New-Item -ItemType Directory $d | Out-Null",
                          "  $dest = Join-Path $root ('m' + ('y' * $len) + $i)",
                          "  try { [AlertColorsFolderGuard]::RenameAside($d, $dest, $true) } catch { $fail++ }",
                          "}",
                          '"failures: $fail"'])
        result = powershell(code)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == "failures: 0"

"""The real Set-LockedRights of scripts/manage.ps1, run on a test folder.

The lock must remove every earlier explicit right, also the write right of the
installing user from an earlier install. The test runs the function text from
manage.ps1 itself, so it tests the shipped code, not a copy.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "manage.ps1").read_text(encoding="utf-8")
EVERYONE = "*S-1-1-0"


def ps_function(name):
    start = re.search(rf"^function {re.escape(name)}[ (]", SCRIPT, re.M).start()
    end = SCRIPT.index("\n}", start) + 2
    return SCRIPT[start:end]


def user_rights_variables():
    return "\n".join(line for line in SCRIPT.splitlines() if re.match(r"^\$User(Folder|File)Rights = '", line))


def lock_code():
    return ps_function("New-LockedSecurity") + "\n" + ps_function("Set-LockedRights")


def renew_code():
    """Replace-Files with what it needs: the plain-file read of the guard and its size limit."""
    size = re.search(r"^\$TuningMaxBytes = \d+", SCRIPT, re.M).group(0)
    start = SCRIPT.index('$FolderGuardSource = @"')
    source = SCRIPT[start:SCRIPT.index('"@', start) + 2]
    load = "if (-not ('AlertColorsFolderGuard' -as [type])) { Add-Type -TypeDefinition $FolderGuardSource }"
    owners = [sid_variables(), "$TargetUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name",
              ps_function("Get-OwnerSid"), ps_function("Get-TargetSid")]  # this test account installs
    return "\n".join([size, source, load, "function Test-Admin { $false }", ps_function("Use-FolderGuardType"),
                      *owners, ps_function("Write-NewFile"), ps_function("Remove-PlainFile"),
                      ps_function("Replace-Files")])


def sid_variables():
    return "\n".join(line for line in SCRIPT.splitlines() if re.match(r"^\$Sid\w+ = '", line))


def powershell(code):
    """Stop at the first error, as the script does: a missing helper fails the test."""
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                           "$ErrorActionPreference = 'Stop'\n" + code],
                          capture_output=True, text=True, check=False)


def icacls(folder):
    result = subprocess.run(["icacls", str(folder)], capture_output=True, text=True, check=False)
    return result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rights")
def test_the_lock_removes_every_earlier_right(tmp_path):
    folder = tmp_path / "tuning"
    folder.mkdir()
    me = powershell("[Security.Principal.WindowsIdentity]::GetCurrent().Name").stdout.strip()
    for grant in (f"{me}:(OI)(CI)M", f"{EVERYONE}:(OI)(CI)M"):  # as after an earlier install
        subprocess.run(["icacls", str(folder), "/grant", grant], capture_output=True, check=True)
    assert "(M)" in icacls(folder)
    result = powershell(f"{sid_variables()}\n{lock_code()}\nSet-LockedRights '{folder}'")
    assert result.returncode == 0, result.stderr
    rights = icacls(folder)
    assert "(M)" not in rights  # neither the user nor Everyone can write
    assert "Everyone" not in rights and "(I)" not in rights  # no inherited rules
    assert rights.count("(OI)(CI)(F)") == 2  # SYSTEM, Administrators
    import win32security

    dacl = win32security.GetFileSecurity(str(folder), win32security.DACL_SECURITY_INFORMATION) \
        .GetSecurityDescriptorDacl()
    sids = {win32security.ConvertSidToStringSid(dacl.GetAce(i)[2]) for i in range(dacl.GetAceCount())}
    assert sids == {"S-1-5-18", "S-1-5-32-544"}  # no Users (S-1-5-32-545): no other account can open the folder


def file_id(path):
    """The NTFS file ID: a new file object gets a new one."""
    return os.stat(path).st_ino


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rights")
def test_each_file_becomes_a_new_file_with_inherited_rights_only(tmp_path):
    """A file made earlier by another account, with its own rules, becomes a new file object
    with the same content. A handle on the old file keeps no rights on the new one."""
    folder = tmp_path / "tuning"
    folder.mkdir()
    tuning = folder / "tuning.json"
    tuning.write_text('{"display": {"brightness": 0.5}}', encoding="utf-8")
    subprocess.run(["icacls", str(tuning), "/grant", f"{EVERYONE}:F"], capture_output=True, check=True)
    old_id = file_id(tuning)
    result = powershell(f"{renew_code()}\nReplace-Files '{folder}'")
    assert result.returncode == 0, result.stderr
    rights = icacls(tuning)
    assert "Everyone" not in rights
    assert all("(I)" in line for line in rights.splitlines()[:-2] if line.strip())  # inherited rules only
    assert file_id(tuning) != old_id
    assert tuning.read_text(encoding="utf-8") == '{"display": {"brightness": 0.5}}'


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handles")
def test_a_file_that_another_program_holds_open_stops_the_setup(tmp_path):
    import ctypes
    from ctypes import wintypes

    folder = tmp_path / "tuning"
    folder.mkdir()
    tuning = folder / "tuning.json"
    tuning.write_text("{}", encoding="utf-8")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read, share_read, open_existing = 0x80000000, 0x1, 3  # no delete sharing
    handle = kernel32.CreateFileW(str(tuning), generic_read, share_read, None, open_existing, 0, None)
    assert handle not in (None, wintypes.HANDLE(-1).value)
    try:
        result = powershell(f"{renew_code()}\nReplace-Files '{folder}'")
    finally:
        kernel32.CloseHandle(handle)
    assert result.returncode != 0
    assert "Cannot renew" in result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows owners")
def test_a_folder_that_another_account_owns_is_refused(tmp_path):
    """A folder that this account made (as a program without admin rights would) is refused."""
    folder = tmp_path / "tuning"
    folder.mkdir()
    code = "\n".join([sid_variables(), ps_function("Get-OwnerSid"), ps_function("Test-AdminOwner"),
                      ps_function("Assert-AdminOwner"), f"Assert-AdminOwner '{folder}'"])
    result = powershell(code)
    assert result.returncode != 0
    assert "belongs to another account" in result.stderr


# ---- the folder guard of the setup ---------------------------------------------------------
def guard_code():
    start = SCRIPT.index('$FolderGuardSource = @"')
    end = SCRIPT.index('"@', start) + 2
    return "\n".join([SCRIPT[start:end], "function Test-Admin { $false }", ps_function("Use-FolderGuardType"),
                      ps_function("Open-FolderGuard")])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handles")
def test_while_the_guard_is_held_the_folder_cannot_be_renamed(tmp_path):
    folder = tmp_path / "tuning"
    folder.mkdir()
    code = "\n".join([
        guard_code(),
        f"$guard = Open-FolderGuard '{folder}'",
        f"try {{ Rename-Item -LiteralPath '{folder}' -NewName 'moved' -ErrorAction Stop; 'renamed while held' }}",
        "catch { 'refused while held' }",
        "$guard.Dispose()",
        f"Rename-Item -LiteralPath '{folder}' -NewName 'moved' -ErrorAction Stop; 'renamed after'",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["refused", "while", "held", "renamed", "after"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handles")
def test_the_guard_refuses_a_folder_that_another_program_holds_with_delete_access(tmp_path):
    import ctypes
    from ctypes import wintypes

    folder = tmp_path / "tuning"
    folder.mkdir()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    delete, share_all, open_existing, backup_semantics = 0x00010000, 0x7, 3, 0x02000000
    handle = kernel32.CreateFileW(str(folder), delete, share_all, None, open_existing, backup_semantics, None)
    assert handle not in (None, wintypes.HANDLE(-1).value)
    try:
        result = powershell(guard_code() + f"\nOpen-FolderGuard '{folder}' | Out-Null")
    finally:
        kernel32.CloseHandle(handle)
    assert result.returncode != 0
    assert "Cannot hold" in result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows links")
def test_the_guard_refuses_a_junction(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    junction = tmp_path / "tuning"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    result = powershell(guard_code() + f"\nOpen-FolderGuard '{junction}' | Out-Null")
    assert result.returncode != 0
    assert "Cannot hold" in result.stderr


def test_the_setup_holds_the_guard_over_every_step_on_paths():
    body = SCRIPT[SCRIPT.index("function Initialize-TuningDir"):]
    body = body[:body.index("\n}\n")]
    guard = body.index("$guard = Open-FolderGuard $TuningDir")
    first_path_step = body.index("Assert-NoLinks $TuningDir")
    grant = body.index('Grant-UserWrite $TuningDir $user')
    release = body.index("$guard.Dispose()")
    assert guard < first_path_step < grant < release
    assert body.index("} finally {") < release  # released also after an error


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rights")
def test_the_steps_of_the_setup_work_while_the_guard_is_held(tmp_path):
    """The guard has no delete sharing: the steps on paths need no delete access, so they work."""
    folder = tmp_path / "tuning"
    folder.mkdir()
    (folder / "tuning.json").write_text("{}", encoding="utf-8")
    me = powershell("[Security.Principal.WindowsIdentity]::GetCurrent().Name").stdout.strip()
    code = "\n".join([
        sid_variables(), user_rights_variables(),
        guard_code(), ps_function("Test-ReparsePoint"), ps_function("Assert-NoLinks"),
        lock_code(), renew_code(), ps_function("Grant-UserWrite"),
        f"$guard = Open-FolderGuard '{folder}'",
        "try {",
        f"    Assert-NoLinks '{folder}'",
        f"    Set-LockedRights '{folder}'",
        # The real setup runs as admin, which keeps full control under the lock.
        f"    icacls '{folder}' /L /grant \"{me}:(OI)(CI)F\" | Out-Null",
        f"    Assert-NoLinks '{folder}'",
        f"    Replace-Files '{folder}'",
        f"    if (-not (Grant-UserWrite '{folder}' '{me}')) {{ throw 'grant failed' }}",
        "} finally { $guard.Dispose() }",
        "'all steps done'",
    ])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("all steps done")
    assert (folder / "tuning.json").read_text(encoding="utf-8") == "{}"  # renewed with its content


def test_the_guard_is_held_before_the_owner_check():
    """The owner check must see the folder that the guard holds: else a program could swap the
    folder between the check and the guard."""
    body = SCRIPT[SCRIPT.index("function Initialize-TuningDir"):]
    body = body[:body.index("\n}\n")]
    assert body.index("$guard = Open-FolderGuard $TuningDir") < body.index("Assert-AdminOwner $TuningDir")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rights")
def test_after_the_setup_the_user_can_save_the_tuning_but_cannot_move_the_folder(tmp_path):
    """The rights of the end of the setup, for this account without admin rights: the
    configurator's save works, and the folder stays where the admin app reads it."""
    from alert_colors import tuning

    parent = tmp_path / "ProgramData"
    parent.mkdir()
    folder = parent / "tuning"
    folder.mkdir()
    (folder / "tuning.json").write_text("{}", encoding="utf-8")
    me = powershell("[Security.Principal.WindowsIdentity]::GetCurrent().Name").stdout.strip()
    # As on ProgramData: no right to delete the entries of the parent.
    subprocess.run(["icacls", str(parent), "/deny", f"{me}:(DC)"], capture_output=True, check=True)
    code = "\n".join([sid_variables(), user_rights_variables(), lock_code(), ps_function("Grant-UserWrite"),
                      f"Set-LockedRights '{folder}'",
                      f"if (-not (Grant-UserWrite '{folder}' '{me}')) {{ throw 'grant failed' }}"])
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    tuning.write_file(folder / "tuning.json", {"display": {"brightness": 0.5}})  # a .tmp file, then a replace
    assert tuning.read_file(folder / "tuning.json") == {"display": {"brightness": 0.5}}
    with pytest.raises(PermissionError):
        folder.rename(parent / "moved")
    with pytest.raises(PermissionError):
        (folder / "sub").mkdir()
    # The test account owns the folder here (an admin owns the real one): give it back its rights
    # so that pytest can remove it.
    subprocess.run(["icacls", str(parent), "/remove:d", me], capture_output=True, check=True)
    subprocess.run(["icacls", str(folder), "/grant", f"{me}:(OI)(CI)F"], capture_output=True, check=True)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_renew_that_fails_after_the_delete_keeps_the_content_on_disk(tmp_path):
    """The new file comes first: a failed last step loses no tuning."""
    folder = tmp_path / "tuning"
    folder.mkdir()
    (folder / "tuning.json").write_text('{"display": {"brightness": 0.5}}', encoding="utf-8")
    code = "\n".join([renew_code(), "function Rename-Item { throw 'the name is still in use' }",
                      f"Replace-Files '{folder}'"])
    result = powershell(code)
    assert result.returncode != 0 and "Its content is also in" in result.stderr
    kept = [p for p in folder.iterdir() if p.name.startswith("tuning.json.new-")]
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == '{"display": {"brightness": 0.5}}'


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_a_file_of_another_account_in_an_old_folder_is_deleted_not_renewed(tmp_path):
    """An old install did not lock the folder: another account could add a tuning file."""
    folder = tmp_path / "tuning"
    folder.mkdir()
    (folder / "tuning.json").write_text('{"display": {"brightness": 0.1}}', encoding="utf-8")
    (folder / "mine.txt").write_text("kept", encoding="utf-8")
    stub = "function Get-OwnerSid([string]$Path) { if ($Path -like '*tuning.json') { 'S-1-5-21-1-1-1-1002' } " \
           "else { [Security.Principal.WindowsIdentity]::GetCurrent().User.Value } }"
    result = powershell(f"{renew_code()}\n{stub}\nReplace-Files '{folder}'")
    assert result.returncode == 0, result.stderr
    assert not (folder / "tuning.json").exists()
    assert (folder / "mine.txt").read_text(encoding="utf-8") == "kept"

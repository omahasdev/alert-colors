# PyInstaller spec. Build with `setup.cmd build` (scripts\manage.ps1).
#
# Optional modules come from build.toml ([build] lighting, sources). The
# environment variables ALERT_COLORS_LIGHTING and ALERT_COLORS_SOURCES override
# one list for one build (comma list, or `none`); `setup.cmd build -Lighting ...
# -Sources ...` sets them. Modules that are not selected are left out of the
# exe completely, with their vendor libraries.
#
# The build writes build-manifest.json next to the exe. The install reads it:
# only a build with a module that needs admin rights runs elevated.
import json
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT / "src"))
from alert_colors import __version__  # noqa: E402
from alert_colors import lighting, sources  # noqa: E402

NONE = "none"
profile = tomllib.loads((ROOT / "build.toml").read_text(encoding="utf-8"))["build"]
NAME = profile["name"]


def selected(group, env_name, known):
    override = os.environ.get(env_name)
    if override is None:
        names = list(profile[group])
    elif override.strip().lower() == NONE:
        names = []
    else:
        names = [n.strip() for n in override.split(",") if n.strip()]
    unknown = sorted(set(names) - set(known))
    if unknown:
        raise SystemExit(f"unknown {group} modules {unknown}; known: {list(known)}")
    return names


GROUPS = {
    "lighting": ("alert_colors.lighting", selected("lighting", "ALERT_COLORS_LIGHTING", lighting.OPTIONAL_MODULES),
                 lighting.OPTIONAL_MODULES),
    "sources": ("alert_colors.sources", selected("sources", "ALERT_COLORS_SOURCES", sources.OPTIONAL_MODULES),
                sources.OPTIONAL_MODULES),
}
hiddenimports, excludes = [], ["tkinter", "unittest", "pytest"]
for group, (package, names, known) in GROUPS.items():
    print(f"alert-colors build: {group} = {names or 'none'}")
    hiddenimports += [f"{package}.{n}" for n in names]
    excludes += [f"{package}.{n}" for n in known if n not in names]
lighting_modules = GROUPS["lighting"][1]
if "aura_sdk" not in lighting_modules:
    excludes += ["win32com", "pythoncom"]  # COM is only for the Aura SDK

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    hiddenimports=hiddenimports,
    datas=[(str(ROOT / "src" / "alert_colors" / "tuner.html"), "alert_colors")],  # the configurator page
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    console=False,  # no console window; the log file is the output
    version=str(ROOT / "packaging" / "version_info.txt"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

manifest = {
    "name": NAME,
    "version": __version__,
    "lighting": lighting_modules,
    "sources": GROUPS["sources"][1],
    "needs_admin": any(lighting.MODULE_NEEDS[m]["admin"] for m in lighting_modules),
    "needs_armoury_crate": any(lighting.MODULE_NEEDS[m]["armoury_crate"] for m in lighting_modules),
}
(Path(DISTPATH) / NAME / "build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

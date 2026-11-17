# Documentation

Everything to install, configure, and understand **alert-colors** — a Windows
app that turns your RGB lighting into a live air-raid-alert indicator.

New here? Start with the [root README](../README.md) for what it is, then
[Setup](SETUP.md) for a clean install.

## Guides

| Guide | What it covers |
|---|---|
| [Setup](SETUP.md) | Step-by-step clean install, the modules your PC needs, and every `setup.cmd` action |
| [Commands](COMMANDS.md) | All commands: `setup.cmd` actions and options, and the exe command line |
| [Configuration](CONFIG.md) | Every config file, section, and key, with defaults |
| [Sources](SOURCES.md) | The alert sources, their order of use, and how to get and save a token |
| [Architecture](ARCHITECTURE.md) | How the parts fit, the module map, and how to add your own |

## Repository map

| Path | What it is |
|---|---|
| [`setup.cmd`](../setup.cmd) | Double-click: build, install, update, restart, status, uninstall, region, token |
| [`configure.cmd`](../configure.cmd) | Double-click: open the configurator |
| [`config.toml`](../config.toml) | All settings with their defaults; `examples/sources/` has a block for each source |
| [`build.toml`](../build.toml) | Build profile: which optional modules go into the exe |
| [`scripts/manage.ps1`](../scripts/manage.ps1) | The logic behind `setup.cmd` |
| [`packaging/`](../packaging) | PyInstaller spec and exe metadata |
| [`requirements-build.txt`](../requirements-build.txt) | Exact build versions with SHA-256 hashes |
| [`src/alert_colors/`](../src/alert_colors) | The app |
| [`tests/`](../tests) | Unit tests and the Ruff lint check (every build runs them) |
| `docs/` | This documentation and the configurator screenshot |

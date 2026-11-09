# Command reference

All commands of alert-colors: the two double-click scripts, the actions of
`setup.cmd`, and the command line of `AlertColors.exe`. The
[full setup guide](SETUP.md) tells when to use them.

## Double-click scripts

These two files are in the project folder.

| Script | What it does | Admin rights |
|---|---|---|
| `setup.cmd` | Shows a menu: install, update, restart, status, uninstall, build, configurator, watched place | Only for the install part (one UAC prompt) |
| `configure.cmd` | Opens the configurator of the installed app in the browser | No |

## `setup.cmd` actions

Run `setup.cmd` without an argument to get the menu. Give the action as an
argument to run it without the menu: `setup.cmd status`.

| Menu | Action | What it does | When |
|---|---|---|---|
| 1 | `install` | Build, choose the place, check the config, copy to Program Files, add the Armoury Crate entry and the logon task, start | The first install |
| 2 | `update` | Build, check the config, stop, copy the app and the config, update the entry and the task, start | After a code change or a new version |
| 3 | `restart` | Check the config, stop, copy the config, start | After you edit the config |
| 4 | `status` | Show the config in use, the process, the task, the Armoury Crate entry, the modules and the last log lines | At any time |
| 5 | `uninstall` | Stop, remove the task, the Armoury Crate entry, the installed files and the tuning folder | To remove the app |
| 6 | `build` | Make `.venv`, run the tests, build `dist\AlertColors` | For development |
| 7 | `tune` | Open the configurator (the same as `configure.cmd`) | To tune colors and effects |
| 8 | `region` | Search the place, save it in the config of your PC, then restart the installed app | To watch a different place |

`install`, `update`, `restart` and `uninstall` need admin rights. The build
always runs with your normal rights. Then only the admin part continues in an
elevated window.

### Options

Put the options after the action: `setup.cmd install -Lighting razer_chroma`.

| Option | Default | What it does |
|---|---|---|
| `-Lighting <list>` | the list in `build.toml` | The lighting modules for this build: a comma list (`aura_sdk,razer_chroma`) or `none` |
| `-Sources <list>` | the list in `build.toml` | The source modules for this build: a comma list (`siren_pp_ua,ubilling`) or `none` |
| `-Python <path>` | automatic | The Python to use for the build. Without it, the script finds Python with the `py` launcher, then in `PATH` |
| `-StopTimeoutSeconds <n>` | 120 | How long to wait for the app to release the LEDs and stop. It must be more than `backend_close_timeout_s` |
| `-RestartCount <n>` | 3 | How many times the logon task restarts the app after a crash |
| `-RestartIntervalMinutes <n>` | 1 | The wait before each restart |
| `-LogLines <n>` | 10 | How many log lines `status` shows |

`-Lighting` and `-Sources` apply to one build only. The next `update` uses
`build.toml` again.

## `AlertColors.exe` command line

The installed exe is `C:\Program Files\AlertColors\app\AlertColors.exe`. Give
the config with `--config` before the command:

```
"C:\Program Files\AlertColors\app\AlertColors.exe" --config "C:\Program Files\AlertColors\config.toml" status
```

Without `--config`, the exe reads `config.toml` in the current folder.

| Command | What it does | Exit code |
|---|---|---|
| `run [--dry-run]` | Watch the alerts and drive the LEDs. The logon task runs this command. `--dry-run` logs the colors and does not use the hardware | 0 after a stop |
| `stop` | Ask the running app to release the LEDs and exit. Use this command, never a kill | 0: stop sent. 1: no app runs |
| `check` | Load the config, build all parts, fetch each source one time: `[source]`, then each fallback of `[failover]`. A config error in any source fails the check. A server that does not answer fails it only when no source gives a status | 0: OK. 1: an error |
| `status` | Fetch the alert status one time, then print it and the scene. With fallback sources, it does this for each source | 0: fetched. Not 0: the fetch failed (with fallbacks: all sources failed) |
| `devices` | List the devices of each enabled lighting backend. `[x]` marks the devices that the config includes | 0 |
| `preview <scene> --seconds <n>` | Show one scene on the hardware for `n` seconds, then release the LEDs | 0. 1: the scene only releases the LEDs |
| `tune [--no-browser]` | Start the configurator on `127.0.0.1` and open it in the browser. `--no-browser` prints the URL only | 0 |
| `region [--id <id>]` | Search the watched place by name and write it into the `--config` file. `--id` sets the region ID without questions | 0: saved. 1: no place has this ID. 3: no change |

The scenes for `preview` are the `[scenes.*]` names in the config: `clear`,
`unknown`, `alert`, `partial`, `threat`, `artillery`, `urban_fights`,
`chemical`, `nuclear`.

Some commands have conditions:

- `devices` and `preview` need admin rights for the motherboard and the ARGB headers.
- `status` and `check` fetch the source. Some sources limit the rate for each IP address. While the app runs, these commands can get HTTP 429. `check` waits and tries again, up to 5 attempts.
- `preview` and the running app write to the same LEDs. Run `stop` before `preview`, or use the preview in the configurator.
- `region` changes only the file that `--config` names. To change the place of the installed app, use `setup.cmd region`.

## Developer commands

Run these from the project root, with the `.venv` that the build makes:

```
.venv\Scripts\python -m pytest
set PYTHONPATH=src
.venv\Scripts\python -m alert_colors --config config.toml status
.venv\Scripts\python -m alert_colors --config config.toml run --dry-run
```

`python -m alert_colors` accepts all the commands of `AlertColors.exe`.

# Full setup guide

[Back to the README](../README.md)

This guide takes a clean Windows PC to a running app. For a first install, do
sections 1 to 4 in this order. The other sections are for reference.

1. [Before you start](#1-before-you-start)
2. [Choose the modules](#2-choose-the-modules)
3. [Get the project](#3-get-the-project)
4. [Install, step by step](#4-install-step-by-step)

- [Choose your region](#choose-your-region)
- [Maintain: update, restart, uninstall](#maintain-update-restart-uninstall)
- [Build profiles](#build-profiles)
- [Configuration](#configuration)
- [Security](#security)
- [Stop: never kill the app](#stop-never-kill-the-app)
- [Armoury Crate integration](#armoury-crate-integration)
- [Troubleshooting](#troubleshooting)
- [Developer commands](#developer-commands)
- [Measured facts](#measured-facts)

## 1. Before you start

You need:

- Windows 10 or 11, and an account with admin rights (for one UAC prompt).
- Internet access to the alert API (siren.pp.ua by default, no key). The first build also downloads packages from PyPI.
- Approximately 150 MB of disk space: 75 MB for the build environment (`.venv`), 35 MB for the build, 25 MB for the installed app.
- Python 3.14. If it is not installed, the setup offers to install it with `winget`.
  If `winget` is not available, install Python 3.14 from https://www.python.org, then start the setup again.
- The vendor app of each lighting system that you want to use:
  - **ASUS devices:** Armoury Crate with Aura Sync. Open Armoury Crate one time and make sure that it shows your devices.
  - **Razer devices:** Razer Synapse with the Chroma module. In Synapse, allow Chroma apps
    (the Chroma Connect or "Chroma Apps" setting). The app uses the Chroma SDK of Synapse on `localhost`.

The first build takes approximately 2 to 5 min. Later builds take approximately 1 min.

## 2. Choose the modules

The app is a small core with optional modules. Each module has one job. The
default build includes all modules. A module without its hardware or vendor app
on your PC only writes an error to the log every 30 s. You can keep it, or
remove it as the table at the end of this section shows.

**Lighting modules: they show the status on the hardware**

| Module | What it is for | Requirements | Admin rights |
|---|---|---|---|
| `aura_sdk` | ASUS Aura devices: motherboard LEDs, ASUS graphics card, ARGB headers (LED strips, fans), and other Aura Sync devices | Armoury Crate with Aura Sync | Yes: the app then runs elevated |
| `razer_chroma` | Razer Chroma devices: keyboards (one color for each of 22 columns), mice, and other Chroma devices | Razer Synapse, with Chroma apps allowed | No |
| `console` (always in the build) | No hardware: writes each scene change to the log. For tests, and for a build without lighting modules | Nothing | No |

**Source modules: they read the status from an API**

| Module | What it is for | API key required | Smallest area |
|---|---|---|---|
| `siren_pp_ua` (default) | Ukrainian air-raid alerts from https://siren.pp.ua, a public mirror of the official ukrainealarm.com API. The fastest source without an API key: 1 to 3 s from a change to the LEDs | No | Community (hromada), with alert types |
| `ukrainealarm` | The same data from the official API https://api.ukrainealarm.com. Use it if you have an official API key (`UKRAINEALARM_API_KEY`) | Yes | Community, with alert types |
| `alerts_in_ua` | Alerts from https://api.alerts.in.ua. Use it if you have a token from that service | Yes (a token) | Community, with alert types |
| `ubilling` | Alerts from https://ubilling.net.ua/aerialalerts/. A fallback without an API key | No | Oblast only |
| `alerts_com_ua` | Alerts from https://alerts.com.ua. The service stopped updating its data (checked 2026-11-08): do not use it until it works again | No | Oblast only |
| `json_http` (always in the build) | Any other JSON API: you write the request and the fields in the config (`examples\sources\custom_json_http.toml`). Each source above is a preset of this module | Only if that API needs one | Any |

**The modules for your PC**

| Your PC has | Lighting modules | Install with |
|---|---|---|
| ASUS and Razer devices | `aura_sdk`, `razer_chroma` | The default: choose **1** in `setup.cmd` |
| ASUS devices only | `aura_sdk` | `setup.cmd install -Lighting aura_sdk` |
| Razer devices only | `razer_chroma` | `setup.cmd install -Lighting razer_chroma` (the app runs without admin rights) |
| No supported lighting | none | `setup.cmd install -Lighting none` (the app only logs) |

An override on the command line applies to that run only. To keep your choice
for each later update, edit the `lighting` list in `build.toml` (see
[Build profiles](#build-profiles)). To turn off one module without a new build,
set `enabled = false` in its `[[lighting]]` entry of the config of your PC (see
[The config of your PC](#the-config-of-your-pc)).

## 3. Get the project

1. Get the files with one of these methods:
   - With git: `git clone https://github.com/omahasdev/alert-colors %USERPROFILE%\alert-colors`.
   - Without git: download the ZIP of the repository, then extract it to `%USERPROFILE%\alert-colors`.
2. If you used the ZIP, right-click `setup.cmd`, then choose **Properties**.
3. Select **Unblock**, then click **OK**. Do steps 2 and 3 for `configure.cmd` too.

Keep the project in a folder that only your account can change, for example in
your user profile. The install part of the setup runs with admin rights from
this folder (see [Security](#security)).

If Windows SmartScreen stops `setup.cmd`, click **More info**, then **Run anyway**.

## 4. Install, step by step

1. Double-click **`setup.cmd`**. A console window opens with a menu.
2. Type **1** (Install), then press Enter.
3. If the setup asks to install Python 3.14 with winget, type **y**, then press Enter.
4. Wait for the build. The setup makes `.venv`, installs the pinned packages
   (exact versions with SHA-256 hashes), runs the tests, and builds the app in `dist\AlertColors`.
5. If this PC has no config yet, the setup shows "Watched place now". Type a part of the name of your
   city, community, district or oblast. Ukrainian and Latin letters both work.
6. Type the number of your place in the list. To keep the test region, press Enter instead.
7. Wait for `CHECK OK`. The setup fetches the alert status one time with your config.
   If the check fails, the setup installs nothing and shows the cause.
8. Accept the UAC prompt. A second console window does the admin part. It copies the app to
   `C:\Program Files\AlertColors`, adds the Armoury Crate entry and the autostart task, and starts the app.
9. In each of the two console windows, press Enter to close it.
10. If you use `aura_sdk`, do the Armoury Crate steps below.

The Armoury Crate steps (one time):

1. Open **Armoury Crate**, then open **Aura Sync**.
2. Make sure that **In-Game lighting effects** is on.
3. Open the in-game lighting priority list. It can need approximately 1 min to load, because
   Armoury Crate first reads all the apps of its Game Library.
4. Move **AlertColors.exe** ("Air Alert Colors") to the top.

Caution: Make sure that **AlertColors.exe** stays at the top of the priority
list. The ASUS SDK gives the LEDs to the app with the highest priority. If an
other app is above it (for example, after a new game is added to the list),
the PC LEDs do not show the alert status. Look at the list again after an
Armoury Crate update.

Note: After the install, and after each start of the app, the PC LEDs (ASUS
Aura) can need approximately 30 s before they change. The first device scan of
the ASUS SDK takes this time. The Razer devices change at once.

**Make sure that the app works:**

1. Run `setup.cmd status`. It must show "Process: running", the task, the
   Armoury Crate entry (with `aura_sdk`), and the config file in use.
2. Look at the last log lines in the status output. A line such as `scene unknown -> clear`
   shows that the source works.
3. Double-click **`configure.cmd`**, or run `"%USERPROFILE%\alert-colors\configure.cmd"`.
   In the browser page, click **Preview 10 s** on the
   `alert` card. The LEDs must breathe red for 10 s, then show the current state again.

The app starts automatically each time you log on.

## Choose your region

The project `config.toml` watches the test region of the API (id `0`). To
choose your place, run `setup.cmd region` (or choose **8** in the menu), or
answer the question during the first install:

1. Type a part of the name, in Ukrainian or Latin letters ("kharkiv" finds "Харків").
2. Choose the number of the place in the list. Each line shows the place, its district and its oblast.
3. If the app is installed, accept the UAC prompt. The setup then applies the new place.

The first install asks this question in step 5. If you ran `setup.cmd region`
before the install, the install does not ask again.

You can choose a community (hromada), a district (raion) or an oblast. The
setup writes three ids into `[source.vars]`:

| Variable | Meaning | Example: Dnipro |
|---|---|---|
| `region_id` | The watched place | `332` |
| `district_id` | Its district: an alert there covers the place | `44` |
| `oblast_id` | Its oblast: an alert there covers the place, and an alert elsewhere in it gives the partial state (P) | `9` |

The tree of the API has some cities at the top level, with no children:
Kharkiv and Zaporizhzhia. The picker links them to their oblast and district
by their name ("м. Харків та Харківська ..." is in "Харківська область").
Kyiv city belongs to no oblast. The picker works for the sources `siren_pp_ua`
and `ukrainealarm`. For other sources, set the ids in the config by hand.

### The config of your PC

The project `config.toml` holds the defaults. The settings of one PC go to
`%APPDATA%\AlertColors\config.toml`:

- The place picker makes this file: it copies the project `config.toml`, then changes the three ids.
- If this file exists, `setup.cmd` installs it instead of the project `config.toml`.
- `setup.cmd status` shows which file is in use.
- To change other settings of your PC, edit this file, then choose **Restart** (3).
- To use the project defaults again, delete this file, then choose **Restart**.

A new version of the project `config.toml` does not change this file. After an
update, compare the two files if you want the new defaults.

## Maintain: update, restart, uninstall

Double-click **`setup.cmd`** and choose a number, or give the action as an
argument (`setup.cmd status`). The [command reference](COMMANDS.md) lists all
actions, their options and the command line of the exe.

| # | Action | What it does | When |
|---|---|---|---|
| 1 | Install | Build, choose the place, check, copy to Program Files, Armoury Crate entry, autostart, start | The first install |
| 2 | Update | Build the current source, check, stop, copy, start | After you get a new version of the project |
| 3 | Restart | Check the config, stop, copy the config, start | After you edit the config |
| 4 | Status | Config in use, process, autostart task, Armoury Crate entry, modules, last log lines | At any time |
| 5 | Uninstall | Stop, remove the autostart task, the Armoury Crate entry, the installed files and the tuning folder | To remove the app |
| 6 | Build | Build only | For development |
| 7 | Configurator | Open the configurator (the same as `configure.cmd`) | To tune colors and effects |
| 8 | Watched place | Search the place, save it in the config of your PC, apply it | To watch a different place |

The build runs with your normal rights. Only the install part asks for admin
rights (one UAC prompt each time).

**Update to a new version:**

1. Get the new files: run `git pull` in the project folder, or extract the new ZIP over the old folder.
2. Run `setup.cmd`, then choose **2** (Update).

The update keeps the config of your PC and the configurator tuning.

**Uninstall:** choose **5**. These items stay on the PC. If you do not need them, delete them by hand:

- The project folder, with `.venv`, `build` and `dist`.
- The config of your PC: `%APPDATA%\AlertColors`.
- The backup of the Armoury Crate list: `C:\ProgramData\ASUS\ProcessList.ini.alert-colors.bak`.
- Python 3.14, if the setup installed it (uninstall it in **Settings > Apps**).

## Build profiles

`build.toml` selects the modules that go into the exe. The section
[Choose the modules](#2-choose-the-modules) tells what each module is for.
Modules that are not selected are not in the exe at all, with their vendor libraries.

```toml
[build]
lighting = ["aura_sdk", "razer_chroma"]   # [] = no lighting: the app only logs
sources = ["siren_pp_ua", "ukrainealarm", "ubilling", "alerts_in_ua", "alerts_com_ua"]
```

One run can override a list: `setup.cmd install -Lighting razer_chroma -Sources siren_pp_ua`,
or `-Lighting none`. The next Update uses `build.toml` again. The build writes
`build-manifest.json` next to the exe. The install reads it: a build without
`aura_sdk` runs **without** admin rights and gets no Armoury Crate entry. If the
config names a lighting module that is not in the build, the app logs a warning
and skips it.

## Configuration

The project `config.toml` has all settings, with a comment for each. Edit the config of your PC
(`%APPDATA%\AlertColors\config.toml`), not the project file. After a change, choose **Restart**.

**Source.** `type` selects a source module. A preset fills in the request,
the fields, the kind map and the time format. Any table in `[source]`
overrides a value of the preset. `examples\sources\` has a complete block for
each source. For any other JSON API, use `type = "json_http"`
(`examples\sources\custom_json_http.toml`):

| Setting | Meaning |
|---|---|
| `poll_interval_s` | Wait after each successful fetch |
| `backoff_max_s` | After errors (for example HTTP 429), the wait doubles up to this value. The server's `Retry-After` is obeyed. |
| `stale_after_s` | Older data shows the `unknown` scene, never `clear` |
| `[source.vars]` | Values for `${name}` placeholders. `${env:NAME}` reads an environment variable, for tokens. |
| `[source.request]` | `url`, `method`, `headers`, `params`, `body`, `timeout_s`, and the network limits (see Security) |
| `[source.fields]` | JMESPath expressions (https://jmespath.org) on the JSON response: `full`, `partial`, `kinds`, `observed_at`, `changed_at` |
| `[source.kinds]` | `map` (API value → kind name), `default`, `unmapped` |
| `[source.time]` | `format` (`iso`, `epoch`, or a strftime pattern), `timezone` |
| `[source.change_marker]` | Optional cheap pre-check: `url`, `field`, `max_age_s`. Each poll fetches only the marker; the full request runs when it changes or after `max_age_s`. `siren_pp_ua` and `ukrainealarm` use `lastActionIndex` (38 bytes). |

Region: the setup writes the ids (see [Choose your region](#choose-your-region)). For example, `region_id = "332"` is "м. Дніпро та Дніпровська територіальна громада".
All ids (oblast > district > community): https://siren.pp.ua/api/v3/regions.
The response for a region lists every alert that covers it, also the alerts of
its district and oblast, so the city lights up for a district alert too.

Freshness without a key: siren.pp.ua caches for 1 s and allows approximately
1 request per second for each IP. With `poll_interval_s = 1`, an update shows
after 1–3 s; approximately one poll in 15 s gets HTTP 429 and waits 2 s. With
an official key, switch to `ukrainealarm` (same API, the key in
`UKRAINEALARM_API_KEY`; request form on https://api.ukrainealarm.com).

An expression that gives no value (for example, a wrong region name) is an
error, and the LEDs show `unknown`. It is never read as "no alert".

**Failover.** A fallback source keeps the LEDs correct when the server of
`[source]` is down. `[[failover.sources]]` lists the fallbacks in order. Each
entry takes the keys of `[source]`, and gets the `[source.vars]` of `[source]`.
Its own `[failover.sources.vars]` replace single values. Without
`[[failover.sources]]`, the app reads only `[source]`, as before.

| Setting | Default | Meaning |
|---|---|---|
| `[failover] after_s` | 30 | Use a fallback when `[source]` had no success for this long. It must be less than `stale_after_s` |
| `[failover] retry_s` | 10 | After a failure, skip the source for this long. Keep it less than `after_s`, so that one timeout does not switch |
| `[failover] retry_max_s` | 120 | The skip time doubles after each failure in a row, up to this value |
| `poll_interval_s` in an entry | 10 | Fetch this fallback at most this often. Between fetches, the app uses its last status |

`[source]` is always the first choice. HTTP 429 never switches to a fallback:
the server works, and the app keeps the last status. While a fallback is in use,
the app tries `[source]` again after each skip time, and goes back to it when it
works. The log shows `using fallback <name>` and `<name> works again`.

Example: `ubilling` as the fallback of `siren_pp_ua`. It needs no key, but it
has oblast level only. While it is in use, an alert anywhere in the oblast
shows the `alert` scene, never `partial`. The `region` value is the oblast name
as the feed spells it:

```toml
[[failover.sources]]
type = "ubilling"
poll_interval_s = 10
[failover.sources.vars]
region = "Київська область"
```

`setup.cmd region` changes only `[source.vars]`. If you choose a place in
another oblast, change `region` of the fallback too. A wrong name fails the
check before each install, and it never shows "no alert".

**Rules.** `[[rules]]` select the scene. The first rule that matches wins, so
the order is the priority. Conditions: `state` (`ok` / `unknown`), `active`,
`coverage` (`none` / `partial` / `full`), `kinds` (any of a list). The config
needs one rule with `state = "unknown"` and a last rule with no conditions.
The shipped rules give these states of the watched city:

| State | When | Look |
|---|---|---|
| N | No active alert | Dim steady green |
| R | An alert on the city, its district or its oblast (level Red) | Red breathing, never below 50 % |
| Y | A preliminary threat on the city (level Yellow only) | Yellow breathing |
| P | No alert on the city, but one elsewhere in the oblast | Steady orange |
| unknown | No data, or data older than `stale_after_s` | Slow purple breathing |

Nuclear, chemical, street fighting and artillery alerts have their own
colors. They come before R, and P comes before them: a partial status has no
kinds of the city.

**Scenes.** Each `[scenes.<name>]` runs one effect, or gives the LEDs back
(`release = true`). The effects follow Armoury Crate (Aura), Razer Synapse and
OpenRGB. "Per LED" effects give each LED its own color: Aura sets every LED of a
device, the Razer keyboard gets one color for each of its 22 columns, and a
device with one color (the mouse) shows the first position.

| Effect | Engine name | Per LED | Options |
|---|---|---|---|
| `solid` | Static | | `color`, `brightness` |
| `pulse` | Breathing | | `color`, `period_s`, `min_brightness`, `max_brightness` |
| `blink` | Blink | | `color`, `period_s`, `duty`, `brightness`, `off_color` |
| `strobe` | Strobing / Flashing | | `color`, `period_s`, `flashes`, `flash_s`, `brightness`, `off_color` |
| `color_cycle` | Color cycle / Spectrum cycling | | `colors` (empty: spectrum), `period_s`, `saturation`, `brightness`, `smooth` |
| `rainbow` | Rainbow / Rainbow wave | yes | `period_s`, `repeats`, `direction`, `saturation`, `brightness`, `moving` |
| `gradient` | Gradient / Color wave | yes | `colors`, `period_s`, `repeats`, `direction`, `brightness`, `moving` |
| `wave` | Wave | yes | `color`, `background`, `period_s`, `repeats`, `direction`, `min_brightness`, `max_brightness` |
| `comet` | Comet / Flash and dash | yes | `color`, `background`, `period_s`, `tail`, `direction` |
| `scanner` | Scanner / Glowing yoyo | yes | `color`, `background`, `period_s`, `tail` |
| `twinkle` | Starry night / Starlight | yes | `colors`, `background`, `density`, `period_s`, `brightness`, `seed` |
| `marquee` | Marquee / Theater chase | yes | `color`, `background`, `spacing`, `lit`, `period_s`, `direction` |
| `fire` | Fire | yes | `colors` (cold to hot), `speed`, `flicker`, `brightness`, `seed` |

Every option has a range and a default (see `src/alert_colors/effects.py`).
An unknown option or a value out of range is an error. An ARGB header reports
its maximum (120 LEDs). If fewer LEDs are connected, set `led_counts =
{ "AddressableStrip 1" = 30 }` in the `aura_sdk` entry, so that a per-LED
effect spans the real strip.

**Taking the LEDs again.** A vendor app can take the LEDs back without a
message to the app. The ASUS service does it each time the display turns on.
The app listens for "display on" and "resume from sleep", then takes the LEDs
again every `reclaim_step_s` (5 s) for `reclaim_after_wake_s` (60 s). A vendor
app also takes the LEDs back when it starts. The app reads the process list every
`vendor_poll_s` (2 s). A new process with a name in `vendor_processes`
(Armoury Crate, the ASUS lighting service) starts the same window. It also takes
the LEDs again every `reclaim_interval_s` (300 s) at any time, for other causes.
All these keys are in `[display]`. The log has one line for each window.

**Brightness.** `[display] brightness` scales every color. `brightness` in a
`[[lighting]]` entry scales one backend.

## Security

The Aura module needs admin rights, so the app can run as admin. These rules
keep the network and the files from becoming a way to attack this PC:

- **Install location.** The app runs from `C:\Program Files\AlertColors`, where
  only administrators can change files. Malware without admin rights cannot
  replace the exe, a DLL or the config of the running app.
- **No elevated code from the project.** The build (pip, tests, PyInstaller)
  runs with normal rights. The elevated part of `setup.cmd` is PowerShell only.
  `manage.ps1` itself runs elevated from the project folder, as any installer
  does: keep the project in a folder that only your account controls.
- **Network.** The API client allows only `https`, follows no redirects,
  refuses hosts that resolve to loopback, private, link-local or reserved
  addresses, refuses credentials in the URL, checks the TLS certificate, and
  refuses responses over 1 MiB. `[source.request]` can relax `allow_http`,
  `allow_private_network` and `max_response_bytes` on purpose. The Razer
  backend talks only to loopback addresses.
- **Parsing.** JSON and JMESPath only read data. The API data can only select
  a scene from your own config.
- **Log file.** The path must stay inside the config folder.
- **Stop event.** Only SYSTEM, the administrators and the user who runs the
  app can signal it.
- **Packages.** pip installs only files whose SHA-256 hash is in
  `requirements-build.txt`.
- **Secrets.** Tokens come from environment variables (`${env:NAME}`), never
  from `config.toml`.

## Stop: never kill the app

Do not end `AlertColors.exe` in Task Manager, with `taskkill /F`, or with
`Stop-ScheduledTask`. A killed process cannot give the LEDs back, and Armoury
Crate then loses its devices until the `LightingService` service restarts.
`setup.cmd` stops the app through its stop event. If a kill happened, run
`Restart-Service LightingService -Force` as admin, then restart Armoury Crate.

## Armoury Crate integration

Armoury Crate cannot load a third-party effect. Its advanced effects are ASUS
"DLC" content: the Aura plugin accepts only names from its built-in list, and
it checks the ASUS signature of each effect program.

Armoury Crate does accept an entry in its **Aura In-Game Priority** list. Its
Aura plugin watches `C:\ProgramData\ASUS\ProcessList.ini`, and each `<exe>=0`
line becomes an app in that list. Armoury Crate identifies an app by its exe
path, so the app is a dedicated exe. `setup.cmd` adds and removes the entry.
The first change makes the backup `ProcessList.ini.alert-colors.bak`.

## Troubleshooting

| Problem | What to do |
|---|---|
| The PC LEDs do not change | Wait 30 s after a start. Make sure that **AlertColors.exe** is at the top of the Armoury Crate priority list, and that **In-Game lighting effects** is on. |
| Armoury Crate does not find its devices | The app was killed (see [Stop](#stop-never-kill-the-app)). Run `Restart-Service LightingService -Force` in an admin PowerShell, then restart Armoury Crate. |
| The Razer devices do not change | Make sure that Synapse runs and that Chroma apps are allowed in Synapse. The log shows `backend razer_chroma failed` with the cause. |
| The LEDs breathe purple (`unknown`) | The app has no fresh data. Make sure that the PC has internet access, then look at the log for the error. If the server of the source is down, add a fallback source (see **Failover** in [Configuration](#configuration)). |
| The log shows HTTP 429 | The API limits the request rate. The app waits and tries again: with siren.pp.ua, approximately one poll in 15 s gets 429. Do not run two copies of the app. |
| `CHECK FAILED` in the setup | The console shows the cause. The old app runs unchanged. Correct the config, then run the action again. |
| An error every 30 s for a module that you do not use | Set `enabled = false` in its `[[lighting]]` entry, or build without it (see [Choose the modules](#2-choose-the-modules)). |
| The PC LEDs show the Armoury Crate effect after the display turns on | The app takes them again within 5 s. If it does not, look for `display on or resume` in the log. Without that line, the app gets no power events: restart it with `setup.cmd restart`. |
| The configurator does not open | Run `configure.cmd` again. The page runs only on `127.0.0.1`, and it closes after 30 min without use. |
| The antivirus reports `AlertColors.exe` | Some antivirus programs flag PyInstaller builds. You built the exe on your PC from this source: add an exception for `C:\Program Files\AlertColors`. |

Logs:

- The app: `C:\Program Files\AlertColors\alert-colors.log`. `setup.cmd status` shows the last lines.
- ASUS: `C:\ProgramData\ASUS\ARMOURY CRATE Diagnosis\LightingService\` (`LightingService.log`, `AuraSDK.log`).

## Developer commands

The [command reference](COMMANDS.md#alertcolorsexe-command-line) lists all commands with their exit codes.
From the project root, with the venv that the build makes:

```
.venv\Scripts\python -m pytest
set PYTHONPATH=src
.venv\Scripts\python -m alert_colors --config config.toml status         # fetch once, print the scene
.venv\Scripts\python -m alert_colors --config config.toml devices        # list LEDs for each backend
.venv\Scripts\python -m alert_colors --config config.toml run --dry-run  # log colors, no hardware
.venv\Scripts\python -m alert_colors --config config.toml preview alert --seconds 10
```

`devices` and `preview` need admin rights for the motherboard and ARGB headers.

**Update the pins.** Change a version in `requirements-build.txt`. Then
download the wheels (`pip download --no-deps --only-binary :all: <name>==<version>`),
put the SHA-256 of each file in its `--hash` value, and run `setup.cmd build`.

## Measured facts

These facts come from tests with Armoury Crate, Razer Synapse and the alert
APIs. They explain the values of some settings.

- The Aura SDK enumerates 0 devices in a single-threaded COM apartment. The backend uses MTA.
- The first Aura `Enumerate()` in a process takes ~30 s with admin rights. Later calls take ~0 s.
- The Aura SDK on the main thread blocks interpreter exit after a release. All
  commands use one worker thread for each backend.
- One Aura apply of all LEDs takes less than 0.1 s, so `frame_interval_s = 0.1`.
- `SwitchMode()` asks the SDK for the control token; the SDK compares the
  priority of the exe with the current holder. `ReleaseControl(0)` gives it
  back, and the SDK restores the Armoury Crate profile.
- Armoury Crate "Breathing" runs in the controller hardware: it continues
  until the app writes colors.
- Each time the display turns on, the ASUS service takes the Aura token back
  (`WM_POWERBROADCAST`, `PBT_POWERSETTINGCHANGE`). It took the token 0.2 s after
  the event, and applied its own profile to the devices 12 s after the event.
  The SDK in the app gets no error: its colors only stop showing. "Display off"
  does not take the token.
- At logon, `ArmouryCrate.exe` can start more than 60 s after the app, when
  the startup window has ended. Armoury Crate then loads its own profile, and
  the LEDs show it until the next interval reclaim. Thus the app also watches
  vendor process starts.
- One `SwitchMode()` adds approximately 14 lines (1.5 KB) to `AuraSDK.log`,
  which the ASUS service does not rotate, and approximately 43 lines to
  `LightingService.log`, which it rotates. So `reclaim_interval_s` is 300 s, not less.
- ubilling answers HTTP 429 when polled too fast (twice in 17 min at 1 s).
- siren.pp.ua allows approximately 1 request per second for each IP; the
  global `lastActionIndex` changed approximately every 7 s.
- A release of the Aura SDK can need 44 s in the ASUS service: the worker waits
  up to `backend_close_timeout_s` (90 s), and `setup.cmd` up to 120 s.
- The Chroma SDK sometimes refuses the first requests of a new session. The
  worker releases, waits `backend_retry_s`, and opens a new session.
- ASUS logs: `C:\ProgramData\ASUS\ARMOURY CRATE Diagnosis\LightingService\`
  (`LightingService.log`, `AuraSDK.log`).

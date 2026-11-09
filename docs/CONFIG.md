# Configuration reference

Every setting of alert-colors: the files, each section of `config.toml`, the
tuning file and the build profile. The [full setup guide](SETUP.md) tells how
to install, and the [command reference](COMMANDS.md) lists the commands.

## Contents

- [The files](#the-files)
- [Value types](#value-types)
- [`[source]`: where the status comes from](#source-where-the-status-comes-from)
- [`[failover]`: the order of the sources](#failover-the-order-of-the-sources)
- [`[[rules]]`: status to scene](#rules-status-to-scene)
- [`[scenes.<name>]`: what the LEDs show](#scenesname-what-the-leds-show)
- [`[display]`: frames, brightness, taking the LEDs again](#display-frames-brightness-taking-the-leds-again)
- [`[[lighting]]`: the hardware backends](#lighting-the-hardware-backends)
- [`[tuning]`: the configurator file](#tuning-the-configurator-file)
- [`[service]` and `[logging]`](#service-and-logging)
- [`tuning.json`](#tuningjson)
- [`build.toml`](#buildtoml)

## The files

| File | What it holds | Who writes it | How a change takes effect |
|---|---|---|---|
| `config.toml` in the project folder | The shipped defaults, with comments. The watched place is the test region of the API | The project | Used only when the next file does not exist |
| `%APPDATA%\AlertColors\config.toml` | Your own config. `setup.cmd region` makes it from the project file and writes the watched place into it | You, and `setup.cmd region` | `setup.cmd restart` (or Update) checks it and copies it to Program Files |
| `C:\Program Files\AlertColors\config.toml` | The copy that the app runs with | `setup.cmd` (admin) | Do not edit it: each install, update and restart replaces it |
| `C:\ProgramData\AlertColors\tuning.json` | The changes from the configurator: scenes, brightness, color correction, preview | The configurator, and you | The running app reloads it within `[tuning] check_interval_s` |
| `HKEY_CURRENT_USER\Environment` (your user environment) | The tokens of the alert services, never a config file | `setup.cmd token` | The next start of the app: `setup.cmd token` restarts it |
| `build.toml` in the project folder | The modules that go into `AlertColors.exe` | You | The next `setup.cmd build`, Install or Update |

Edit your own config, not the project file: `git pull` can then update the
project without a conflict. After an edit, run `setup.cmd restart`. The
restart checks the config first. If the check fails, the old app runs on
unchanged.

A new version can add settings. Each new setting is optional and has a
default in the code, so an older config file keeps working. To use a new
setting, copy its lines from the project `config.toml` into your own config.

## Value types

| Type | Example | Notes |
|---|---|---|
| Seconds | `poll_interval_s = 1` | A name that ends in `_s` is a time in seconds. Decimals are allowed |
| Factor | `brightness = 0.5` | 0 to 1. 1 is the full value |
| Color | `color = "#FF4000"` | `#RRGGBB` in hex |
| Color correction | `color_correction = [1.0, 0.69, 0.94]` | `[r, g, b]` factors, each 0 to 1 |
| Text with placeholders | `url = "https://x/${region_id}"` | `${name}` takes a value of `[source.vars]`. `${env:NAME}` takes an environment variable (also from the user environment in the registry), for tokens |
| Windows path | `file = '%ProgramData%\AlertColors\tuning.json'` | Use single quotes: a TOML literal string keeps the backslashes. `%NAME%` takes an environment variable |

"Required" in the tables below means that the app does not start without the
setting. Each other setting has the default that its row shows.

## `[source]`: where the status comes from

`type` selects a source module. A preset fills in the request, the fields, the
kind map and the time format of its API. Each key that you give replaces the
value of the preset. `examples\sources\` has a complete block for each source.

| Setting | Default | Meaning |
|---|---|---|
| `type` | Required | `siren_pp_ua`, `ukrainealarm`, `ubilling`, `alerts_in_ua`, `alerts_com_ua` or `json_http`. It must be in the build (see [`build.toml`](#buildtoml)). It is the first source unless a service with a saved token comes before it (see [`[failover]`](#failover-the-order-of-the-sources)) |
| `poll_interval_s` | Required | The wait after each successful fetch |
| `backoff_max_s` | `poll_interval_s` | After errors in a row, the wait doubles up to this value. If the server sends `Retry-After`, the app obeys it |
| `stale_after_s` | Required | Data older than this shows the `unknown` scene, never `clear` |

### `[source.vars]`: the watched place

The values for the `${name}` placeholders. `setup.cmd region` writes the
place one time for all sources:

| Variable | Meaning | Used by |
|---|---|---|
| `region_id` | The ID of the place (community, district or oblast). All IDs: https://siren.pp.ua/api/v3/regions | `siren_pp_ua`, `ukrainealarm`, `alerts_in_ua` |
| `district_id` | The ID of its district | the same |
| `oblast_id` | The ID of its oblast | the same |
| `oblast_name` | The name of its oblast, for example `"Київська область"` | `ubilling`, `alerts_in_ua` (for the `partial` scene) |

Some presets use other names. Each one takes its value from a variable of the
table, unless the config gives it:

| Preset | Its variable | Takes the value of |
|---|---|---|
| `alerts_in_ua` | `city_uid`, `raion_uid`, `oblast_uid` | `region_id`, `district_id`, `oblast_id` (alerts.in.ua uses the same numbers) |
| `ubilling` | `region` | `oblast_name` |
| `alerts_com_ua` | `state_id` | none: give it yourself |

An older config that gives `city_uid` or `region` keeps that value.

Tokens are never in the config file. `setup.cmd token` saves them in the
user environment: `UKRAINEALARM_API_KEY` for `ukrainealarm` and
`ALERTS_IN_UA_TOKEN` for `alerts_in_ua`. The [sources guide](SOURCES.md)
tells how to get each token.

### `[source.request]`: the HTTP request

A preset gives these values. Give them yourself only for `json_http`, or to
change one value of a preset.

| Setting | Default | Meaning |
|---|---|---|
| `url` | Required | The URL of the JSON API. It must be `https://` |
| `method` | `"GET"` | The HTTP method |
| `headers` | `{}` | HTTP headers, for example `{ Authorization = "Bearer ${env:MY_TOKEN}" }` |
| `params` | `{}` | URL query parameters |
| `body` | none | The request body |
| `timeout_s` | Required | The time limit for one request |
| `allow_http` | `false` | `true` also allows `http://` without TLS |
| `allow_private_network` | `false` | `true` also allows LAN and localhost addresses |
| `max_response_bytes` | `1048576` | A larger response is refused |

The app runs with admin rights, so these limits apply to every request: only
`https://`, no redirects, no private addresses and a size limit. Change them
only for a server that you control.

### `[source.fields]`: where the status is in the response

JMESPath expressions (https://jmespath.org) on the JSON response:

| Field | Default | Meaning |
|---|---|---|
| `full` | Required | A true value: the alert covers the watched place |
| `partial` | none | A true value: an alert near the place, for example elsewhere in the oblast |
| `kinds` | none | A string or a list of strings: the alert kinds |
| `observed_at` | none | The time stamp of the data, for the `stale_after_s` check |
| `changed_at` | none | The time stamp of the last change. A list gives the latest time |

If `full` or `partial` gives no value (for example, a wrong region name), the
fetch fails and the LEDs show `unknown`. It is never read as "no alert".

### `[source.kinds]`, `[source.time]`, `[source.lookups.<name>]`, `[source.change_marker]`

| Setting | Default | Meaning |
|---|---|---|
| `[source.kinds] map` | `{}` | API value to kind name, for example `{ AIR = "air_raid" }`. The rules use the kind names |
| `[source.kinds] default` | none | The kind of an active alert when `kinds` gives nothing |
| `[source.kinds] unmapped` | none | The kind name for an API value that is not in `map`. Without it, the API value stays |
| `[source.time] format` | `"iso"` | `"iso"`, `"epoch"`, or a strftime pattern such as `"%Y-%m-%d %H:%M:%S"` |
| `[source.time] timezone` | `"UTC"` | The IANA time zone for time stamps without a zone, for example `"Europe/Kyiv"` |
| `[source.lookups.<name>]` | none | A request that runs once, at the first poll. The JSON of its `expression` becomes the variable `${<name>}`. It takes `url`, `expression` and any key of `[source.request]` |
| `[source.change_marker]` | none | A small request that runs at each poll. The full request runs only when the marker `field` changes, or after `max_age_s`. It takes `url`, `field`, `max_age_s` and any key of `[source.request]` |

The kind names that the shipped rules use: `air_raid`, `artillery`,
`urban_fights`, `chemical`, `nuclear`, `threat`, `other`.

## `[failover]`: the order of the sources

The app reads its sources in this order:

1. Each `[[failover.token_sources]]` entry whose token is saved.
2. `[source]`.
3. Each `[[failover.sources]]` entry.

The first source of this order is the first choice. A config without
`[failover]` gets the default token sources, and no backup. Thus it reads only
`[source]` until a token is saved. The [sources guide](SOURCES.md#the-order-of-the-sources)
explains the order.

| Setting | Default | Meaning |
|---|---|---|
| `after_s` | `30` | Use the next source when the first one had no success for this long. It must be less than `stale_after_s` |
| `retry_s` | `10` | After a failure, skip that source for this long. Keep it less than `after_s`, so that one timeout does not switch |
| `retry_max_s` | `120` | The skip time doubles after each failure in a row, up to this value |

Each `[[failover.token_sources]]` and `[[failover.sources]]` entry is one
source, in the order of use. It takes the keys of `[source]`, plus these:

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` ignores the entry |
| `poll_interval_s` | `10` | Fetch this source at most this often. Between fetches, the app uses its last status |
| `vars` | the `[source.vars]` of `[source]` | Values that replace single variables |

Without `[[failover.token_sources]]`, the default is `ukrainealarm` (1 s), then
`alerts_in_ua` (10 s). A token source without a saved token is not used, and
that is no error. A token source that is the type of `[source]` is not added
twice.

The default `[[failover.sources]]` list is empty. The shipped config has one
backup, `ubilling`, that takes `oblast_name`. A backup whose place variable is
empty is not used.

HTTP 429 never switches. A refused token (HTTP 401 or 403) switches at once,
and that source is skipped for `retry_max_s`. The app tries the first source
again after each skip time, and goes back to it when it works.

## `[[rules]]`: status to scene

Each `[[rules]]` entry maps a status to a scene. The app reads the rules from
the top, and the first rule that matches wins. Thus the order is the priority.

| Setting | Default | Meaning |
|---|---|---|
| `scene` | Required | The name of a `[scenes.<name>]` section |
| `state` | `"ok"` | `"ok"`: the app has data. `"unknown"`: no data, or old data. A status without data matches only a rule with `state = "unknown"` |
| `active` | any | `true`: an alert is active. `false`: no alert |
| `coverage` | any | `"none"`, `"partial"` or `"full"` |
| `kinds` | any | A list: the rule matches if the status has one of these kinds |

A rule with no condition matches every status that has data. Two rules are
required: one with `state = "unknown"`, and a last rule with no condition.

## `[scenes.<name>]`: what the LEDs show

Each scene runs one effect, or gives the LEDs back to their vendor apps. The
shipped scenes are `clear`, `unknown`, `alert`, `partial`, `threat`,
`artillery`, `urban_fights`, `chemical` and `nuclear`.

| Setting | Default | Meaning |
|---|---|---|
| `effect` | Required, if `release` is not set | One of the 13 effects, for example `solid` or `pulse`. The [effect table](SETUP.md#configuration) gives each effect with its options |
| `release` | `false` | `true`: each backend gives its LEDs back to its vendor app, for example to Armoury Crate. Set either `effect` or `release = true`, not the two |
| The options of the effect | the defaults of the effect | For example `color`, `brightness`, `period_s`, `min_brightness`, `max_brightness`. An unknown option, or a value out of range, is an error |

## `[display]`: frames, brightness, taking the LEDs again

| Setting | Default | Meaning |
|---|---|---|
| `brightness` | `1.0` | A global factor for every color |
| `frame_interval_s` | Required | The time between two frames. One Aura apply of all LEDs takes less than 0.1 s |
| `backend_retry_s` | Required | After a backend error, wait this long, then try again |
| `backend_close_timeout_s` | Required | At exit, wait this long for each backend to release the LEDs. An Aura release can need more than 40 s |
| `reclaim_after_wake_s` | `60` | After the display turns on, the PC resumes, or a vendor app starts: take the LEDs again for this long |
| `reclaim_step_s` | `5` | In that time, take the LEDs again this often |
| `reclaim_interval_s` | `300` | Also take the LEDs again this often at any time. `0`: never by time |
| `vendor_processes` | `["ArmouryCrate.exe", "LightingService.exe", "AacAmbientLighting.exe"]` | A new process with one of these names starts the reclaim window. `[]`: do not watch |
| `vendor_poll_s` | `2` | Read the process list this often |

Each reclaim adds lines to the ASUS log files, so keep `reclaim_interval_s`
at 300 s or more.

## `[[lighting]]`: the hardware backends

Each `[[lighting]]` entry is one backend. These settings apply to all backends:

| Setting | Default | Meaning |
|---|---|---|
| `type` | Required | `aura_sdk` or `razer_chroma`. It must be in the build. If it is not, the app logs a warning and skips the entry |
| `enabled` | `true` | `false` ignores the entry |
| `brightness` | `1.0` | A brightness factor for this backend only |
| `color_correction` | `[1, 1, 1]` | `[r, g, b]` factors for the LEDs of this backend. Use it when a color looks wrong, for example orange that looks yellow |

**`aura_sdk`** (ASUS Aura, Armoury Crate):

| Setting | Default | Meaning |
|---|---|---|
| `include` | Required | Device-name patterns, for example `["*"]`, `["Mainboard*", "AddressableStrip *"]`. `setup.cmd` and the `devices` command show the names |
| `led_counts` | `{}` | The real LED count of a header, for example `{ "AddressableStrip 1" = 30 }`. An ARGB header reports its maximum (120), so a per-LED effect spans the real strip only with this value |

**`razer_chroma`** (Razer Synapse, Chroma SDK):

| Setting | Default | Meaning |
|---|---|---|
| `url` | Required | The local Chroma SDK server, `http://localhost:54235/razer/chromasdk` |
| `devices` | Required | The device kinds to drive, for example `["keyboard", "mouse"]` |
| `app_title` | Required | The app name that Synapse shows |
| `heartbeat_interval_s` | Required | Send a heartbeat this often. The SDK closes a session after 15 s without requests |
| `init_delay_s` | Required | Wait this long after a new session, before the first effect |
| `timeout_s` | Required | The time limit for one request to the SDK |

## `[tuning]`: the configurator file

| Setting | Default | Meaning |
|---|---|---|
| `file` | Required in the section | The tuning file that the configurator writes. `%NAME%` takes an environment variable |
| `check_interval_s` | Required in the section | The app checks the file for changes this often |

Without the `[tuning]` section, the app reads no tuning file.

## `[service]` and `[logging]`

| Setting | Default | Meaning |
|---|---|---|
| `[service] stop_event` | Required | The name of the Windows event that stops the app. `setup.cmd` and the `stop` command use it. Keep the single quotes |
| `[logging] level` | Required | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `[logging] file` | Required | The log file name. It is always in the folder of the config file: an absolute path or `..` is not allowed |
| `[logging] max_bytes` | Required | At this size, the app starts a new log file |
| `[logging] backups` | Required | The number of old log files to keep |

## `tuning.json`

The configurator writes this file, and you can also edit it. The app accepts
only these four parts, and it rejects a file with any other content:

```json
{
  "scenes":   {"<scene name>": {"effect": "pulse", "color": "#FF0000"}},
  "display":  {"brightness": 0.8},
  "lighting": {"<backend type>": {"brightness": 1.0, "color_correction": [1.0, 0.69, 0.94]}},
  "preview":  {"scene": "<scene name>", "until": 1767225600}
}
```

| Part | Meaning |
|---|---|
| `scenes` | Replaces the settings of scenes that exist in `config.toml`. It cannot add a scene |
| `display` | Only `brightness` |
| `lighting` | For each backend type: `brightness` and `color_correction` |
| `preview` | Shows one scene until the Unix time `until`. An optional `definition` gives a scene for the preview only |

The file can never change the source, the URLs, the rules or the log path. If
the file has an error, the app logs a warning and keeps the last valid tuning.

## `build.toml`

| Setting | Default | Meaning |
|---|---|---|
| `[build] name` | Required | The name of the exe |
| `[build] lighting` | Required | The lighting modules: `aura_sdk`, `razer_chroma`. `[]`: no hardware, the app only logs |
| `[build] sources` | Required | The source presets: `siren_pp_ua`, `ukrainealarm`, `ubilling`, `alerts_in_ua`, `alerts_com_ua` |

The core is always in the build: `json_http`, the rules, the scenes, failover
and the `console` backend. `setup.cmd build -Lighting <list> -Sources <list>`
replaces a list for one build. A build without `aura_sdk` runs without admin
rights.

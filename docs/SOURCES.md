# Alert sources

The alert sources that alert-colors can read, the order in which the app uses
them, and how to get and save a token for a service that needs one. The
[configuration reference](CONFIG.md#failover-the-order-of-the-sources) lists
every source setting.

## Contents

- [Which source to use](#which-source-to-use)
- [The order of the sources](#the-order-of-the-sources)
- [Save a token](#save-a-token)
- [`siren_pp_ua`](#siren_pp_ua)
- [`ukrainealarm`](#ukrainealarm)
- [`alerts_in_ua`](#alerts_in_ua)
- [`ubilling`](#ubilling)
- [`alerts_com_ua`](#alerts_com_ua)
- [`json_http`](#json_http)

## Which source to use

| Source | Smallest area | Alert types | Token | Poll interval | Its place in the order |
|---|---|---|---|---|---|
| `ukrainealarm` | Community (hromada) | Yes | Yes: API key | 1 s | 1, when its token is saved |
| `alerts_in_ua` | Community (hromada) | Yes | Yes: token | 10 s | 2, when its token is saved |
| `siren_pp_ua` | Community (hromada) | Yes | No | 1 s | `[source]`: the default |
| `ubilling` | Oblast | No | No | 10 s | The last backup |
| `alerts_com_ua` | Oblast | No | No | — | Do not use (see below) |
| `json_http` | Any | Any | Any | Any | Your own JSON API |

A community-level source shows the `partial` scene (orange) for an alert
elsewhere in the oblast. An oblast-level source cannot: an alert anywhere in
the oblast shows the `alert` scene (red).

You need to do nothing to use these sources. Choose the place one time with
`setup.cmd region`. If you have a token, save it with `setup.cmd token`.

## The order of the sources

The app puts the best working data first:

1. Each service with a token whose token is saved, in the order of the
   table above.
2. `[source]` of the config: `siren_pp_ua` by default.
3. Each backup of `[[failover.sources]]`: `ubilling` by default.

The app reads the first source. It uses the next one only when the first
source had no data for `after_s` (30 s), or at once when the server refused
the token. HTTP 429 never switches. The app tries the first source again at
intervals, and goes back to it when it works. The log shows `using fallback
<name>` and `<name> works again`.

Each source is fetched at most once each poll interval of the table. Thus a
service with a low request limit stays below its limit, also when it is first.

**The place.** `setup.cmd region` writes the place one time for all sources:
`region_id`, `district_id`, `oblast_id` and `oblast_name`. The location UIDs of
alerts.in.ua are the same numbers as the region IDs of ukrainealarm.com
(checked: 1604 IDs in the published UID table of alerts.in.ua, each for the
same place). So no service needs its own IDs.

To change the order, or the poll interval of a service, see
[`[failover]`](CONFIG.md#failover-the-order-of-the-sources).

## Save a token

The token tool saves the token in the environment of your Windows account,
never in a config file. You can thus share your config.

1. Run `setup.cmd`, then choose **9** (Tokens). You can also run `setup.cmd token`.
2. Type the number of the service.
3. Paste the token, then press Enter. The input is hidden.

The tool then:
- Tests the token with one request.
- Saves the token if it works. A token that the server refuses (HTTP 401 or
  403) is not saved. If the server does not answer, the token is saved
  anyway, and the tool tells you so.
- Restarts the installed app, so that the app puts the service first.

No sign-out is necessary: the app also reads the token from the registry
(`HKEY_CURRENT_USER\Environment`).

To remove a token, choose the service, then press Enter with no token. To
replace a token, save the new one.

If the server refuses a saved token later, the app logs `the server refused
the token`, uses the next source at once, and tries again after
`retry_max_s`. Save the token again with `setup.cmd token`.

## `siren_pp_ua`

A public mirror of the official ukrainealarm.com API at https://siren.pp.ua.
It is the fastest source without a token: 1 to 3 s from a change to the LEDs.

- **Token:** not necessary.
- **Limit:** approximately 1 request per second for each IP address. A second
  program on the same PC that polls it gets HTTP 429.
- **Example:** `examples\sources\siren_pp_ua.toml`.

A third party runs the mirror, and it can be down. The default backup
`ubilling` keeps the LEDs correct during that time.

## `ukrainealarm`

The official API of the "Повітряна тривога" app at https://api.ukrainealarm.com.
It has the same data and the same region IDs as `siren_pp_ua`.

**To get the API key:**

1. Open https://api.ukrainealarm.com in a browser.
2. Fill in the request form for an API key. Tell what the project does and how often it polls.
3. Wait until you get the key.
4. Save it with `setup.cmd token` (see [Save a token](#save-a-token)).

The site is behind Cloudflare: open it in a normal browser, not with a script.

- **How the app sends it:** the HTTP header `Authorization: <key>`.
- **Environment variable:** `UKRAINEALARM_API_KEY`.
- **Example:** `examples\sources\ukrainealarm.toml`.

## `alerts_in_ua`

The API of https://alerts.in.ua.

**To get the token:**

1. Fill in the request form at https://alerts.in.ua/api-request.
2. Wait until they send you the token.
3. Save it with `setup.cmd token` (see [Save a token](#save-a-token)).

The developer page https://devs.alerts.in.ua does not say if the token is
free. Volunteers run the project. In the form, tell what the project does and
how often it polls. For example: a non-commercial open-source program on one
home PC, one request each 10 s.

- **How the app sends it:** the HTTP header `Authorization: Bearer <token>`.
- **Environment variable:** `ALERTS_IN_UA_TOKEN`.
- **Limit:** 8 to 10 requests per minute for each IP address, never more than
  12. A request above the limit gets HTTP 429. If you go over the limit often,
  they can block the IP address and the token. The app polls it each 10 s.
- **Example:** `examples\sources\alerts_in_ua.toml`.

## `ubilling`

The feed at https://ubilling.net.ua/aerialalerts/.

- **Token:** not necessary.
- **Place:** the oblast name. It takes `oblast_name`, which `setup.cmd region`
  writes. An older config with `region = "<oblast name>"` keeps working.
- **Limit:** at 1 s polling, it sends HTTP 429 sometimes. The app polls it each 10 s.
- **Example:** `examples\sources\ubilling.toml`.

While `oblast_name` is empty (the test region of the shipped config), the app
does not use `ubilling`.

## `alerts_com_ua`

The API of https://alerts.com.ua. No token is necessary, and it has oblast
level only. The service stopped updating its data (checked 2026-11-08). Do not
use it until the service works again.

## `json_http`

Any other JSON API. You give the request and the JMESPath fields yourself. See
`examples\sources\custom_json_http.toml` and the
[configuration reference](CONFIG.md#sourcerequest-the-http-request). If the
API needs a token, put it in an environment variable and write
`${env:NAME}` in a header:

```toml
[source.request]
headers = { Authorization = "Bearer ${env:MY_API_TOKEN}" }
```

The token tool knows only the services of this page. Save another token with
`setx MY_API_TOKEN "<token>"`. The app also reads it from the registry, so no
sign-out is necessary.

"""Command line: run | stop | check | status | devices | preview | tune | region."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from . import power_events, process_events, stop_signal
from .http import RateLimitedError
from .config import DEFAULT_CONFIG, App
from .frame import Frame

log = logging.getLogger("alert_colors")
FILE_LOG_COMMANDS = ("run", "preview")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alert-colors", description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="watch alerts and drive the LEDs")
    run.add_argument("--dry-run", action="store_true", help="log colors instead of using hardware")
    commands.add_parser("stop", help="ask the running daemon to release the LEDs and exit")
    commands.add_parser("check", help="validate the config and fetch once; exit 1 on any error")
    commands.add_parser("status", help="fetch the alert status once and print the scene")
    commands.add_parser("devices", help="list the devices of each enabled backend")
    tune = commands.add_parser("tune", help="open the configurator (live tuning in the browser)")
    tune.add_argument("--no-browser", action="store_true", help="print the URL only")
    region = commands.add_parser("region", help="choose the watched city or community (writes config.toml)")
    region.add_argument("--id", help="set this region id without questions")
    preview = commands.add_parser("preview", help="show one scene on the hardware")
    preview.add_argument("scene")
    preview.add_argument("--seconds", type=float, required=True)

    args = parser.parse_args(argv)
    app = App.load(args.config)
    # Only the long-running commands write the log file. The configurator and the
    # checks run with normal rights and cannot write the log of the installed app.
    app.configure_logging(to_file=args.command in FILE_LOG_COMMANDS)
    handlers = {"run": _run, "stop": _stop, "check": _check, "status": _status,
                "devices": _devices, "preview": _preview, "tune": _tune,
                "region": _region}
    return handlers[args.command](app, args)


def _stop_event() -> threading.Event:
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    if sys.platform == "win32":  # console close, logoff, shutdown
        try:
            import win32api

            win32api.SetConsoleCtrlHandler(lambda _event: stop.set() or True, True)
        except Exception:  # noqa: BLE001  no console under pythonw
            pass
    return stop


def _run(app: App, args: argparse.Namespace) -> int:
    stop = _stop_event()
    handle = stop_signal.watch(app.stop_event_name(), stop)  # noqa: F841  keep alive
    daemon = app.daemon(dry_run=args.dry_run)
    stop_power_events = power_events.watch(daemon.request_reclaim)
    names, poll_s = app.vendor_watch()
    stop_process_events = process_events.watch(
        names, poll_s, lambda name: daemon.request_reclaim(f"{name} started"))
    try:
        daemon.run(stop)
    finally:
        stop_process_events()
        stop_power_events()
    return 0


def _stop(app: App, args: argparse.Namespace) -> int:
    if stop_signal.request(app.stop_event_name()):
        print("stop requested")
        return 0
    print("no running daemon")
    return 1


CHECK_ATTEMPTS = 5
CHECK_FIRST_WAIT_S = 2.0


def _fetch_with_retry(source):  # noqa: ANN001, ANN202
    """HTTP 429 means the server works and limits the rate (the running app also
    polls it): wait and try again, so the check does not fail for that reason."""
    wait_s = CHECK_FIRST_WAIT_S
    for attempt in range(1, CHECK_ATTEMPTS + 1):
        try:
            return source.fetch()
        except RateLimitedError as error:
            if attempt == CHECK_ATTEMPTS:
                raise
            delay = error.retry_after_s or wait_s
            print(f"rate limited (HTTP 429), try {attempt + 1}/{CHECK_ATTEMPTS} in {delay:.0f} s")
            time.sleep(delay)
            wait_s *= 2
    raise AssertionError("unreachable")


def _check(app: App, args: argparse.Namespace) -> int:
    """The gate before an install: a config that cannot fetch is never deployed.

    Each source is fetched once: the primary, then each fallback of [failover].
    A config error in any source fails the check. A network error (the server
    is down) fails the check only when no source gives a status: the install
    of a fallback must be possible while the primary server is down.
    """
    try:
        scenes = app.scenes()
        policy = app.policy()
        app.settings()
        entries = app.lighting_entries()
        sources = [app.source(), *app.fallback_sources()]
    except Exception as error:  # noqa: BLE001  report every kind of config fault
        print(f"CHECK FAILED: {type(error).__name__}: {error}")
        return 1
    first_status = None
    for source in sources:
        try:
            status = _fetch_with_retry(source)
        except OSError as error:  # the network or the server: maybe another source works
            if len(sources) == 1:
                print(f"CHECK FAILED: {type(error).__name__}: {error}")
                return 1
            print(f"source {source.name}: NOT REACHABLE: {type(error).__name__}: {error}")
            continue
        except Exception as error:  # noqa: BLE001  a config fault: never deploy it
            print(f"CHECK FAILED: source {source.name}: {type(error).__name__}: {error}")
            return 1
        print(f"source {source.name}: {status}")
        first_status = first_status or status
    if first_status is None:
        print("CHECK FAILED: no source gives a status")
        return 1
    print(f"scene: {policy.select(first_status)} (scenes: {len(scenes)}, lighting: "
          f"{', '.join(e['type'] for e in entries) or 'none'})")
    print("CHECK OK")
    return 0


def _tune(app: App, args: argparse.Namespace) -> int:
    from .tuner import Tuner

    stop = threading.Event()
    handle = stop_signal.watch(app.stop_event_name(), stop)  # noqa: F841  keep alive
    Tuner(app).serve(open_browser=not args.no_browser, stop=stop)
    return 0


MAX_LISTED_PLACES = 20
EXIT_UNCHANGED = 3  # setup.cmd: the place did not change, no restart is necessary


def _region(app: App, args: argparse.Namespace) -> int:
    from . import regions

    try:
        all_places = regions.places(regions.fetch_tree(app.source()))
    except regions.RegionError as error:
        print(error)
        return EXIT_UNCHANGED
    old = regions.current(app.raw["source"].get("vars", {}), all_places)
    print(f"Watched place now: {old.path if old else 'not set'}")
    if args.id:
        chosen = regions.find(all_places, args.id)
        if chosen is None:
            print(f"No place has the id {args.id}.")
            return 1
    else:
        chosen = _ask_place(all_places)
    if chosen is None or chosen == old:
        print("The watched place did not change.")
        return EXIT_UNCHANGED
    text = args.config.read_text(encoding="utf-8")
    args.config.write_text(regions.set_place(text, chosen), encoding="utf-8", newline="")
    print(f"Watched place: {chosen.path} (region_id {chosen.region_id}). Saved in {args.config}.")
    return 0


def _ask_place(all_places):  # noqa: ANN001, ANN202
    from . import regions

    while True:
        text = input("Type a part of the city or community name (Ukrainian or Latin), "
                     "or press Enter to keep it: ").strip()
        if not text:
            return None
        found = regions.search(all_places, text)
        if not found:
            print("No match. Try a shorter part of the name.")
            continue
        for number, place in enumerate(found[:MAX_LISTED_PLACES], start=1):
            print(f"  {number:2}. {place.path}")
        if len(found) > MAX_LISTED_PLACES:
            print(f"  ... {len(found) - MAX_LISTED_PLACES} more. Type more of the name to see them.")
        answer = input("Number of the place, or press Enter to search again: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= min(len(found), MAX_LISTED_PLACES):
            return found[int(answer) - 1]


def _status(app: App, args: argparse.Namespace) -> int:
    fallbacks = app.fallback_sources()
    if not fallbacks:  # no [failover]: the same output as before
        status = app.source().fetch()
        print(status)
        print("scene:", app.policy().select(status))
        return 0
    policy, any_ok = app.policy(), False
    for source in [app.source(), *fallbacks]:
        try:
            status = source.fetch()
        except Exception as error:  # noqa: BLE001  show each source
            print(f"source {source.name}: FAILED: {type(error).__name__}: {error}")
            continue
        print(f"source {source.name}: {status}")
        print(f"  scene: {policy.select(status)}")
        any_ok = True
    return 0 if any_ok else 1


def _devices(app: App, args: argparse.Namespace) -> int:
    for backend in app.backends():
        print(f"{backend.name}:")
        try:
            for line in backend.describe():
                print("   ", line)
        except Exception as error:  # noqa: BLE001
            print("    unavailable:", error)
    return 0


def _preview(app: App, args: argparse.Namespace) -> int:
    # Same worker threads as `run`: the Aura SDK on the main thread blocks
    # interpreter exit after the release (seen on 2026-11-08).
    scene = app.scenes()[args.scene]
    if scene.release:
        print(f"scene {args.scene!r} only releases the LEDs: nothing to preview")
        return 1
    outputs = app.outputs()
    frame_s = app.settings().frame_interval_s
    stop = _stop_event()
    started = time.monotonic()
    try:
        while not stop.is_set() and (elapsed := time.monotonic() - started) < args.seconds:
            frame = Frame(scene.effect, elapsed)
            for output in outputs:
                output.show(frame)
            stop.wait(frame_s)
    finally:
        for output in outputs:
            output.close()  # close() releases the LEDs first
    return 0

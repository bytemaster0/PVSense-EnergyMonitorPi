#!/usr/bin/env python3
# main.py — Solar Energy Monitor — entry point
#
# Runs on Raspberry Pi Zero W, driving the Waveshare 7.5" HD 3-colour e-ink
# display on two independent refresh cadences:
#
#   Every 2 minutes (REALTIME_INTERVAL_SECONDS):
#       • Fetch instantaneous solar W + home consumption W from Sense
#       • Re-draw the display with fresh live readings
#
#   Every 5 minutes (UPDATE_INTERVAL_SECONDS):
#       • Also fetch PVOutput 5-min interval data (for the graph)
#       • Also fetch Sense daily stats (peak, cumulative energy)
#       • Update the on-disk cache for reboot resilience
#
# Reboot resilience
# ─────────────────
# data_cache.py persists the last known-good daily data to cache.json.
# On reboot the display shows real (marked-stale) figures immediately
# rather than zeros while waiting for WiFi + APIs.  Realtime readings
# are never cached — showing a stale instantaneous wattage is misleading.
#
# Usage:
#   python main.py              # normal operation
#   python main.py --dry-run    # render preview.png without touching hardware
#   python main.py --once       # single full refresh then exit

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime

import config
import data_cache
import display_renderer
import pvoutput_client
import sense_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

# Sentinel for "no realtime data yet" so the renderer shows "—" rather than "0 W"
_NO_REALTIME = {"solar_w": 0, "consumption_w": 0, "valid": False}


def do_full_refresh(cache: dict) -> tuple[dict, dict, bool, dict]:
    """
    Fetch PVOutput interval data and Sense daily stats.

    get_daily_solar_stats() already calls update_realtime() internally, so
    the returned dict includes current solar_w and consumption_w at no extra
    API cost.  We extract those here as ``realtime`` and strip them before
    passing sense_daily to the cache (instantaneous readings must not be
    persisted).

    Returns (pvo, sense_daily, is_stale, realtime).
    """
    log.info("── Full refresh (PVOutput + Sense daily) ──")
    live_pvo         = pvoutput_client.fetch_day_data()
    live_sense_daily = sense_client.get_daily_solar_stats()

    # Pull out the free realtime piggyback before merging / caching
    realtime = {
        "solar_w":       live_sense_daily.pop("solar_w",       0),
        "consumption_w": live_sense_daily.pop("consumption_w", 0),
        "valid":         live_sense_daily.get("valid", False),
    }

    pvo, sense_daily, is_stale = data_cache.merge(live_pvo, live_sense_daily, cache)

    if live_pvo.get("valid") or live_sense_daily.get("valid"):
        data_cache.save(cache, live_pvo, live_sense_daily)

    merged_peak = max(pvo.get("peak_w", 0), sense_daily.get("peak_w", 0))
    log.info(
        "Daily stats — PVOutput: %d Wh / %d W peak  |  Sense: %d Wh / %d W peak"
        "  |  merged peak: %d W  |  stale=%s",
        pvo.get("total_wh", 0), pvo.get("peak_w", 0),
        sense_daily.get("total_wh", 0), sense_daily.get("peak_w", 0),
        merged_peak, is_stale,
    )
    return pvo, sense_daily, is_stale, realtime


def run_loop(dry_run: bool, cache: dict) -> None:
    """
    Main event loop.

    Sleeps REALTIME_INTERVAL_SECONDS between iterations.  Triggers a full
    refresh (PVOutput + daily Sense) whenever UPDATE_INTERVAL_SECONDS have
    elapsed since the last one.
    """
    # Initialise daily state from cache so the first render has real numbers
    pvo         = cache["pvo"]
    sense_daily = cache["sense"]
    is_stale    = cache["pvo"].get("total_wh", 0) > 0  # warm cache → show as stale until confirmed live

    last_full_at: float = 0.0   # monotonic; 0 forces a full refresh on first iteration
    cache_date: str = cache.get("date", "")

    while True:
        now_mono = time.monotonic()

        # At midnight, discard yesterday's in-memory cache so peaks, totals,
        # and intervals from the previous day don't bleed into the new day.
        today = date.today().isoformat()
        if today != cache_date:
            log.info("Day rollover detected (%s → %s) — resetting cache", cache_date, today)
            cache = data_cache.load()   # returns a fresh empty-today structure
            cache_date = today
            pvo         = cache["pvo"]
            sense_daily = cache["sense"]
            is_stale    = False
            last_full_at = 0.0          # force a full refresh immediately

        # Full refresh due?
        if (now_mono - last_full_at) >= config.UPDATE_INTERVAL_SECONDS:
            try:
                pvo, sense_daily, is_stale, realtime = do_full_refresh(cache)
            except Exception as exc:
                log.exception("Full refresh failed: %s", exc)
                realtime = _NO_REALTIME
            last_full_at = time.monotonic()
        else:
            # Realtime-only: one Sense call, no PVOutput or daily stats
            log.info("── Realtime fetch (Sense) ──")
            try:
                realtime = sense_client.get_realtime_power()
            except Exception as exc:
                log.error("Realtime fetch failed: %s", exc)
                realtime = _NO_REALTIME

        # Track running daily peak from realtime readings.
        # Sense samples at sub-second resolution, so this is more accurate
        # than anything available from PVOutput or the trends endpoint.
        if realtime.get("valid"):
            data_cache.update_realtime_peak(cache, realtime.get("solar_w", 0))

        display_renderer.render(
            intervals   = pvo.get("intervals", []),
            pvo_daily   = pvo,
            sense_daily = sense_daily,
            realtime    = realtime,
            updated_at  = datetime.now(),
            is_stale    = is_stale,
            dry_run     = dry_run,
        )

        log.info("Sleeping %d s…", config.REALTIME_INTERVAL_SECONDS)
        try:
            time.sleep(config.REALTIME_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            log.info("Interrupted — exiting")
            sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Solar Energy Monitor")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Render preview.png instead of driving the EPD hardware",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Perform a single full refresh + realtime fetch then exit",
    )
    args = parser.parse_args()

    cache = data_cache.load()
    if cache["pvo"].get("total_wh", 0) > 0:
        log.info(
            "Warm cache (written %s): %d Wh, peak %d W",
            cache.get("written_at", "?"),
            cache["pvo"]["total_wh"],
            max(cache["pvo"].get("peak_w", 0), cache["sense"].get("peak_w", 0)),
        )

    if args.once or args.dry_run:
        # Single cycle: full refresh + realtime, then exit
        pvo, sense_daily, is_stale, realtime = do_full_refresh(cache)
        display_renderer.render(
            intervals   = pvo.get("intervals", []),
            pvo_daily   = pvo,
            sense_daily = sense_daily,
            realtime    = realtime,
            updated_at  = datetime.now(),
            is_stale    = is_stale,
            dry_run     = args.dry_run,
        )
        return

    log.info(
        "Solar Energy Monitor starting — realtime every %d s, full refresh every %d s",
        config.REALTIME_INTERVAL_SECONDS,
        config.UPDATE_INTERVAL_SECONDS,
    )
    try:
        run_loop(dry_run=False, cache=cache)
    except KeyboardInterrupt:
        log.info("Interrupted — exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()

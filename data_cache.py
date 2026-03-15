# data_cache.py — persist last-known-good data across reboots
#
# Why this exists
# ───────────────
# The Pi Zero W is an embedded device that can be unplugged at any time.
# On reboot it may take 30–90 s for WiFi to associate and APIs to respond.
# Without a cache the display would show zeros until both APIs succeed.
# With a cache it shows the last real data (clearly marked as stale) until
# fresh data arrives.
#
# Cache validity rules
# ────────────────────
# • The cache is keyed to a calendar date.  At midnight the old cache is
#   ignored (returns zeros) so yesterday's numbers never bleed into today.
# • Each data source (PVOutput, Sense) is cached independently.  If only
#   Sense fails, PVOutput data is still live and vice-versa.
# • Writes are atomic (write tmp → os.replace) so a power-cut mid-write
#   cannot leave a corrupt file.

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Cache lives next to the script so no extra directory setup is needed.
CACHE_PATH = Path(__file__).parent / "cache.json"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _empty_pvo() -> dict:
    return {"intervals": [], "total_wh": 0, "peak_w": 0, "valid": False}


def _empty_sense() -> dict:
    return {"peak_w": 0, "total_wh": 0, "valid": False}


def _today() -> str:
    return date.today().isoformat()   # "YYYY-MM-DD"


# ── Public API ────────────────────────────────────────────────────────────────

def load() -> dict:
    """
    Load the cache from disk.

    Returns a dict::

        {
            "date":         "YYYY-MM-DD",   # date the cache was written
            "written_at":   "HH:MM",        # wall-clock time of last save
            "pvo":          { ... },        # last good PVOutput data
            "sense":        { ... },        # last good Sense data
        }

    If the file is missing, unreadable, or from a previous day the cache
    sources are replaced with empty/zero dicts so callers always get a
    consistent structure.
    """
    today = _today()
    try:
        with open(CACHE_PATH, "r") as f:
            cache = json.load(f)
    except FileNotFoundError:
        log.info("No cache file found — starting fresh")
        cache = {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Cache file unreadable (%s) — starting fresh", exc)
        cache = {}

    if cache.get("date") != today:
        if cache.get("date"):
            log.info("Cache is from %s — discarding (new day)", cache["date"])
        # Return empty-but-valid structure for today
        return {
            "date":       today,
            "written_at": None,
            "pvo":        _empty_pvo(),
            "sense":      _empty_sense(),
        }

    # Ensure expected keys exist even if the file is from an older version
    cache.setdefault("pvo",   _empty_pvo())
    cache.setdefault("sense", _empty_sense())
    log.info(
        "Cache loaded (written %s): PVOutput %d Wh, Sense peak %d W",
        cache.get("written_at", "?"),
        cache["pvo"].get("total_wh", 0),
        cache["sense"].get("peak_w", 0),
    )
    return cache


def save(cache: dict, pvo: dict, sense: dict) -> None:
    """
    Atomically update the cache with fresh data from either source.

    Only overwrites a source's entry if that source reported ``valid=True``,
    so a single API failure never clears previously cached good data.
    """
    now_str = datetime.now().strftime("%H:%M")
    updated = dict(cache)          # shallow copy; we replace top-level keys
    updated["date"]       = _today()
    updated["written_at"] = now_str

    if pvo.get("valid"):
        updated["pvo"] = pvo
    if sense.get("valid"):
        updated["sense"] = sense

    _atomic_write(updated)


def merge(live_pvo: dict, live_sense: dict, cache: dict) -> tuple[dict, dict, bool]:
    """
    Combine live API results with cached fallback.

    Peak power is treated as a running daily maximum: once a non-zero peak is
    seen during the day it is preserved in the cache, so a nighttime reading
    that returns 0 W (no live intervals, inverters off) can never overwrite the
    real daytime peak captured earlier.

    Returns ``(pvo, sense, is_stale)`` where *is_stale* is True when at
    least one source is serving cached rather than live data.
    """
    stale = False

    if live_pvo.get("valid"):
        cached_pvo = cache.get("pvo", {})

        # Peak is a running daily maximum — never let a nighttime 0 erase a
        # real daytime peak that was captured earlier in the day.
        live_pvo["peak_w"] = max(
            live_pvo.get("peak_w", 0), cached_pvo.get("peak_w", 0)
        )

        # Intervals only grow during the day; getstatus.jsp returns fewer
        # entries at night as the rolling buffer ages out.  Preserve the
        # richer cached set so the graph doesn't go blank after sunset.
        cached_intervals = cached_pvo.get("intervals", [])
        live_intervals   = live_pvo.get("intervals", [])
        if len(cached_intervals) > len(live_intervals):
            log.info(
                "PVOutput: keeping %d cached intervals (live has only %d at night)",
                len(cached_intervals), len(live_intervals),
            )
            live_pvo["intervals"] = cached_intervals

        pvo = live_pvo
    else:
        pvo = cache.get("pvo", _empty_pvo())
        if pvo.get("total_wh", 0) > 0 or pvo.get("intervals"):
            log.info("PVOutput: using cached data from %s", cache.get("written_at", "?"))
            stale = True

    if live_sense.get("valid"):
        # Same running-maximum logic for Sense peak.
        cached_sense_peak = cache.get("sense", {}).get("peak_w", 0)
        live_sense["peak_w"] = max(live_sense.get("peak_w", 0), cached_sense_peak)
        sense = live_sense
    else:
        sense = cache.get("sense", _empty_sense())
        if sense.get("peak_w", 0) > 0:
            log.info("Sense: using cached data from %s", cache.get("written_at", "?"))
            stale = True

    return pvo, sense, stale


def update_realtime_peak(cache: dict, solar_w: int) -> None:
    """
    Update the cached Sense peak immediately if *solar_w* is a new daily high.

    Called after every realtime Sense fetch so the peak accumulates throughout
    the day even when Sense's trends endpoint doesn't expose it directly.
    Only writes to disk when the peak actually improves.
    """
    if solar_w <= 0:
        return
    sense   = cache.setdefault("sense", _empty_sense())
    current = sense.get("peak_w", 0)
    if solar_w > current:
        sense["peak_w"] = solar_w
        log.info("Realtime peak updated: %d W → %d W", current, solar_w)
        _atomic_write({**cache, "date": _today(),
                       "written_at": datetime.now().strftime("%H:%M")})


# ── Atomic write ──────────────────────────────────────────────────────────────

def _atomic_write(data: dict) -> None:
    """
    Write *data* to CACHE_PATH via a temp file + os.replace().

    os.replace() is atomic on POSIX (Linux/macOS): the file is either fully
    written or untouched — a mid-write power cut cannot corrupt the cache.
    Intervals are stripped of datetime objects before serialisation.
    """
    # Convert datetime objects to strings for JSON serialisation
    serialisable = _prepare_for_json(data)

    dir_ = CACHE_PATH.parent
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8"
        ) as f:
            json.dump(serialisable, f, indent=2)
            tmp_path = f.name
        os.replace(tmp_path, CACHE_PATH)
        log.debug("Cache saved to %s", CACHE_PATH)
    except OSError as exc:
        log.error("Failed to write cache: %s", exc)
        # Non-fatal — next successful cycle will try again


def _prepare_for_json(obj):
    """Recursively convert datetime → ISO string so json.dump() doesn't choke."""
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="minutes")
    if isinstance(obj, dict):
        return {k: _prepare_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_prepare_for_json(i) for i in obj]
    return obj

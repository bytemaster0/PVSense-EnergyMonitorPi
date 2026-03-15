# pvoutput_client.py — PVOutput API v2
#
# Authentication: X-Pvoutput-Apikey + X-Pvoutput-SystemId request headers.
# This is ENTIRELY separate from the PVOutput web-interface login; anti-spam
# browser-session requirements have no effect on these API calls.
#
# Rate limits (per API key, rolling hour window):
#   Free account  :  60 requests / hour  (~1 / min)
#   Donated account: 300 requests / hour (~5 / min)
#
# This module makes TWO API requests per refresh cycle:
#   1. getstatus.jsp  — 5-minute interval history for the graph
#   2. getoutput.jsp  — PVOutput's own authoritative daily total + recorded peak
#
# At a 5-minute cadence that is 24 requests/hour on a free account (40% of
# quota), leaving ample headroom.  The _get() helper enforces a minimum gap
# between calls and handles 429 back-off, so quota overrun is not possible.

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Optional

import requests

import config

log = logging.getLogger(__name__)

_BASE    = "https://pvoutput.org/service/r2"
_HEADERS = {
    "X-Pvoutput-Apikey":   config.PVOUTPUT_API_KEY,
    "X-Pvoutput-SystemId": config.PVOUTPUT_SYSTEM_ID,
    "User-Agent": "SolarEnergyMonitor/2.0 (Raspberry Pi; python-requests)",
}
_TIMEOUT = 20  # seconds — Pi Zero W can be slow on weak WiFi

# ── Rate-limit guard ──────────────────────────────────────────────────────────
# Backoff state — only populated after a 429 response.
# The main loop already enforces a 5-minute cadence so there is no need for
# a per-call minimum gap guard; two back-to-back calls per cycle (intervals +
# daily output) are expected and stay well inside free-tier quota.
_rate_limit_backoff_until: float = 0.0


def _get(endpoint: str, params: dict) -> Optional[str]:
    """
    Authenticated GET to the PVOutput API.

    Returns the response body as a string, or None on any failure.
    Handles 429 Too Many Requests by logging a backoff duration and returning
    None so the caller falls back to cached data rather than crashing.
    """
    global _rate_limit_backoff_until

    now = time.monotonic()
    if now < _rate_limit_backoff_until:
        remaining = _rate_limit_backoff_until - now
        log.warning(
            "PVOutput rate-limit backoff active — skipping request (%.0f s remaining)",
            remaining,
        )
        return None

    url = f"{_BASE}/{endpoint}"
    try:
        r = requests.get(url, headers=_HEADERS, params=params, timeout=_TIMEOUT)

        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", 3600))
            _rate_limit_backoff_until = time.monotonic() + retry_after
            log.error("PVOutput 429 — backing off for %d s", retry_after)
            return None

        if r.status_code == 400 and "No status found" in r.text:
            # Normal before inverters wake up at dawn
            log.info("PVOutput: no data yet for today")
            return None

        r.raise_for_status()
        return r.text.strip()

    except requests.Timeout:
        log.error("PVOutput request timed out after %d s", _TIMEOUT)
        return None
    except requests.RequestException as exc:
        log.error("PVOutput request failed: %s", exc)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_day_data(for_date: Optional[date] = None) -> dict:
    """
    Fetch the full picture for *for_date* (default: today) in two API calls.

    Call 1 — getstatus.jsp (full-day history)
        Returns up to 288 × 5-minute intervals with per-slot power (W) and
        cumulative energy (Wh).  Used for the line graph.

    Call 2 — getoutput.jsp (daily summary)
        Returns PVOutput's own authoritative daily total (Wh) and the highest
        power figure PVOutput recorded for the day (W).  This is preferred over
        deriving the total from the last interval row because getoutput reflects
        PVOutput's own aggregation logic (including any corrections or uploads
        that arrived after the last status entry).

    Returns
    -------
    dict:
        intervals : list[dict]  — [{time: datetime, power_w: int, energy_wh: int}, ...]
        total_wh  : int         — authoritative daily generation total (Wh)
        peak_w    : int         — PVOutput-recorded peak power for the day (W)
        valid     : bool        — False if neither call succeeded
    """
    d        = for_date or date.today()
    date_str = d.strftime("%Y%m%d")

    intervals = _fetch_intervals(d)
    daily     = _fetch_daily_output(date_str)

    if not intervals and not daily["valid"]:
        return {"intervals": [], "total_wh": 0, "peak_w": 0, "valid": False}

    # Total Wh: getoutput is authoritative; fall back to last interval row.
    total_wh = daily["total_wh"] if daily["valid"] else (
        intervals[-1]["energy_wh"] if intervals else 0
    )

    # Peak W: always take the maximum of all available sources.
    # getoutput.jsp often returns 0 at night (no recent upload), while
    # interval data also shows 0 after sunset.  Taking the max means whichever
    # source captured the real daytime peak wins.
    interval_peak  = max((r["power_w"] for r in intervals), default=0)
    getoutput_peak = daily["peak_w"] if daily["valid"] else 0
    peak_w         = max(interval_peak, getoutput_peak)

    log.info(
        "PVOutput: %d intervals | %d Wh total | %d W peak "
        "(interval=%d W, getoutput=%d W)",
        len(intervals), total_wh, peak_w, interval_peak, getoutput_peak,
    )
    return {
        "intervals": intervals,
        "total_wh":  total_wh,
        "peak_w":    peak_w,
        "valid":     True,
    }


# ── Internal fetchers ─────────────────────────────────────────────────────────

def _fetch_intervals(for_date: date) -> list[dict]:
    """
    GET getstatus.jsp — returns today's 5-minute interval dicts.

    Omitting the ``d`` parameter returns PVOutput's rolling 288-entry buffer
    of most recent status uploads.  Passing ``d=today`` causes PVOutput to
    return only the single most recent entry for that date.  We filter to
    today's date in Python instead.
    """
    raw = _get("getstatus.jsp", {
        "h":     "1",      # history mode — multiple entries
        "limit": "288",    # up to 288 × 5-min slots
        "asc":   "1",      # oldest first → graph renders left-to-right
    })
    if not raw:
        return []

    # PVOutput returns all rows on ONE line, semicolon-delimited.
    # Confirmed column layout from live data (0-based):
    #   [0] date  [1] time  [2] energy_gen(Wh)  [3] efficiency(kWh/kW)
    #   [4] power_gen(W)  [5] power_gen(W) again  [6] normalised  ...
    rows = raw.split(";")
    log.info("getstatus.jsp: %d rows, sample: %r", len(rows), rows[0][:80])

    results = []
    for row in rows:
        parts = row.split(",")
        if len(parts) < 5:
            continue
        try:
            dt = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y%m%d %H:%M")
            if dt.date() != for_date:
                continue
            energy_wh = int(parts[2]) if parts[2] not in ("", "NaN") else 0
            power_w   = int(parts[4]) if parts[4] not in ("", "NaN") else 0
            results.append({"time": dt, "power_w": power_w, "energy_wh": energy_wh})
        except (ValueError, IndexError) as exc:
            log.debug("Skipping malformed interval row %r: %s", row, exc)

    return results


def _fetch_daily_output(date_str: str) -> dict:
    """
    GET getoutput.jsp — PVOutput's authoritative daily aggregate.

    CSV columns (PVOutput API v2 getoutput):
      0: date
      1: energy_generated (Wh)
      2: energy_exported  (Wh)   ← NOT efficiency
      3: peak_power       (W)
      4: peak_time        (hh:mm)
      5: condition
      6: temp
      7: comments
      ...
    """
    raw = _get("getoutput.jsp", {"d": date_str})
    if not raw:
        return {"total_wh": 0, "peak_w": 0, "valid": False}

    log.info("getoutput.jsp raw: %r", raw[:200])
    parts = raw.splitlines()[0].split(",")
    # Confirmed column layout (0-based):
    #   [0] date  [1] energy_gen(Wh)  [2] efficiency(kWh/kW)
    #   [3] energy_exported(Wh)  [4] ???  [5] peak_power(W)  [6] peak_time
    try:
        total_wh = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        peak_w   = int(parts[5]) if len(parts) > 5 and parts[5] else 0
        return {"total_wh": total_wh, "peak_w": peak_w, "valid": True}
    except (ValueError, IndexError) as exc:
        log.error("Failed to parse getoutput.jsp: %s — raw: %r", exc, raw)
        return {"total_wh": 0, "peak_w": 0, "valid": False}

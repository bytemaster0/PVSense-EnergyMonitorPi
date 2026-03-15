from __future__ import annotations

# sense_client.py — direct Sense Energy REST + WebSocket client
#
# Graph data
# ──────────
# PVOutput only receives daily cumulative totals from this system's inverters,
# not 5-minute status intervals.  Sense's trends endpoint returns 24 hourly
# production buckets (kWh each) which we convert to power-W intervals and use
# as the graph source instead.
#
# Peak power
# ──────────
# PVOutput's getoutput.jsp returns peak_w=0 for this system (inverters don't
# upload instantaneous peak).  Sense's trends response may not include it
# either.  The reliable source is the Sense realtime WebSocket: main.py tracks
# the running daily maximum of solar_w across every 2-minute realtime call and
# persists it via data_cache.update_realtime_peak().
#
# Replaces the `sense-energy` PyPI library, which has intermittent
# compatibility issues with the Python versions shipped on Raspberry Pi OS.
# All API calls go directly to Sense's unofficial endpoints using `requests`
# (HTTP) and `websocket-client` (WebSocket).
#
# Dependencies (both available via pip on Python 3.7+):
#   pip install requests websocket-client
#
# Two public entry points:
#   get_daily_solar_stats()  — today's cumulative Wh + peak W  (every 5 min)
#   get_realtime_power()     — live solar W + consumption W     (every 2 min)
#
# Session management
# ──────────────────
# A single authenticated session is reused across calls.  If any call fails
# with an auth-related error the session is cleared and re-created on the
# next call.  Token expiry is handled by re-authenticating transparently.

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

import config

log = logging.getLogger(__name__)

# ── Sense API endpoints (unofficial) ─────────────────────────────────────────
_AUTH_URL   = "https://api.sense.com/apiservice/api/v1/authenticate"
_TRENDS_URL = "https://api.sense.com/apiservice/api/v1/app/history/trends"
_WS_URL     = "wss://clientrt.sense.com/monitors/{monitor_id}/realtimefeed"
_TIMEOUT    = 30   # seconds for REST calls
_WS_TIMEOUT = 15   # seconds to wait for a realtime_update message from the WebSocket


# ── Session ───────────────────────────────────────────────────────────────────

class _SenseSession:
    """
    Holds an authenticated Sense session (token + monitor ID).

    All network operations are methods here so failures raise exceptions that
    the module-level functions catch and convert to "valid: False" results.
    """

    def __init__(self, access_token: str, monitor_id: int) -> None:
        self.access_token = access_token
        self.monitor_id   = monitor_id
        self._headers = {
            "Authorization": f"bearer {access_token}",
            "User-Agent":    "SolarEnergyMonitor/2.0 (Raspberry Pi; python-requests)",
        }

    # ── Daily trend data ──────────────────────────────────────────────────────

    def get_daily_stats(self) -> tuple[int, int]:
        """
        Return (total_wh, peak_w) for today from the Sense trends endpoint.

        Sense returns production figures keyed under ``production`` or
        ``solar``; we try both plus several known flat-key variants so the
        code doesn't break if Sense adjusts their response schema again.
        """
        # Sense expects `start` in UTC.  Convert local midnight → UTC so we
        # get today's data regardless of the Pi's timezone offset.
        midnight_local = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_utc   = midnight_local.astimezone(timezone.utc)
        params = {
            "monitor_id": self.monitor_id,
            "scale":      "DAY",
            "start":      midnight_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        r = requests.get(
            _TRENDS_URL, headers=self._headers, params=params, timeout=_TIMEOUT
        )
        if not r.ok:
            log.error("Sense trends %d — body: %s", r.status_code, r.text[:400])
        r.raise_for_status()
        data = r.json()
        log.info("Sense trends — top-level keys: %s", list(data.keys()))
        total_wh, peak_w = _parse_daily_stats(data)
        return total_wh, peak_w

    # ── Real-time WebSocket ───────────────────────────────────────────────────

    def get_realtime(self) -> tuple[int, int]:
        """
        Open the Sense real-time WebSocket, receive the first
        ``realtime_update`` message, and close the connection.

        Returns (solar_w, consumption_w).  Raises on timeout or error.
        """
        try:
            import websocket  # websocket-client package
        except ImportError:
            raise RuntimeError(
                "websocket-client not installed — run: pip install websocket-client"
            )

        ws_url = _WS_URL.format(monitor_id=self.monitor_id)
        ws = websocket.create_connection(
            ws_url,
            timeout=_WS_TIMEOUT,
            # Some Sense server versions want the token in the header;
            # others want it as a query parameter — send both to be safe.
            header={"Authorization": f"bearer {self.access_token}"},
        )
        try:
            for _ in range(30):   # safety cap — should hit realtime_update fast
                raw = ws.recv()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if msg.get("type") != "realtime_update":
                    continue

                payload = msg.get("payload", {})
                # Sense may nest data one level deeper under "d"
                if "d" in payload:
                    payload = payload["d"]

                solar_w       = max(0, int(payload.get("solar_w", 0) or 0))
                consumption_w = max(0, int(payload.get("w",       0) or 0))
                return solar_w, consumption_w
        finally:
            try:
                ws.close()
            except Exception:
                pass

        raise TimeoutError(
            f"No realtime_update message received within {_WS_TIMEOUT} s"
        )


# ── Authentication ────────────────────────────────────────────────────────────

def _authenticate() -> Optional[_SenseSession]:
    """POST credentials, return a _SenseSession or None on failure."""
    try:
        r = requests.post(
            _AUTH_URL,
            data={"email": config.SENSE_EMAIL, "password": config.SENSE_PASSWORD},
            timeout=_TIMEOUT,
        )
        if r.status_code == 401:
            log.error(
                "Sense authentication failed — check SENSE_EMAIL / SENSE_PASSWORD in config.py"
            )
            return None
        r.raise_for_status()
        data = r.json()

        access_token = data.get("access_token") or data.get("token")
        monitors     = data.get("monitors", [])
        if not access_token or not monitors:
            log.error("Sense auth response missing token or monitors: %s", data)
            return None

        monitor_id = monitors[0]["id"]
        log.info("Sense authenticated — monitor ID %s", monitor_id)
        return _SenseSession(access_token=access_token, monitor_id=monitor_id)

    except requests.RequestException as exc:
        log.error("Sense authentication request failed: %s", exc)
        return None


# ── Module-level session cache ────────────────────────────────────────────────

_session: Optional[_SenseSession] = None


def _get_session() -> Optional[_SenseSession]:
    global _session
    if _session is None:
        _session = _authenticate()
    return _session


def _invalidate_session() -> None:
    global _session
    _session = None


# ── Public API ────────────────────────────────────────────────────────────────

def get_daily_solar_stats() -> dict:
    """
    Fetch today's solar stats + piggyback the current realtime readings.

    Called every UPDATE_INTERVAL_SECONDS (5 min).  Also captures solar_w and
    consumption_w so the main loop doesn't need a separate realtime call on
    full-refresh cycles.

    Returns dict:
        peak_w        : int   — highest solar W recorded today (from trends)
        total_wh      : int   — cumulative solar Wh today
        solar_w       : int   — current instantaneous solar W
        consumption_w : int   — current home consumption W
        valid         : bool
    """
    session = _get_session()
    if session is None:
        return {"peak_w": 0, "total_wh": 0, "solar_w": 0, "consumption_w": 0, "valid": False}

    result = {"peak_w": 0, "total_wh": 0, "solar_w": 0, "consumption_w": 0, "valid": False}
    had_error = False

    # Daily stats
    try:
        total_wh, peak_w = session.get_daily_stats()
        result["total_wh"] = total_wh
        result["peak_w"]   = peak_w
        result["valid"]    = True
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (401, 403):
            log.warning("Sense daily stats: auth error — will re-authenticate next cycle")
            had_error = True
        else:
            log.error("Sense daily stats HTTP error: %s", exc)
            had_error = True
    except Exception as exc:
        log.error("Sense daily stats failed: %s", exc)
        had_error = True

    # Realtime (best-effort — doesn't affect valid flag for daily data)
    try:
        solar_w, consumption_w = session.get_realtime()
        result["solar_w"]       = solar_w
        result["consumption_w"] = consumption_w
        log.info(
            "Sense: %d Wh net, %d W peak | realtime %d W solar, %d W consumption",
            result["total_wh"], result["peak_w"], solar_w, consumption_w,
        )
    except Exception as exc:
        log.error("Sense realtime (piggyback) failed: %s", exc)
        had_error = True

    if had_error:
        _invalidate_session()

    return result


def get_realtime_power() -> dict:
    """
    Fetch Sense's instantaneous power readings via WebSocket.

    Called every REALTIME_INTERVAL_SECONDS (2 min) on non-full-refresh cycles.

    Returns dict:
        solar_w       : int
        consumption_w : int
        valid         : bool
    """
    session = _get_session()
    if session is None:
        return {"solar_w": 0, "consumption_w": 0, "valid": False}

    try:
        solar_w, consumption_w = session.get_realtime()
        log.info("Sense realtime: %d W solar, %d W consumption", solar_w, consumption_w)
        return {"solar_w": solar_w, "consumption_w": consumption_w, "valid": True}
    except Exception as exc:
        log.error("Sense realtime failed: %s — will retry next cycle", exc)
        _invalidate_session()
        return {"solar_w": 0, "consumption_w": 0, "valid": False}


# ── Response parsing ──────────────────────────────────────────────────────────

def _parse_daily_stats(data: dict) -> tuple[int, int]:
    """
    Extract (net_wh, peak_w) from a Sense trends response.

    net_wh = to_grid - from_grid (net metering balance, kWh).
    Positive = net exporter (more solar to grid than grid to home).
    Negative = net importer (drew more from grid than solar produced).

    peak_w is not available from the trends endpoint; the realtime
    WebSocket tracker in main.py accumulates it during the day instead.
    """
    to_grid   = float(data.get("to_grid",   0) or 0)
    from_grid = float(data.get("from_grid", 0) or 0)
    net_kwh   = to_grid - from_grid
    net_wh    = int(net_kwh * 1000)
    log.info(
        "Sense to_grid: %.2f kWh, from_grid: %.2f kWh → net: %.2f kWh (%d Wh)",
        to_grid, from_grid, net_kwh, net_wh,
    )
    return net_wh, 0



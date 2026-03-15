# display_renderer.py — Waveshare 7.5" HD 3-colour E-ink rendering
#
# Physical display: 880 × 528 px, black + red + white.
# Two PIL images are composed (one per colour channel) then sent to the EPD.
#
# Layout (all measurements in pixels):
#
#   y=  0 ┌──────────────────────────────────────────────────────────────────┐
#         │  SOLAR ENERGY MONITOR                    Saturday, March 14 2026 │  title bar (black)
#   y= 68 ├──────────┬──────────┬──────────┬──────────┬──────────┤
#         │Peak:     │Today:    │Net:      │Solar:    │Using:    │  stats row
#         │10,944 W  │74.3 kWh  │51.8 kWh  │3,800 W   │1,200 W   │
#         │(BLACK)   │(BLACK)   │(BLACK)   │(RED-live)│(RED-live)│
#   y=128 ├──────────┴──────────┴──────────┴──────────┴──────────┤
#         │                   (red filled-area graph)                        │  graph area
#   y=490 ├──────────────────────────────────────────────────────────────────┤
#         │  6   7   8   9  10  11  12   1   2   3   4   5   6   7   8   9  │  x-axis labels
#   y=528 └──────────────────────────────────────────────────────────────────┘
#
# Colour semantics:
#   Black text — historical / daily aggregates (peak, cumulative energy)
#   Red text   — live / instantaneous readings (solar now, consumption now)

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

import config

log = logging.getLogger(__name__)

# ── Geometry constants ────────────────────────────────────────────────────────
W, H = config.DISPLAY_WIDTH, config.DISPLAY_HEIGHT
MARGIN       = 12
TITLE_H      = 68
STATS_H      = 60
XLABEL_H     = 32
DIVIDER_W    = 2

GRAPH_X0 = MARGIN + 52          # left edge of graph (space for y-axis labels)
GRAPH_X1 = W - MARGIN           # right edge
GRAPH_Y0 = TITLE_H + STATS_H + 8
GRAPH_Y1 = H - XLABEL_H - MARGIN
GRAPH_W  = GRAPH_X1 - GRAPH_X0
GRAPH_H  = GRAPH_Y1 - GRAPH_Y0

WHITE = 255
BLACK = 0


# ── Font loader ───────────────────────────────────────────────────────────────

def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except (OSError, IOError):
        log.warning("Could not load font %s — falling back to PIL default", path)
        return ImageFont.load_default()


# ── Main render entry point ───────────────────────────────────────────────────

def render(
    intervals:   list[dict],              # from pvoutput_client.fetch_day_data()
    pvo_daily:   dict,                    # from pvoutput_client.fetch_day_data()
    sense_daily: dict,                    # from sense_client.get_daily_solar_stats()
    realtime:    dict,                    # from sense_client.get_realtime_power()
    updated_at:  Optional[datetime] = None,
    is_stale:    bool = False,            # True → at least one daily source is from cache
    dry_run:     bool = False,            # True → save preview.png instead of driving EPD
) -> None:
    """
    Compose and push one complete display refresh.

    Parameters
    ----------
    intervals   : 5-minute PVOutput records [{time, power_w, energy_wh}, ...]
    pvo_daily   : {total_wh, peak_w, valid}
    sense_daily : {peak_w, total_wh, valid}
    realtime    : {solar_w, consumption_w, valid}  — instantaneous Sense readings
    updated_at  : timestamp to print; defaults to now
    is_stale    : show cache indicator in title when True
    dry_run     : save preview.png instead of driving hardware
    """
    updated_at = updated_at or datetime.now()

    # Daily peak: Sense realtime tracker captures higher transient spikes than PVOutput 5-min averages
    peak_w   = max(pvo_daily.get("peak_w", 0), sense_daily.get("peak_w", 0))
    total_wh = pvo_daily.get("total_wh", 0)
    net_wh   = sense_daily.get("total_wh", 0)   # net solar from Sense

    # Build images — mode '1': 1-bit, 0=black pixel, 255=white pixel
    img_black = Image.new("1", (W, H), WHITE)
    img_red   = Image.new("1", (W, H), WHITE)
    d_black   = ImageDraw.Draw(img_black)
    d_red     = ImageDraw.Draw(img_red)

    _draw_title(d_black, updated_at, is_stale)
    _draw_stats(d_black, d_red, peak_w, total_wh, net_wh, realtime)
    _draw_graph(d_red, d_black, intervals)

    if dry_run:
        _save_debug(img_black, img_red)
        return

    _push_to_epd(img_black, img_red)


# ── Section drawers ───────────────────────────────────────────────────────────

def _draw_title(d: ImageDraw.ImageDraw, updated_at: datetime, is_stale: bool = False) -> None:
    font_title  = _load_font(config.FONT_PATH_BOLD,    28)
    font_sub    = _load_font(config.FONT_PATH_REGULAR, 18)

    d.rectangle([0, 0, W, TITLE_H], fill=BLACK)
    d.text((MARGIN, 10), "SOLAR ENERGY MONITOR", font=font_title, fill=WHITE)

    date_str = updated_at.strftime("%A, %B %-d %Y")   # e.g. Saturday, March 14 2026
    # %-d is Linux only (no leading zero)
    # When serving cached data, say so clearly rather than implying live figures
    if is_stale:
        time_str = updated_at.strftime("Cached – live at %-I:%M %p")
    else:
        time_str = updated_at.strftime("Updated %-I:%M %p")

    # Right-align the date
    bbox = d.textbbox((0, 0), date_str, font=font_sub)
    tw = bbox[2] - bbox[0]
    d.text((W - MARGIN - tw, 12), date_str, font=font_sub, fill=WHITE)
    d.text((MARGIN, 44), time_str, font=font_sub, fill=WHITE)


def _draw_stats(
    d_black:  ImageDraw.ImageDraw,
    d_red:    ImageDraw.ImageDraw,
    peak_w:   int,
    total_wh: int,
    net_wh:   int,
    realtime: dict,
) -> None:
    """
    Draw the five-column stats bar.

    Columns 1-3 (black): daily historical — peak power, gross energy (PVOutput),
                          net energy (Sense).
    Columns 4-5 (red):   live realtime   — solar now, using now.
    """
    font_label = _load_font(config.FONT_PATH_REGULAR, 15)
    font_value = _load_font(config.FONT_PATH_BOLD,    24)

    y_top      = TITLE_H + 5
    col_w      = W // 5          # 176 px per column at 880 px wide
    divider_y0 = TITLE_H + 2
    divider_y1 = TITLE_H + STATS_H - 2

    # ── Column 1: Peak Power (BLACK) ──────────────────────────────────────────
    x1 = MARGIN
    d_black.text((x1, y_top),      "Peak Power",    font=font_label, fill=BLACK)
    d_black.text((x1, y_top + 20), f"{peak_w:,} W", font=font_value, fill=BLACK)
    d_black.line([col_w, divider_y0, col_w, divider_y1], fill=BLACK, width=DIVIDER_W)

    # ── Column 2: Today's Gross Energy from PVOutput (BLACK) ──────────────────
    x2 = col_w + MARGIN
    d_black.text((x2, y_top),      "Total Energy",             font=font_label, fill=BLACK)
    d_black.text((x2, y_top + 20), f"{total_wh / 1000:.1f} kWh", font=font_value, fill=BLACK)
    d_black.line([col_w * 2, divider_y0, col_w * 2, divider_y1], fill=BLACK, width=DIVIDER_W)

    # ── Column 3: Net Energy from Sense (BLACK) ────────────────────────────────
    x3 = col_w * 2 + MARGIN
    d_black.text((x3, y_top),      "Net Energy",              font=font_label, fill=BLACK)
    d_black.text((x3, y_top + 20), f"{net_wh / 1000:.1f} kWh", font=font_value, fill=BLACK)

    # Heavier divider separates daily historical from live readings
    d_black.line([col_w * 3, divider_y0, col_w * 3, divider_y1], fill=BLACK, width=DIVIDER_W + 1)

    # ── Columns 4-5: Realtime readings (RED) ──────────────────────────────────
    rt_valid  = realtime.get("valid", False)
    solar_str = f"{realtime.get('solar_w', 0):,} W" if rt_valid else "—"
    usage_str = f"{realtime.get('consumption_w', 0):,} W" if rt_valid else "—"

    x4 = col_w * 3 + MARGIN
    d_red.text((x4, y_top),      "Solar Now", font=font_label, fill=BLACK)
    d_red.text((x4, y_top + 20), solar_str,   font=font_value, fill=BLACK)
    d_black.line([col_w * 4, divider_y0, col_w * 4, divider_y1], fill=BLACK, width=DIVIDER_W)

    x5 = col_w * 4 + MARGIN
    d_red.text((x5, y_top),      "Using Now", font=font_label, fill=BLACK)
    d_red.text((x5, y_top + 20), usage_str,   font=font_value, fill=BLACK)

    # Horizontal rule below the whole stats row
    d_black.line([0, TITLE_H + STATS_H + 2, W, TITLE_H + STATS_H + 2],
                 fill=BLACK, width=DIVIDER_W)


def _draw_graph(
    d_red:   ImageDraw.ImageDraw,
    d_black: ImageDraw.ImageDraw,
    intervals: list[dict],
) -> None:
    """Draw a 5-minute-resolution red line graph of solar power output."""
    font_axis = _load_font(config.FONT_PATH_REGULAR, 15)

    # Graph border and axes (black)
    d_black.rectangle(
        [GRAPH_X0, GRAPH_Y0, GRAPH_X1, GRAPH_Y1],
        outline=BLACK, width=1,
    )

    if not intervals:
        d_black.text(
            (GRAPH_X0 + GRAPH_W // 2 - 60, GRAPH_Y0 + GRAPH_H // 2),
            "No data", font=font_axis, fill=BLACK,
        )
        return

    # ── Y-axis scale ──────────────────────────────────────────────────────────
    max_power = max((r["power_w"] for r in intervals), default=0)
    max_power = max(max_power, 100)                    # avoid divide-by-zero
    # Round up to nearest 500 W for a clean axis ceiling
    y_max = ((max_power // 500) + 1) * 500

    def to_y(watts: int) -> int:
        fraction = watts / y_max
        return int(GRAPH_Y1 - fraction * GRAPH_H)

    # Y gridlines + labels at 25%, 50%, 75%, 100%
    for frac, label_fn in [(0.25, lambda v: f"{v:,}"),
                           (0.50, lambda v: f"{v:,}"),
                           (0.75, lambda v: f"{v:,}"),
                           (1.00, lambda v: f"{v:,}")]:
        gw = int(y_max * frac)
        gy = to_y(gw)
        d_black.line([GRAPH_X0, gy, GRAPH_X1, gy], fill=BLACK, width=1)
        label = label_fn(gw)
        bbox  = d_black.textbbox((0, 0), label, font=font_axis)
        tw    = bbox[2] - bbox[0]
        d_black.text((GRAPH_X0 - tw - 4, gy - 8), label, font=font_axis, fill=BLACK)

    # ── X-axis time mapping ───────────────────────────────────────────────────
    earliest = intervals[0]["time"]
    latest   = intervals[-1]["time"]

    total_minutes = max((latest - earliest).total_seconds() / 60, 1)

    def to_x(dt: datetime) -> int:
        elapsed = (dt - earliest).total_seconds() / 60
        return GRAPH_X0 + int(elapsed / total_minutes * GRAPH_W)

    # Hour tick marks and labels
    _draw_x_axis_labels(d_black, font_axis, earliest, latest)

    # ── Line graph (red) ──────────────────────────────────────────────────────
    points = [(to_x(r["time"]), to_y(r["power_w"])) for r in intervals]

    # Filled area under curve for visibility
    # Build polygon: line points + close down to x-axis baseline
    if len(points) >= 2:
        poly = list(points) + [(points[-1][0], GRAPH_Y1), (points[0][0], GRAPH_Y1)]
        d_red.polygon(poly, fill=BLACK)  # mode '1': 0=colour, 255=white — BLACK=0 = red pixel

    # Overdraw the line slightly thicker for crispness
    for i in range(len(points) - 1):
        d_red.line([points[i], points[i + 1]], fill=BLACK, width=2)


def _draw_x_axis_labels(
    d:        ImageDraw.ImageDraw,
    font:     ImageFont.FreeTypeFont,
    earliest: datetime,
    latest:   datetime,
) -> None:
    """Draw hour labels below the graph."""
    total_minutes = max((latest - earliest).total_seconds() / 60, 1)

    start_h = earliest.hour
    end_h   = latest.hour + (1 if latest.minute > 0 else 0)

    for h in range(start_h, end_h + 1):
        # position of this hour on the x-axis
        minutes_from_start = (h - earliest.hour) * 60 - earliest.minute
        if minutes_from_start < 0:
            continue
        x = GRAPH_X0 + int(minutes_from_start / total_minutes * GRAPH_W)
        if x > GRAPH_X1:
            break

        # Tick mark
        d.line([x, GRAPH_Y1, x, GRAPH_Y1 + 5], fill=BLACK, width=1)

        # Label — 12-hour without leading zero
        label = f"{h % 12 or 12}{'a' if h < 12 else 'p'}"
        bbox  = d.textbbox((0, 0), label, font=font)
        tw    = bbox[2] - bbox[0]
        d.text((x - tw // 2, GRAPH_Y1 + 7), label, font=font, fill=BLACK)


# ── Hardware push ─────────────────────────────────────────────────────────────

def _push_to_epd(img_black: Image.Image, img_red: Image.Image) -> None:
    """Send composed images to the Waveshare 7.5" HD 3-colour EPD."""
    try:
        from waveshare_epd import epd7in5b_HD  # type: ignore
    except ImportError:
        log.error(
            "waveshare_epd not found. "
            "Install the Waveshare e-Paper library or run with dry_run=True."
        )
        return

    epd = epd7in5b_HD.EPD()
    try:
        log.info("Initialising EPD…")
        epd.init()
        log.info("Pushing frame to display…")
        epd.display(epd.getbuffer(img_black), epd.getbuffer(img_red))
        log.info("Display updated — entering sleep mode")
        epd.sleep()
    except Exception as exc:
        log.error("EPD display failed: %s", exc)
        try:
            epd.sleep()
        except Exception:
            pass


# ── Debug helper ──────────────────────────────────────────────────────────────

def _save_debug(img_black: Image.Image, img_red: Image.Image) -> None:
    """Save composite PNG for off-device testing (dry_run=True)."""
    # Composite: white bg, black pixels from img_black, red pixels from img_red
    out = Image.new("RGB", (W, H), (255, 255, 255))
    pixels = out.load()
    pb = img_black.load()
    pr = img_red.load()
    for y in range(H):
        for x in range(W):
            if pb[x, y] == BLACK:
                pixels[x, y] = (0, 0, 0)
            elif pr[x, y] == BLACK:
                pixels[x, y] = (220, 30, 30)
    out.save("preview.png")
    log.info("Dry run: saved preview.png")

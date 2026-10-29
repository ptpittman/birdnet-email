#!/usr/bin/env python3
"""BirdNET-Go weekly digest.

Reads a BirdNET-Go station through its /api/v2 HTTP API, works out what was
interesting about the past week, renders an HTML email in BirdNET-Go's own
visual language and sends it through Mailgun.

Usage:
  digest.py --preview out.html    render to a file, send nothing
  digest.py --now                 render and send once
  digest.py                       run forever, sending on the configured schedule
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:  # thumbnails are sent unresized without Pillow
    Image = None

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing required environment variable {name}")
    return val or ""


class Config:
    def __init__(self) -> None:
        self.api = env("BIRDNET_URL", "http://localhost:8080").rstrip("/")
        # Where links in the email should point (a LAN name, Tailscale URL, etc.)
        self.public = env("BIRDNET_PUBLIC_URL", self.api).rstrip("/")
        self.token = env("BIRDNET_TOKEN")
        self.station = env("STATION_NAME", "BirdNET-Go")
        self.scheme = env("COLOR_SCHEME", "blue").lower()
        self.min_conf = float(env("MIN_CONFIDENCE", "0"))
        self.mg_key = env("MAILGUN_API_KEY")
        self.mg_domain = env("MAILGUN_DOMAIN")
        self.mg_base = env("MAILGUN_API_BASE") or (
            "https://api.eu.mailgun.net" if env("MAILGUN_REGION", "us").lower() == "eu"
            else "https://api.mailgun.net")
        self.mail_from = env("MAIL_FROM")
        self.mail_to = [a.strip() for a in env("MAIL_TO").split(",") if a.strip()]
        self.day = env("SCHEDULE_DAY", "mon").lower()[:3]
        self.at = env("SCHEDULE_TIME", "07:30")


# --------------------------------------------------------------------------
# BirdNET-Go API client
# --------------------------------------------------------------------------

class BirdNET:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _req(self, path: str, params: dict | None = None, raw: bool = False, timeout: int = 30):
        url = f"{self.cfg.api}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, headers={"Accept": "*/*" if raw else "application/json"})
        if self.cfg.token:
            req.add_header("Authorization", f"Bearer {self.cfg.token}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            if raw:
                return r.status, r.headers.get("Content-Type", ""), body
            return json.loads(body)

    def get(self, path: str, **params):
        return self._req(path, params)

    def detections(self, start: dt.date, end: dt.date) -> list[dict]:
        out, offset = [], 0
        while True:
            page = self.get("/api/v2/detections", start_date=start.isoformat(),
                            end_date=end.isoformat(), limit=1000, offset=offset,
                            sortBy="date_asc")
            rows = page.get("data") or []
            out.extend(rows)
            offset += len(rows)
            if not rows or offset >= int(page.get("total", 0)):
                return out

    def summary(self, start: dt.date | None = None, end: dt.date | None = None) -> list[dict]:
        p = {}
        if start:
            p = {"start_date": start.isoformat(), "end_date": end.isoformat()}
        return self.get("/api/v2/analytics/species/summary", **p) or []

    def new_species(self, start: dt.date, end: dt.date) -> list[dict]:
        try:
            return self.get("/api/v2/analytics/species/detections/new",
                            start_date=start.isoformat(), end_date=end.isoformat()) or []
        except urllib.error.HTTPError:
            return []

    def image(self, path_or_url: str, tries: int = 1) -> bytes | None:
        """Fetch an image, retrying while BirdNET-Go is still generating it."""
        for attempt in range(tries):
            try:
                status, ctype, body = self._req(path_or_url, raw=True, timeout=60)
                if status == 200 and ctype.startswith("image/"):
                    return body
            except (urllib.error.URLError, TimeoutError):
                pass
            if attempt < tries - 1:
                time.sleep(4)
        return None

    def image_credit(self, sci: str) -> dict:
        try:
            return self.get("/api/v2/media/species-image/info", name=sci) or {}
        except (urllib.error.URLError, ValueError):
            return {}


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def pct(conf: float) -> int:
    conf = conf or 0
    return round(conf * 100 if conf <= 1 else conf)


def hour_of(d: dict) -> int:
    try:
        return int((d.get("time") or "00")[:2])
    except ValueError:
        return 0


def analyse(api: BirdNET, cfg: Config, end: dt.date) -> dict:
    start = end - dt.timedelta(days=6)
    prev_start, prev_end = start - dt.timedelta(days=7), start - dt.timedelta(days=1)
    base_start = start - dt.timedelta(days=28)

    dets = [d for d in api.detections(start, end)
            if d.get("verified") != "false_positive" and pct(d.get("confidence")) >= cfg.min_conf * 100]
    prev = {s["scientific_name"]: s for s in api.summary(prev_start, prev_end)}
    base = {s["scientific_name"]: s for s in api.summary(base_start, prev_end)}
    alltime = {s["scientific_name"]: s for s in api.summary()}
    ytd_end = start - dt.timedelta(days=1)
    jan1 = dt.date(end.year, 1, 1)
    ytd = {s["scientific_name"] for s in api.summary(jan1, ytd_end)} if ytd_end >= jan1 else set()
    new_ever = {n["scientific_name"]: n for n in api.new_species(start, end)}

    names = {}
    by_sp = defaultdict(list)
    for d in dets:
        sci = d["scientificName"]
        names[sci] = d.get("commonName") or sci
        by_sp[sci].append(d)

    counts = Counter({s: len(v) for s, v in by_sp.items()})
    days = [start + dt.timedelta(days=i) for i in range(7)]
    daily = Counter(d["date"] for d in dets)
    hourly = Counter(hour_of(d) for d in dets)

    # Species ranking with change against the 4-week weekly average
    top = []
    for sci, n in counts.most_common(10):
        avg = base.get(sci, {}).get("count", 0) / 4
        top.append({"sci": sci, "name": names[sci], "count": n, "avg": avg,
                    "delta": None if avg < 1 else (n - avg) / avg})

    # Species x hour heatmap for the 12 busiest species
    grid = []
    for sci, _ in counts.most_common(12):
        row = [0] * 24
        for d in by_sp[sci]:
            row[hour_of(d)] += 1
        grid.append({"sci": sci, "name": names[sci], "hours": row, "total": counts[sci]})

    def best(sci: str) -> dict:
        return max(by_sp[sci], key=lambda d: d.get("confidence") or 0)

    # New arrivals: first ever, then first this year
    arrivals = []
    for sci in counts:
        if sci in new_ever:
            kind = "First ever"
        elif ytd and sci not in ytd and alltime.get(sci, {}).get("count", 0) > counts[sci]:
            kind = f"First of {end.year}"
        else:
            continue
        b = best(sci)
        arrivals.append({"sci": sci, "name": names[sci], "kind": kind, "count": counts[sci],
                         "first": min(by_sp[sci], key=lambda d: (d["date"], d["time"])),
                         "conf": pct(b.get("confidence")), "best_id": b["id"]})
    arrivals.sort(key=lambda a: (a["kind"] != "First ever", -a["conf"]))

    # Highlight: the best new arrival, else the week's rarest species by all-time count
    hero_sci = None
    if arrivals:
        hero_sci = arrivals[0]["sci"]
    elif counts:
        good = [s for s in counts if pct(best(s).get("confidence")) >= 80] or list(counts)
        hero_sci = min(good, key=lambda s: (alltime.get(s, {}).get("count", 0), -counts[s]))
    hero = None
    if hero_sci:
        b = best(hero_sci)
        hero = {"sci": hero_sci, "name": names[hero_sci], "det": b, "conf": pct(b.get("confidence")),
                "alltime": alltime.get(hero_sci, {}).get("count", counts[hero_sci]),
                "count": counts[hero_sci], "new": hero_sci in new_ever}

    # Gone quiet: regulars over the past 4 weeks with nothing this week
    quiet = []
    for sci, s in base.items():
        if sci in counts or s.get("count", 0) < 12:
            continue
        last = (alltime.get(sci, {}).get("last_heard") or s.get("last_heard") or "")[:10]
        quiet.append({"sci": sci, "name": s.get("common_name") or sci,
                      "avg": s["count"] / 4, "last": last})
    quiet.sort(key=lambda q: -q["avg"])

    # After dark: BirdNET-Go's own sun-based classification, hours as a fallback
    def is_night(d):
        tod = d.get("timeOfDay")
        return tod == "night" if tod else (hour_of(d) >= 22 or hour_of(d) < 5)
    night = Counter(d["scientificName"] for d in dets if is_night(d))

    prev_total = sum(s.get("count", 0) for s in prev.values())
    busiest = max(days, key=lambda d: daily.get(d.isoformat(), 0))
    return {
        "start": start, "end": end, "days": days, "total": len(dets), "prev_total": prev_total,
        "species": len(counts), "prev_species": len(prev), "daily": daily, "hourly": hourly,
        "busiest": busiest, "peak_hour": max(range(24), key=lambda h: hourly.get(h, 0)) if dets else None,
        "top": top, "grid": grid, "arrivals": arrivals[:6], "hero": hero, "quiet": quiet[:5],
        "night": [(names[s], n) for s, n in night.most_common(5)],
        "alltime_species": len(alltime),
    }


# --------------------------------------------------------------------------
# Design tokens, lifted from BirdNET-Go's frontend (tailwind.css, schemes.css,
# DailySummaryCard heatmap ramp, ConfidenceBadge thresholds)
# --------------------------------------------------------------------------

SCHEMES = {  # (light primary, dark primary)
    "blue": ("#2563eb", "#3b82f6"),
    "forest": ("#047857", "#059669"),
    "amber": ("#b45309", "#f59e0b"),
    "violet": ("#7c3aed", "#8b5cf6"),
    "rose": ("#e11d48", "#fb7185"),
}
HEAT_LIGHT = ["#f0f9fc", "#e0f3f8", "#ccebf6", "#99d7ed", "#66c2e4",
              "#33ade1", "#0099d8", "#0077be", "#005595", "#003366"]
HEAT_DARK = ["#1e293b", "#164e63", "#0e7490", "#0891b2", "#06b6d4",
             "#22d3ee", "#38bdf8", "#60a5fa", "#93c5fd", "#bfdbfe"]
SUCCESS, WARNING, ERROR = "#22c55e", "#f59e0b", "#dc2626"
FONT = "'Inter',ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "'JetBrains Mono',ui-monospace,Menlo,Consolas,monospace"


def mix(a: str, b: str, t: float) -> str:
    """Blend hex colour a toward b by t (what color-mix does in the app)."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def conf_colors(p: int) -> tuple[str, str]:
    if p >= 90:
        return SUCCESS, "#020617"
    if p >= 70:
        return mix(SUCCESS, WARNING, 0.2), "#020617"
    if p >= 50:
        return WARNING, "#020617"
    if p >= 30:
        return mix(WARNING, ERROR, 0.4), "#020617"
    return ERROR, "#ffffff"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

E = html.escape


class Images:
    """Collects inline images; each becomes a cid: attachment."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {}

    def add(self, name: str, data: bytes, mime: str = "image/png") -> str:
        self.files[name] = (data, mime)
        return f"cid:{name}"


def thumb(data: bytes, size: int = 96) -> tuple[bytes, str]:
    if not Image:
        return data, "image/jpeg"
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im = ImageOps.fit(im, (size, size), Image.LANCZOS, centering=(0.5, 0.4))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85, optimize=True)
    return buf.getvalue(), "image/jpeg"


def slug(sci: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in sci.lower())


def fmt_date(d: dt.date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def fmt_range(a: dt.date, b: dt.date) -> str:
    if a.month == b.month:
        return f"{a.day}–{b.day} {b:%B %Y}"
    return f"{a.day} {a:%b} – {b.day} {b:%b %Y}"


def delta_chip(cur: float, prev: float) -> str:
    if not prev:
        return ""
    ch = (cur - prev) / prev
    if abs(ch) < 0.03:
        return '<span class="muted" style="color:#475569;font-size:12px;">flat vs last wk</span>'
    up = ch > 0
    col = "#15803d" if up else "#b91c1c"
    return (f'<span style="color:{col};font-size:12px;font-weight:600;">'
            f'{"▲" if up else "▼"} {abs(ch) * 100:.0f}%</span>'
            f'<span class="muted" style="color:#475569;font-size:12px;"> vs last wk</span>')


def make_card(primary: str, title: str, body: str, sub: str = "", link: str = "") -> str:
    head_link = (f'<a href="{E(link)}" class="link" style="color:{primary};font-size:13px;'
                 f'font-weight:500;text-decoration:none;">Open&nbsp;→</a>') if link else ""
    return f"""
<tr><td style="padding:0 0 16px 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="card"
  style="background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;">
<tr><td style="padding:20px 20px 4px 20px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td style="font:600 16px/1.3 {FONT};color:#1f2937;" class="fg">{E(title)}</td>
<td align="right" style="font:13px {FONT};">{head_link}</td></tr>
  {f'<tr><td colspan="2" class="muted" style="padding-top:2px;font:13px/1.4 {FONT};color:#475569;">{sub}</td></tr>' if sub else ''}
  </table></td></tr>
<tr><td style="padding:12px 20px 20px 20px;">{body}</td></tr>
</table></td></tr>"""


def page(cfg: Config, logo: str, subtitle: str, parts: list[str], subject: str,
         preheader: str, footer: str = "") -> str:
    """The outer email: header, cards, footer, light and dark styles."""
    primary, primary_dark = SCHEMES.get(cfg.scheme, SCHEMES["blue"])
    pub = cfg.public
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">
<title>{E(subject)}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>
  body{{margin:0;padding:0;background:#f3f4f6;-webkit-text-size-adjust:100%;}}
  a{{color:{primary};}}
  @media (max-width:600px){{
    .kpi{{display:inline-block!important;width:50%!important;box-sizing:border-box;}}
    .wrap{{padding:16px 10px!important;}} .lab{{width:112px!important;}}
  }}
  @media (prefers-color-scheme: dark){{
    body,.page{{background:#020617!important;}}
    .card{{background:#0f172a!important;border-color:#1e293b!important;}}
    .fg{{color:#f1f5f9!important;}} .muted{{color:#cbd5e1!important;}}
    .bd{{border-color:#1e293b!important;}} .b300{{background:#1e293b!important;}}
    .bar{{background:{primary_dark}!important;}} .bar2{{background:{mix(primary_dark, '#0f172a', .5)}!important;}}
    .link,a{{color:{mix(primary_dark, '#ffffff', .25)}!important;}}
    .h0{{background:#1e293b!important;}}
    {''.join(f'.h{i}{{background:{HEAT_DARK[i]}!important;}}' for i in range(1, 10))}
  }}
</style></head>
<body class="page" style="margin:0;background:#f3f4f6;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{E(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="page" style="background:#f3f4f6;">
<tr><td align="center" class="wrap" style="padding:28px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;">

<tr><td style="padding:0 4px 20px 4px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="40" valign="middle"><img src="{logo}" width="40" height="40" alt="" style="display:block;border-radius:10px;"></td>
<td valign="middle" style="padding-left:12px;">
  <div class="fg" style="font:700 18px/1.2 {FONT};color:#1f2937;">{E(cfg.station)}</div>
  <div class="muted" style="font:13px {FONT};color:#475569;">{E(subtitle)}</div>
</td>
<td align="right" valign="middle"><a href="{pub}/ui/dashboard" class="link" style="font:500 13px {FONT};color:{primary};text-decoration:none;">Dashboard&nbsp;→</a></td>
</tr></table></td></tr>

{''.join(parts)}

<tr><td class="muted" align="center" style="padding:8px 12px 0 12px;font:12px/1.6 {FONT};color:#475569;">
  Sent by your BirdNET-Go station. Species identification by the BirdNET model; check unusual records by ear before reporting them.
  {footer}
</td></tr>
</table></td></tr></table></body></html>"""


def render(a: dict, cfg: Config, api: BirdNET | None) -> tuple[str, str, Images]:
    imgs = Images()
    primary, primary_dark = SCHEMES.get(cfg.scheme, SCHEMES["blue"])
    pub = cfg.public
    logo = imgs.add("logo.png", (HERE / "assets" / "logo.png").read_bytes())

    credits = {}

    def bird_img(sci: str, size: int = 48, radius: int = 10) -> str:
        data = api.image(f"/api/v2/media/image/{urllib.parse.quote(sci)}") if api else None
        if not data:
            initials = "".join(w[0] for w in sci.split()[:2]).upper()
            return (f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
                    f'<td class="b300" width="{size}" height="{size}" align="center" '
                    f'style="width:{size}px;height:{size}px;border-radius:{radius}px;background:#e5e7eb;'
                    f'font:600 {size // 3}px {FONT};color:#475569;">{E(initials)}</td></tr></table>')
        body, mime = thumb(data, size * 2)
        cid = imgs.add(f"sp-{slug(sci)}-{size}.jpg", body, mime)
        if api and sci not in credits:
            credits[sci] = api.image_credit(sci)
        return (f'<img src="{cid}" width="{size}" height="{size}" alt="" '
                f'style="display:block;width:{size}px;height:{size}px;border-radius:{radius}px;object-fit:cover;">')

    def card(title: str, body: str, sub: str = "", link: str = "") -> str:
        return make_card(primary, title, body, sub, link)

    parts = []

    # KPI tiles -----------------------------------------------------------
    def kpi(label, value, foot=""):
        return (f'<td class="kpi" width="25%" valign="top" style="padding:4px;">'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="card" '
                f'style="background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;"><tr>'
                f'<td style="padding:14px 16px;">'
                f'<div class="muted" style="font:500 12px {FONT};color:#475569;text-transform:uppercase;letter-spacing:.04em;">{label}</div>'
                f'<div class="fg" style="font:700 26px/1.2 {FONT};color:#1f2937;padding-top:4px;">{value}</div>'
                f'<div style="padding-top:2px;font:12px {FONT};">{foot or "&nbsp;"}</div></td></tr></table></td>')

    new_count = sum(1 for x in a["arrivals"] if x["kind"] == "First ever")
    busiest_n = a["daily"].get(a["busiest"].isoformat(), 0)
    parts.append(
        '<tr><td style="padding:0 0 12px 0;"><table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0"><tr>'
        + kpi("Detections", f"{a['total']:,}", delta_chip(a["total"], a["prev_total"]))
        + kpi("Species", a["species"], delta_chip(a["species"], a["prev_species"]))
        + kpi("New species", new_count,
              f'<span class="muted" style="color:#475569;">{a["alltime_species"]} all time</span>')
        + kpi("Busiest day", f"{a['busiest']:%a}",
              f'<span class="muted" style="color:#475569;">{busiest_n:,} detections</span>')
        + "</tr></table></td></tr>")

    # Highlight -----------------------------------------------------------
    h = a["hero"]
    if h:
        det = h["det"]
        spec = api.image(f"/api/v2/spectrogram/{det['id']}?size=lg", tries=6) if api else None
        spec_html = ""
        if spec:
            cid = imgs.add("spectrogram.png", spec, "image/png")
            spec_html = (f'<tr><td colspan="2" style="padding-top:16px;">'
                         f'<a href="{pub}/ui/detections/{det["id"]}"><img src="{cid}" width="558" alt="Spectrogram" '
                         f'style="display:block;width:100%;height:auto;border-radius:10px;'
                         f'background:#0f172a;"></a></td></tr>')
        bg, fg = conf_colors(h["conf"])
        if h["new"]:
            why = "A first for this station."
        elif h["alltime"] <= h["count"] * 2:
            why = f"Only {h['alltime']} detections ever at this station."
        else:
            why = f"The rarest regular of the week: {h['alltime']:,} detections all time."
        body = f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="72" valign="top">{bird_img(h['sci'], 72, 14)}</td>
<td valign="top" style="padding-left:16px;">
  <div class="fg" style="font:700 20px/1.25 {FONT};color:#1f2937;">{E(h['name'])}</div>
  <div class="muted" style="font:italic 13px {FONT};color:#475569;padding-top:2px;">{E(h['sci'])}</div>
  <div style="padding-top:10px;">
    <span style="display:inline-block;background:{bg};color:{fg};font:600 12px {FONT};padding:3px 9px;border-radius:9999px;">{h['conf']}%</span>
    <span class="muted" style="font:13px {FONT};color:#475569;padding-left:6px;">{fmt_date(dt.date.fromisoformat(det['date']))} · {det['time'][:5]}</span>
  </div>
  <div class="fg" style="font:14px/1.5 {FONT};color:#1f2937;padding-top:8px;">{E(why)}</div>
</td></tr>{spec_html}</table>"""
        parts.append(card("Highlight of the week", body, link=f"{pub}/ui/detections/{det['id']}"))

    # New arrivals --------------------------------------------------------
    if a["arrivals"]:
        rows = []
        for x in a["arrivals"]:
            first = x["first"]
            tone = SUCCESS if x["kind"] == "First ever" else primary
            rows.append(f"""
<tr><td style="padding:8px 0;border-top:1px solid #e2e8f0;" class="bd">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="44" valign="middle">{bird_img(x['sci'], 44)}</td>
<td valign="middle" style="padding-left:12px;">
  <div class="fg" style="font:600 14px {FONT};color:#1f2937;">{E(x['name'])}</div>
  <div class="muted" style="font:12px {FONT};color:#475569;">First heard {fmt_date(dt.date.fromisoformat(first['date']))}, {first['time'][:5]} · {x['count']} detection{'s' if x['count'] != 1 else ''}</div>
</td>
<td align="right" valign="middle" style="white-space:nowrap;">
  <span style="display:inline-block;background:{mix(tone, '#ffffff', .82)};color:{mix(tone, '#000000', .35)};font:600 11px {FONT};padding:3px 8px;border-radius:9999px;">{E(x['kind'])}</span>
  <a href="{pub}/ui/detections/{x['best_id']}" class="link" style="color:{primary};font:500 12px {FONT};text-decoration:none;padding-left:6px;">Listen</a>
</td></tr></table></td></tr>""")
        parts.append(card("New arrivals", f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>'))

    # Daily totals bar chart ---------------------------------------------
    peak = max([a["daily"].get(d.isoformat(), 0) for d in a["days"]] + [1])
    cols = []
    for d in a["days"]:
        n = a["daily"].get(d.isoformat(), 0)
        hgt = max(3, round(110 * n / peak)) if n else 3
        col = primary if d == a["busiest"] else mix(primary, "#ffffff", .55)
        cols.append(f"""
<td valign="bottom" align="center" width="14%" style="padding:0 4px;">
  <div class="muted" style="font:500 11px {FONT};color:#475569;padding-bottom:4px;">{n:,}</div>
  <div class="{'bar' if d == a['busiest'] else 'bar2'}" style="height:{hgt}px;background:{col};border-radius:6px 6px 2px 2px;font-size:0;line-height:0;">&nbsp;</div>
  <div class="muted" style="font:500 12px {FONT};color:#475569;padding-top:6px;">{d:%a}</div>
</td>""")
    peak_line = ""
    if a["peak_hour"] is not None:
        peak_line = f"Busiest hour across the week: {a['peak_hour']:02d}:00."
    parts.append(card("Daily activity", f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="height:150px;"><tr>{"".join(cols)}</tr></table>', sub=peak_line))

    # Species x hour heatmap (DailySummaryCard style) --------------------
    if a["grid"]:
        gmax = max(max(r["hours"]) for r in a["grid"]) or 1

        def level(n):
            return 0 if n == 0 else min(9, max(1, math.ceil(9 * math.sqrt(n / gmax))))

        head = "".join(
            f'<td align="center" class="muted" style="font:500 9px {MONO};color:#475569;padding:0 0 4px 0;">{h if h % 3 == 0 else ""}</td>'
            for h in range(24))
        rows = []
        for r in a["grid"]:
            cells = "".join(
                f'<td class="h{level(n)}" style="background:{HEAT_LIGHT[level(n)] if n else "#e2e8f0"};'
                f'height:18px;border-radius:3px;font-size:0;line-height:0;" title="{h:02d}:00 · {n}">&nbsp;</td>'
                for h, n in enumerate(r["hours"]))
            rows.append(
                f'<tr><td class="fg" style="font:500 12px {FONT};color:#1f2937;white-space:nowrap;'
                f'padding-right:6px;overflow:hidden;text-overflow:ellipsis;">{E(r["name"])}</td>'
                f'{cells}<td class="muted" align="right" style="font:500 11px {FONT};color:#475569;padding-left:8px;">{r["total"]}</td></tr>')
        legend = "".join(
            f'<td class="h{i}" width="14" style="width:14px;height:10px;background:{HEAT_LIGHT[i]};border-radius:2px;font-size:0;">&nbsp;</td>'
            for i in range(1, 10))
        body = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="2" style="table-layout:fixed;">'
                f'<colgroup><col class="lab" style="width:164px;">{"<col>" * 24}<col style="width:34px;"></colgroup>'
                f'<tr><td></td>{head}<td></td></tr>{"".join(rows)}</table>'
                f'<table role="presentation" cellpadding="0" cellspacing="2" align="right" style="margin-top:8px;"><tr>'
                f'<td class="muted" style="font:11px {FONT};color:#475569;padding-right:4px;">Less</td>{legend}'
                f'<td class="muted" style="font:11px {FONT};color:#475569;padding-left:4px;">More</td></tr></table>')
        parts.append(card("Who sang when", body, sub="Detections by hour across the week, busiest species first.",
                          link=f"{pub}/ui/analytics/activity"))

    # Top species ---------------------------------------------------------
    if a["top"]:
        tmax = a["top"][0]["count"]
        rows = []
        for i, t in enumerate(a["top"], 1):
            w = max(2, round(100 * t["count"] / tmax))
            if t["delta"] is None:
                ch = '<span class="muted" style="color:#475569;">new</span>'
            elif abs(t["delta"]) < .1:
                ch = '<span class="muted" style="color:#475569;">steady</span>'
            else:
                up = t["delta"] > 0
                ch = (f'<span style="color:{"#15803d" if up else "#b91c1c"};font-weight:600;">'
                      f'{"▲" if up else "▼"}{abs(t["delta"]) * 100:.0f}%</span>')
            rows.append(f"""
<tr>
<td width="36" style="padding:6px 0;">{bird_img(t['sci'], 32, 8)}</td>
<td style="padding:6px 0 6px 10px;">
  <div class="fg" style="font:500 13px {FONT};color:#1f2937;padding-bottom:4px;">{E(t['name'])}</div>
  <div class="b300" style="background:#e5e7eb;border-radius:9999px;height:6px;font-size:0;line-height:0;">
    <div class="bar" style="width:{w}%;height:6px;background:{primary};border-radius:9999px;font-size:0;line-height:0;">&nbsp;</div></div>
</td>
<td align="right" class="fg" style="padding:6px 0 6px 12px;font:600 13px {FONT};color:#1f2937;white-space:nowrap;">{t['count']:,}</td>
<td align="right" style="padding:6px 0 6px 10px;font:12px {FONT};white-space:nowrap;width:56px;">{ch}</td>
</tr>""")
        parts.append(card("Top species", f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>',
                          sub="Change is against each species' weekly average over the previous four weeks.",
                          link=f"{pub}/ui/analytics/species"))

    # After dark + gone quiet (side by side on desktop) -------------------
    small = []
    if a["night"]:
        li = "".join(f'<tr><td class="fg" style="font:13px {FONT};color:#1f2937;padding:4px 0;">{E(n)}</td>'
                     f'<td align="right" class="muted" style="font:500 12px {FONT};color:#475569;">{c}</td></tr>'
                     for n, c in a["night"])
        small.append(("After dark", "Detections between sunset and sunrise.", li, f"{pub}/ui/analytics/nocturnal"))
    if a["quiet"]:
        li = "".join(f'<tr><td class="fg" style="font:13px {FONT};color:#1f2937;padding:4px 0;">{E(q["name"])}</td>'
                     f'<td align="right" class="muted" style="font:500 12px {FONT};color:#475569;white-space:nowrap;">'
                     f'{"last " + fmt_date(dt.date.fromisoformat(q["last"])) if q["last"] else ""}</td></tr>'
                     for q in a["quiet"])
        small.append(("Gone quiet", "Regulars of the past month with no detections this week.", li, ""))
    for title, sub, li, link in small:
        parts.append(card(title, f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{li}</table>', sub=sub, link=link))

    if not a["total"]:
        parts.append(card("A quiet week", f'<div class="fg" style="font:14px {FONT};color:#1f2937;">'
                          'No detections came through this week. Worth checking the microphone and the audio source on the station.</div>'))

    # Footer + image credits ---------------------------------------------
    by_lic = defaultdict(set)
    for c in credits.values():
        if c.get("authorName"):
            by_lic[c.get("licenseName") or ""].add(c["authorName"])
    credit_bits = [", ".join(E(n) for n in sorted(names)) + (f" ({E(lic)})" if lic else "")
                   for lic, names in sorted(by_lic.items())]
    credit_html = (f'<div style="padding-top:8px;">Bird photos: {"; ".join(credit_bits)}.</div>' if credit_bits else "")

    title = f"{a['species']} species, {a['total']:,} detections"
    subject = f"{cfg.station} weekly: {title}"
    if a["arrivals"] and a["arrivals"][0]["kind"] == "First ever":
        subject = f"{cfg.station} weekly: new bird, {a['arrivals'][0]['name']}"
    preheader = f"{fmt_range(a['start'], a['end'])}. " + (
        f"Highlight: {h['name']}." if h else "A quiet week.")

    doc = page(cfg, logo, f"Weekly digest · {fmt_range(a['start'], a['end'])}", parts,
               subject, preheader, credit_html)

    text = [f"{cfg.station} weekly digest, {fmt_range(a['start'], a['end'])}",
            f"{a['total']:,} detections, {a['species']} species.", ""]
    if h:
        text.append(f"Highlight: {h['name']} ({h['sci']}), {h['conf']}%  {pub}/ui/detections/{h['det']['id']}")
    for x in a["arrivals"]:
        text.append(f"{x['kind']}: {x['name']} ({x['count']})")
    text.append("")
    text.extend(f"{t['name']}: {t['count']}" for t in a["top"])
    return subject, doc, "\n".join(text), imgs


# --------------------------------------------------------------------------
# Mailgun
# --------------------------------------------------------------------------

def send_mailgun(cfg: Config, subject: str, html_body: str, text: str, imgs: Images) -> None:
    for name in ("MAILGUN_API_KEY", "MAILGUN_DOMAIN", "MAIL_FROM", "MAIL_TO"):
        if not os.environ.get(name):
            sys.exit(f"Missing required environment variable {name}")
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()

    def field(name, value):
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buf.write(value.encode() + b"\r\n")

    field("from", cfg.mail_from)
    for to in cfg.mail_to:
        field("to", to)
    field("subject", subject)
    field("html", html_body)
    field("text", text)
    for fname, (data, mime) in imgs.files.items():
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="inline"; filename="{fname}"\r\n'
                  f'Content-Type: {mime}\r\n\r\n'.encode())
        buf.write(data + b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"{cfg.mg_base}/v3/{cfg.mg_domain}/messages", data=buf.getvalue(), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": "Basic " + base64.b64encode(f"api:{cfg.mg_key}".encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print("Mailgun:", r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"Mailgun rejected the message ({e.code}): {e.read().decode()}")


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def build_test(cfg: Config):
    """A short email that proves Mailgun works and reports what the digest can see."""
    imgs = Images()
    primary, _ = SCHEMES.get(cfg.scheme, SCHEMES["blue"])
    logo = imgs.add("logo.png", (HERE / "assets" / "logo.png").read_bytes())
    api = BirdNET(cfg)
    checks = []  # (ok, label, detail)

    try:
        h = api.get("/api/v2/health")
        ver = h.get("version") or "unknown version"
        db = h.get("database_status", "unknown")
        checks.append((db == "connected", "BirdNET-Go reachable",
                       f"{ver} at {cfg.api}, database {db}"))
        reachable = True
    except Exception as e:  # noqa: BLE001 - any failure here is the finding
        checks.append((False, "BirdNET-Go reachable", f"{cfg.api}: {e}"))
        reachable = False

    if reachable:
        try:
            recent = api.get("/api/v2/detections/recent", limit=1)
            rows = recent.get("data", []) if isinstance(recent, dict) else recent
            if rows:
                d = rows[0]
                checks.append((True, "Latest detection",
                               f"{d.get('commonName')} on {d.get('date')} at {(d.get('time') or '')[:5]}"))
            else:
                checks.append((False, "Latest detection", "No detections stored yet"))
        except Exception as e:  # noqa: BLE001
            checks.append((False, "Latest detection", f"Couldn't read detections: {e}"))
        try:
            today = dt.date.today()
            week = api.summary(today - dt.timedelta(days=6), today)
            n = sum(s.get("count", 0) for s in week)
            checks.append((n > 0, "Past seven days", f"{n:,} detections across {len(week)} species"))
        except Exception as e:  # noqa: BLE001
            checks.append((False, "Past seven days", f"Couldn't read analytics: {e}"))

    when = next_run(cfg, dt.datetime.now())
    checks.append((True, "Mailgun", f"Sending from {cfg.mail_from} via {cfg.mg_domain}"))
    checks.append((True, "Next digest", f"{when:%A %d %B, %H:%M} ({os.environ.get('TZ', 'container time')})"))

    rows = []
    for ok, label, detail in checks:
        dot = SUCCESS if ok else WARNING
        rows.append(f"""
<tr><td style="padding:10px 0;border-top:1px solid #e2e8f0;" class="bd">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td width="18" valign="top" style="padding-top:5px;"><div style="width:10px;height:10px;border-radius:9999px;background:{dot};font-size:0;line-height:0;">&nbsp;</div></td>
<td valign="top">
  <div class="fg" style="font:600 14px {FONT};color:#1f2937;">{E(label)}</div>
  <div class="muted" style="font:13px/1.45 {FONT};color:#475569;padding-top:2px;word-break:break-word;">{E(detail)}</div>
</td></tr></table></td></tr>""")
    all_ok = all(ok for ok, *_ in checks)
    lead = ("Mail is working and the digest can read your station. Your first weekly email will look like the preview."
            if all_ok else
            "Mail is working, but something on the station side needs a look before the weekly digest will be useful.")
    body = (f'<div class="fg" style="font:14px/1.5 {FONT};color:#1f2937;padding-bottom:12px;">{E(lead)}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>')
    parts = [make_card(primary, "Test email", body, link=f"{cfg.public}/ui/dashboard")]
    subject = f"[Test] {cfg.station} weekly digest"
    preheader = "Mailgun is set up. " + ("Station checks passed." if all_ok else "One or more station checks need attention.")
    doc = page(cfg, logo, "Setup check", parts, subject, preheader)
    text = "\n".join([subject, "", lead, ""] + [f"[{'ok' if ok else '!!'}] {l}: {d}" for ok, l, d in checks])
    for ok, label, detail in checks:
        print(f"[{'ok' if ok else '!!'}] {label}: {detail}")
    return subject, doc, text, imgs


def build(cfg: Config, end: dt.date | None = None):
    api = BirdNET(cfg)
    end = end or (dt.date.today() - dt.timedelta(days=1))
    a = analyse(api, cfg, end)
    return render(a, cfg, api)


def preview_html(doc: str, imgs: Images) -> str:
    """Swap cid: references for data: URIs so the file opens in a browser."""
    for name, (data, mime) in imgs.files.items():
        doc = doc.replace(f"cid:{name}", f"data:{mime};base64,{base64.b64encode(data).decode()}")
    return doc


def next_run(cfg: Config, now: dt.datetime) -> dt.datetime:
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    target = days.index(cfg.day) if cfg.day in days else 0
    hh, mm = (int(x) for x in cfg.at.split(":"))
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    cand += dt.timedelta(days=(target - now.weekday()) % 7)
    if cand <= now:
        cand += dt.timedelta(days=7)
    return cand


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preview", metavar="FILE", help="write the email to an HTML file instead of sending")
    ap.add_argument("--now", action="store_true", help="send one digest now and exit")
    ap.add_argument("--test", action="store_true",
                    help="send a short test email that checks Mailgun and the BirdNET-Go connection")
    ap.add_argument("--to", metavar="ADDRESS", help="send to this address instead of MAIL_TO")
    ap.add_argument("--end", metavar="YYYY-MM-DD", help="last day of the week to report (default: yesterday)")
    args = ap.parse_args()
    cfg = Config()
    if args.to:
        cfg.mail_to = [a.strip() for a in args.to.split(",") if a.strip()]
        os.environ["MAIL_TO"] = args.to
    end = dt.date.fromisoformat(args.end) if args.end else None

    if args.preview:
        subject, doc, _text, imgs = build_test(cfg) if args.test else build(cfg, end)
        Path(args.preview).write_text(preview_html(doc, imgs), encoding="utf-8")
        print(f"Wrote {args.preview}  ({subject})")
        return
    if args.test:
        send_mailgun(cfg, *build_test(cfg))
        return
    if args.now:
        send_mailgun(cfg, *build(cfg, end))
        return

    print(f"Scheduler started: every {cfg.day} at {cfg.at} ({os.environ.get('TZ', 'local time')})", flush=True)
    while True:
        when = next_run(cfg, dt.datetime.now())
        print(f"Next digest {when:%a %d %b %H:%M}", flush=True)
        while (wait := (when - dt.datetime.now()).total_seconds()) > 0:
            time.sleep(min(wait, 3600))
        try:
            send_mailgun(cfg, *build(cfg))
        except SystemExit as e:
            print(f"Digest failed: {e}", flush=True)
        except Exception as e:  # keep the scheduler alive through a bad week
            print(f"Digest failed: {e!r}", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()

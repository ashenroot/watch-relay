#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "aprslib==0.7.2",
#   "aiohttp==3.10.11",
# ]
# ///
"""
comms_watch_relay.py
----------------------
The whole server-side of comms-watch.html, in one file on purpose — this
used to be two scripts (a live relay and a separate nightly cache-builder)
plus a second systemd timer, and that was more moving parts than a club
member self-hosting this needed to keep straight. Everything below runs as
one process under one systemd unit. All station-specific configuration
(callsign, grid, relay ports, sweep geometry, anything) comes from
relay.env — nothing about who you are or where you are is hard-coded here.

Three things a browser cannot do on its own, all served over plain HTTP GET
with permissive CORS, matching the poll-based fetch() pattern the rest of
comms-watch.html already uses:

  * APRS-IS  — a raw TCP socket (port 14580), not HTTP. This process holds
    the persistent connection, keeps the most recent position report per
    station heard inside a radius of your grid square, and serves it back
    as JSON.

      GET /aprs.json  -> { generated, grid, center, radius_km, stations: [...] }

  * NetLogger — netlogger.org/api/GetActiveNets.php answers fine, but sends
    no CORS headers, so a browser fetch() to it is silently blocked. This
    process polls it server-side (no CORS restriction between servers),
    converts its XML to JSON, and serves *that* back — respecting
    NetLogger's published one-call-per-minute limit regardless of how often
    browsers ask this relay.

      GET /netlogger.json  -> { generated, nets: [...] }

  * Analog repeaters — RepeaterBook's export API returns 401 auth_missing,
    and — separately, and more to the point — its own export terms
    explicitly forbid the bulk/offline use this needs. Feedline Labs
    (app.feedlinelabs.com, formerly the Amateur Repeater Directory) runs no
    such restriction and needs no key, but its API only answers "what's
    near this one point" — there's no "give me a whole region" endpoint.
    So this process periodically sweeps a grid of sample points across the
    country, unions the results into memory (and a local JSON file, so a
    restart has something to serve immediately instead of an empty panel
    for a day), and answers browser requests by filtering that union
    against whatever grid square they're currently asking about. Two
    clocks, deliberately decoupled: how often the sweep refreshes the data
    (REPEATER_SWEEP_INTERVAL_HOURS, default once a day) has nothing to do
    with how current a query result is against the browser's grid (every
    request is filtered fresh, in memory, against whichever centre point it
    names).

      GET /repeaters.json  -> { generated, source, age_hours, stale, center,
                                 radius_km, count, repeaters: [...] }

Grid Watch (grid-watch.html) rides the same relay, for the same reason —
these all answer an ordinary client and none of them sends a CORS header:

  * CISA KEV — the catalog, split into the two tiers a grid brief wants
    (plant equipment vs. the remote-access kit in front of it) with an EPSS
    score joined onto every entry, so the browser makes one request instead
    of five.

      GET /kev.json  -> { catalog_version, total_entries, tier_a{}, tier_b{} }

  * DOE 202(c) — the emergency order register, parsed into records and each
    one classified as a retirement deferral, event dispatch or capacity
    assurance, with the phrase that decided it attached.

      GET /doe202c.json  -> { ids_on_page, parsed, coverage_note, orders: [...] }

  * NERC — alerts and reliability assessments, as link lists plus page text.

      GET /nerc.json  -> { alerts{links,text_excerpt}, assessments{...} }

  * Briefing digests — the structured block a daily grid brief emits, kept so
    the next run can mark each item NEW, ONGOING or NOT-SEEN against the last
    one. Reads are open; writes need DIGEST_TOKEN.

      GET  /digest.json?limit=30  -> { count, digests: [...] }
      POST /digest.json           -> { stored, received }   (token required)

  GET /health  -> { aprs_connected, stations, netlogger_nets,
                    netlogger_last_poll, repeater_count,
                    repeater_cache_generated, repeater_cache_age_hours,
                    repeater_cache_stale, repeater_last_sweep_ok, uptime_s }

Deploy: see install-aprs-relay.sh — it embeds this exact file and handles
packages, the system user, venv, systemd, nginx, and TLS in one pass, and
is safe to re-run for an upgrade (it detects an existing install and offers
to update in place rather than re-asking every question). This file is
kept standalone too, for reading, auditing, or hand-deploying:

    python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
    cp relay.env.example relay.env   # fill it in
    ./venv/bin/python comms_watch_relay.py

or, with uv (github.com/astral-sh/uv) installed, the dependency block at
the top of this file lets it run with no separate install step at all:

    uv run comms_watch_relay.py
"""

from __future__ import annotations

import asyncio
import email
import html as _html
import imaplib
import io
import json
import logging
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from email import policy as _email_policy
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import aprslib
from aiohttp import ClientSession, ClientTimeout, web

# --------------------------------------------------------------------------
# Config — every parameter in this file comes from here, i.e. from
# relay.env (or real environment variables, which always win). Nothing
# below this block should ever need editing to stand up a new station.
# --------------------------------------------------------------------------

def _load_dotenv(path: str) -> None:
    """Minimal .env loader — no extra dependency for one file of KEY=VALUE
    lines. Existing environment variables always win, so `systemctl
    set-environment` or an EnvironmentFile= directive still overrides this."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv(os.environ.get("RELAY_ENV_FILE", str(Path(__file__).with_name("relay.env"))))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# The APRS and NetLogger feeds need a licensed callsign; the Grid Watch feeds
# need nothing. Someone running this only for grid data is not necessarily a
# ham, and refusing to start without a callsign would shut them out of
# everything. Default true so an existing install behaves exactly as before.
APRS_ENABLED = _env_bool("APRS_ENABLED", True)
NETLOGGER_ENABLED = _env_bool("NETLOGGER_ENABLED", True)
REPEATERS_ENABLED = _env_bool("REPEATERS_ENABLED", True)

CALLSIGN = os.environ.get("CALLSIGN", "").strip().upper()
GRID = os.environ.get("GRID", "").strip().upper()
CENTER_LAT_ENV = os.environ.get("CENTER_LAT", "").strip()
CENTER_LON_ENV = os.environ.get("CENTER_LON", "").strip()
RADIUS_KM = _env_int("RADIUS_KM", 100)

APRS_HOST = os.environ.get("APRS_HOST", "rotate.aprs2.net")
APRS_PORT = _env_int("APRS_PORT", 14580)
APRS_RECONNECT_SECONDS = _env_int("APRS_RECONNECT_SECONDS", 30)

NETLOGGER_URL = os.environ.get(
    "NETLOGGER_URL", "https://netlogger.org/api/GetActiveNets.php"
)
# NetLogger's own published limit is one call/minute with a 429 anti-flood
# rule. 60s is the floor regardless of what's configured, on purpose — this
# relay is what stands between however many browser tabs ask it and NetLogger
# actually seeing one request a minute.
NETLOGGER_POLL_SECONDS = max(60, _env_int("NETLOGGER_POLL_SECONDS", 60))

LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = _env_int("LISTEN_PORT", 8090)
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")

# A dashboard can ask this relay to re-centre its live APRS-IS filter on a
# different grid square (see /aprs.json's ?grid= param below) — e.g. when
# checking conditions somewhere other than home. These two constants keep
# that from thrashing: a request has to actually move the centre by more
# than a rounding error, and re-filters are throttled regardless.
RECENTER_MIN_DELTA_KM = _env_float("RECENTER_MIN_DELTA_KM", 2.0)
RECENTER_COOLDOWN_SECONDS = _env_int("RECENTER_COOLDOWN_SECONDS", 20)

STATION_MAX_AGE_SECONDS = _env_int("STATION_MAX_AGE_SECONDS", 3600)

# ---- Repeater cache: serving side ----------------------------------------
REPEATER_CACHE_FILE = os.environ.get(
    "REPEATER_CACHE_FILE", str(Path(__file__).with_name("repeater_cache.json"))
)
REPEATER_DEFAULT_RADIUS_KM = _env_int("REPEATER_DEFAULT_RADIUS_KM", 160)
REPEATER_DEFAULT_COUNT = _env_int("REPEATER_DEFAULT_COUNT", 300)
# The sweep is expected to succeed at least this often; past it, /repeaters.json
# still answers (stale data beats no data) but flags itself, so the dashboard
# can say the refresh looks like it's failing rather than silently showing
# old rows as if they were current.
REPEATER_MAX_AGE_HOURS = _env_float("REPEATER_MAX_AGE_HOURS", 48.0)

# ---- Repeater cache: sweep side ------------------------------------------
FEEDLINE_BASE = os.environ.get("FEEDLINE_BASE", "https://app.feedlinelabs.com").rstrip("/")
FEEDLINE_ENDPOINT = "/api/radio/repeaters-verbose/{lat}/{lon}"
# aiohttp's default User-Agent ("Python/3.x aiohttp/3.10.11") is generic and
# identical across every script that ever hit a 429 — nothing distinguishes
# a considerate once-a-day sweep from a hammering scraper. Identifying
# ourselves doesn't relax Feedline's rate limit (pacing/concurrency above do
# that), but it's the polite, low-cost thing to do, and if Feedline ever
# wants to reach out about usage, this is how. Override if you'd rather send
# something else (e.g. with your own contact info).
REPEATER_SWEEP_USER_AGENT = os.environ.get(
    "REPEATER_SWEEP_USER_AGENT", "comms-watch-relay/1.0 (ham radio situational-awareness relay; nightly repeater sweep)"
)

# Sweep geometry. Default box is the continental US — Alaska and Hawaii are
# not covered (a stated limitation, not a silent one; see README.md). Point
# spacing is deliberately tighter than the query radius so adjacent sample
# circles overlap — irregular coastline/border shape and Feedline's own
# per-call result cap both eat into nominal coverage.
REPEATER_SWEEP_LAT_MIN = _env_float("REPEATER_SWEEP_LAT_MIN", 24.0)
REPEATER_SWEEP_LAT_MAX = _env_float("REPEATER_SWEEP_LAT_MAX", 49.5)
REPEATER_SWEEP_LON_MIN = _env_float("REPEATER_SWEEP_LON_MIN", -125.0)
REPEATER_SWEEP_LON_MAX = _env_float("REPEATER_SWEEP_LON_MAX", -66.5)
REPEATER_SWEEP_SPACING_MI = _env_float("REPEATER_SWEEP_SPACING_MI", 150.0)
REPEATER_SWEEP_RANGE_MI = _env_int("REPEATER_SWEEP_RANGE_MI", 100)
REPEATER_SWEEP_COUNT = _env_int("REPEATER_SWEEP_COUNT", 200)
# Feedline does rate-limit (confirmed via a live 429 — an earlier, much
# smaller test sweep never triggered it, which is how "no known rate limit"
# ended up stated below before this was caught). Its 429 response carries a
# Retry-After that counted down from ~10s, and three points fired ~400ms
# apart all got 429'd immediately — consistent with a window on the order
# of 10s, not simple per-request throttling. So this runs fully serial
# (concurrency 1) with a pacing floor well past that window: slower sweeps
# (a full nationwide one now takes on the order of an hour, not minutes),
# but it only runs once a day and reliability matters more than speed here.
REPEATER_SWEEP_CONCURRENCY = _env_int("REPEATER_SWEEP_CONCURRENCY", 1)
REPEATER_SWEEP_PACING_MS = _env_int("REPEATER_SWEEP_PACING_MS", 12000)
REPEATER_SWEEP_TIMEOUT_S = _env_int("REPEATER_SWEEP_TIMEOUT_S", 20)
REPEATER_SWEEP_RETRIES = _env_int("REPEATER_SWEEP_RETRIES", 1)
# A 429 gets its own, more generous retry budget and backoff than an
# ordinary network hiccup — pacing should mostly prevent 429s outright, so
# this is a safety net for whatever timing still slips through, not the
# primary defense.
REPEATER_SWEEP_429_MAX_RETRIES = _env_int("REPEATER_SWEEP_429_MAX_RETRIES", 4)
REPEATER_SWEEP_429_BACKOFF_S = _env_float("REPEATER_SWEEP_429_BACKOFF_S", 8.0)
# A run that can't reach at least this fraction of its sample points is
# treated as failed rather than as "the country got quieter" — the existing
# cache (in memory and on disk) is left exactly as it was.
REPEATER_SWEEP_MIN_OK_FRACTION = _env_float("REPEATER_SWEEP_MIN_OK_FRACTION", 0.5)
# How often the sweep re-runs, and how much to jitter that interval (as a
# fraction of it) so a fleet of club members' relays — most of which will
# have started up around the same time someone posted the install steps —
# doesn't settle into hitting Feedline at the exact same moment every day.
REPEATER_SWEEP_INTERVAL_HOURS = _env_float("REPEATER_SWEEP_INTERVAL_HOURS", 24.0)
REPEATER_SWEEP_JITTER_FRACTION = _env_float("REPEATER_SWEEP_JITTER_FRACTION", 0.15)
# Run one sweep shortly after startup rather than waiting a full interval —
# otherwise a fresh install shows an empty repeater panel for up to a day.
REPEATER_SWEEP_ON_STARTUP = _env_bool("REPEATER_SWEEP_ON_STARTUP", True)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("relay")


# --------------------------------------------------------------------------
# Maidenhead grid -> lat/lon, mirroring comms-watch.html's gridToLatLon()
# exactly, so the APRS-IS filter is centred on the same point the dashboard
# calls "your grid square".
# --------------------------------------------------------------------------

_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$")


def grid_to_lat_lon(g: str) -> tuple[float, float] | None:
    s = (g or "").strip().upper()
    if not _GRID_RE.match(s):
        return None
    lon = (ord(s[0]) - 65) * 20 - 180
    lat = (ord(s[1]) - 65) * 10 - 90
    lon += int(s[2]) * 2
    lat += int(s[3]) * 1
    if len(s) == 6:
        lon += (ord(s[4]) - 65) * (2 / 24)
        lat += (ord(s[5]) - 65) * (1 / 24)
        return (lat + 0.5 / 24, lon + 1 / 24)
    return (lat + 0.5, lon + 1)


def resolve_center() -> tuple[float, float]:
    if CENTER_LAT_ENV and CENTER_LON_ENV:
        try:
            return (float(CENTER_LAT_ENV), float(CENTER_LON_ENV))
        except ValueError:
            pass
    if GRID:
        ll = grid_to_lat_lon(GRID)
        if ll:
            return ll
    raise SystemExit(
        "Set GRID (e.g. EM73) or both CENTER_LAT and CENTER_LON in relay.env — "
        "the APRS-IS filter (and the default repeater search centre) needs one."
    )


def center_or_none() -> tuple[float, float] | None:
    """resolve_center() raises SystemExit, which is right at startup and wrong
    inside a request — it kills the handler and surfaces as a dead socket
    rather than an answer. Handlers use this instead."""
    try:
        return resolve_center()
    except SystemExit:
        return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Mirrors comms-watch.html's own haversineKm() — used to decide whether
    a requested APRS-IS centre has moved enough to bother re-filtering, and
    to sort/filter the repeater cache by distance from a query point."""
    r = math.pi / 180
    dlat = (lat2 - lat1) * r
    dlon = (lon2 - lon1) * r
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin(dlon / 2) ** 2
    return 2 * 6371 * math.asin(min(1, math.sqrt(h)))


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# APRS-IS worker — runs in its own thread because aprslib's consumer loop is
# blocking. Shared state is a plain dict behind a lock; reads from the
# asyncio HTTP handlers are quick enough that this is not a bottleneck.
# --------------------------------------------------------------------------

class AprsState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stations: dict[str, dict[str, Any]] = {}
        self.connected = False
        self.last_error: str | None = None

    def update(self, call: str, fields: dict[str, Any]) -> None:
        with self.lock:
            self.stations[call] = fields

    def snapshot(self, max_age_seconds: int) -> list[dict[str, Any]]:
        cutoff = time.time() - max_age_seconds
        with self.lock:
            items = [
                {**v, "call": k, "last_heard": iso(v["_ts"]), "age_s": int(time.time() - v["_ts"])}
                for k, v in self.stations.items()
                if v["_ts"] >= cutoff
            ]
        for it in items:
            it.pop("_ts", None)
        return sorted(items, key=lambda s: s["age_s"])

    def clear(self) -> None:
        """Drop everything heard under the previous filter. Called on a
        recentre — stations from the old area are not wrong, just no longer
        relevant, and leaving them in would make the new area's picture look
        busier than it actually is until they age out an hour later."""
        with self.lock:
            self.stations.clear()


APRS_STATE = AprsState()


class FilterState:
    """The APRS-IS filter is normally whatever GRID/CENTER_LAT/CENTER_LON
    resolved to at startup, but a dashboard can move it live by passing
    ?grid= (or ?lat=&lon=) to /aprs.json — e.g. checking a different area
    than home. This holds whichever centre is actually active right now,
    plus a handle to the live connection so a recentre can be applied
    in-place via aprslib's own filter-resend mechanism instead of a
    reconnect. All access is under `lock` since the HTTP handlers run on
    the asyncio event loop while the APRS-IS worker runs in its own thread."""

    def __init__(self, center: tuple[float, float], radius_km: int) -> None:
        self.lock = threading.Lock()
        self.center = center
        self.radius_km = radius_km
        self.ais: aprslib.IS | None = None
        self.last_recenter_ts = 0.0


FILTER_STATE: FilterState


def maybe_recenter(lat: float, lon: float) -> bool:
    """Move the live APRS-IS filter to (lat, lon) if it isn't already there,
    subject to a minimum-movement threshold and a cooldown. Returns True if
    a recentre was actually applied. Safe to call from the asyncio side —
    aprslib's set_filter() both sends the change on the live socket *and*
    updates the value it will use on any future automatic reconnect, so one
    call here is all a recentre ever needs."""
    with FILTER_STATE.lock:
        cur_lat, cur_lon = FILTER_STATE.center
        moved_km = haversine_km(cur_lat, cur_lon, lat, lon)
        if moved_km <= RECENTER_MIN_DELTA_KM:
            return False
        now = time.time()
        if now - FILTER_STATE.last_recenter_ts < RECENTER_COOLDOWN_SECONDS:
            return False
        ais = FILTER_STATE.ais
        filter_text = f"r/{lat:.4f}/{lon:.4f}/{FILTER_STATE.radius_km}"
        if ais is not None:
            try:
                ais.set_filter(filter_text)
            except Exception as exc:  # noqa: BLE001 — a failed live resend just
                # means the next reconnect (or next recentre attempt) picks
                # it up; it must not take the worker thread down.
                log.warning("APRS-IS: failed to apply live filter change: %s", exc)
                return False
        FILTER_STATE.center = (lat, lon)
        FILTER_STATE.last_recenter_ts = now
    log.info("APRS-IS: recentring filter to %s (moved %.1f km)", filter_text, moved_km)
    APRS_STATE.clear()
    return True


def aprs_worker() -> None:
    passcode = aprslib.passcode(CALLSIGN)

    def on_packet(packet: dict[str, Any]) -> None:
        if packet.get("latitude") is None or packet.get("longitude") is None:
            return  # not a position report — status, telemetry, message, etc.
        call = packet.get("from") or "UNKNOWN"
        APRS_STATE.update(call, {
            "lat": packet["latitude"],
            "lon": packet["longitude"],
            "symbol": packet.get("symbol"),
            "symbol_table": packet.get("symbol_table"),
            "comment": packet.get("comment"),
            "course": packet.get("course"),
            "speed": packet.get("speed"),
            "altitude": packet.get("altitude"),
            "path": packet.get("path"),
            "_ts": time.time(),
        })

    while True:
        with FILTER_STATE.lock:
            lat, lon = FILTER_STATE.center
            radius = FILTER_STATE.radius_km
        filter_text = f"r/{lat:.4f}/{lon:.4f}/{radius}"
        log.info(
            "APRS-IS: connecting to %s:%s as %s (passcode %s), filter %s",
            APRS_HOST, APRS_PORT, CALLSIGN, passcode, filter_text,
        )
        try:
            ais = aprslib.IS(CALLSIGN, passwd=passcode, host=APRS_HOST, port=APRS_PORT)
            ais.set_filter(filter_text)
            ais.connect(blocking=True, retry=APRS_RECONNECT_SECONDS)
            with FILTER_STATE.lock:
                FILTER_STATE.ais = ais
            APRS_STATE.connected = True
            APRS_STATE.last_error = None
            log.info("APRS-IS: connected and logged in")
            # immortal=True makes this call itself reconnect forever on a
            # dropped connection; it only returns (or raises) on a real
            # failure this loop's outer try/except should log and retry.
            # Any recentre applied while connected lives on self.filter, so
            # even an immortal-triggered reconnect keeps the new centre —
            # only a full new IS object (the outer loop below) needs
            # FILTER_STATE re-read, which it does at the top of this loop.
            ais.consumer(on_packet, blocking=True, immortal=True)
        except Exception as exc:  # noqa: BLE001 — this thread must never die silently
            APRS_STATE.connected = False
            APRS_STATE.last_error = str(exc)
            with FILTER_STATE.lock:
                FILTER_STATE.ais = None
            log.warning("APRS-IS: %s — reconnecting in %ss", exc, APRS_RECONNECT_SECONDS)
            time.sleep(APRS_RECONNECT_SECONDS)


# --------------------------------------------------------------------------
# NetLogger poller — plain asyncio task on the main event loop. Server-to-
# server HTTP has no CORS restriction; the restriction NetLogger triggers is
# browser-only, which is exactly why this needs to run here and not in the
# dashboard's JS.
# --------------------------------------------------------------------------

class NetloggerState:
    def __init__(self) -> None:
        self.nets: list[dict[str, Any]] = []
        self.generated: str | None = None
        self.last_poll_ok: bool = False
        self.last_error: str | None = None


NETLOGGER_STATE = NetloggerState()


def _text(el: ET.Element | None, tag: str) -> str | None:
    if el is None:
        return None
    child = el.find(tag)
    return child.text.strip() if child is not None and child.text else None


def _parse_netlogger_xml(body: str) -> list[dict[str, Any]]:
    root = ET.fromstring(body)
    nets: list[dict[str, Any]] = []
    for server in root.findall("./ServerList/Server"):
        server_name = _text(server, "ServerName") or "?"
        for net in server.findall("Net"):
            date_raw = _text(net, "Date")  # "YYYY-MM-DD HH:MM:SS", already UTC
            started = None
            if date_raw:
                try:
                    started = (
                        datetime.strptime(date_raw, "%Y-%m-%d %H:%M:%S")
                        .replace(tzinfo=timezone.utc)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z")
                    )
                except ValueError:
                    started = date_raw
            subs = _text(net, "SubscriberCount")
            nets.append({
                "server": server_name,
                "name": _text(net, "NetName"),
                "alt_name": _text(net, "AltNetName"),
                "frequency": _text(net, "Frequency"),
                "band": _text(net, "Band"),
                "mode": _text(net, "Mode"),
                "net_control": _text(net, "NetControl"),
                "logger": _text(net, "Logger"),
                "started": started,
                "subscribers": int(subs) if subs and subs.isdigit() else None,
            })
    return nets


async def netlogger_poll_loop(session: ClientSession) -> None:
    if not NETLOGGER_ENABLED:
        return
    while True:
        try:
            async with session.get(NETLOGGER_URL, timeout=ClientTimeout(total=20)) as resp:
                resp.raise_for_status()
                body = await resp.text()
            nets = _parse_netlogger_xml(body)
            NETLOGGER_STATE.nets = nets
            NETLOGGER_STATE.generated = iso(time.time())
            NETLOGGER_STATE.last_poll_ok = True
            NETLOGGER_STATE.last_error = None
            log.info("NetLogger: %d active net(s)", len(nets))
        except Exception as exc:  # noqa: BLE001
            NETLOGGER_STATE.last_poll_ok = False
            NETLOGGER_STATE.last_error = str(exc)
            log.warning("NetLogger: poll failed: %s", exc)
        await asyncio.sleep(NETLOGGER_POLL_SECONDS)


# --------------------------------------------------------------------------
# Repeater cache — sweep side. Feedline Labs only answers "what's near this
# one point", so building a region-wide picture means sampling many points
# ahead of time and unioning the results. See the module docstring for why
# this lives in-process instead of as a separate nightly script.
# --------------------------------------------------------------------------

def repeater_sample_points() -> list[tuple[float, float]]:
    """Latitude steps are constant (69 mi/degree everywhere); longitude
    steps widen with latitude (a degree of longitude shrinks by cos(lat))
    so physical spacing stays roughly uniform east-west instead of
    oversampling the Canadian border and undersampling the Gulf coast. Rows
    are staggered by half a longitude step for a rough hex pattern — fewer
    points for the same coverage than a plain square grid."""
    lat_step = REPEATER_SWEEP_SPACING_MI / 69.0
    points: list[tuple[float, float]] = []
    lat = REPEATER_SWEEP_LAT_MIN
    row = 0
    while lat <= REPEATER_SWEEP_LAT_MAX:
        lon_step = lat_step / max(0.15, math.cos(math.radians(lat)))
        offset = (lon_step / 2.0) if (row % 2) else 0.0
        lon = REPEATER_SWEEP_LON_MIN + offset
        while lon <= REPEATER_SWEEP_LON_MAX:
            points.append((round(lat, 4), round(lon, 4)))
            lon += lon_step
        lat += lat_step
        row += 1
    return points


def repeater_normalise(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Feedline's field names are terse (built for a small JSON payload, not
    for reading) — expanded here to something the relay and dashboard don't
    have to decode from a wiki page. Anything missing an id or a position is
    dropped: it can't be deduplicated or distance-filtered later."""
    rid = raw.get("id")
    lat, lon = raw.get("la"), raw.get("lo")
    if rid is None or lat is None or lon is None:
        return None
    return {
        "id": rid,
        "source": "feedline",
        "callsign": raw.get("cs"),
        "service": raw.get("st"),
        "city": raw.get("ci"),
        "state": raw.get("sr"),
        "freq_out": raw.get("fo"),
        "freq_in": raw.get("fi"),
        "offset_mhz": raw.get("of"),
        "offset_sign": raw.get("os"),
        "tone_mode": raw.get("tm"),
        "tone_tx": raw.get("tx"),
        "tone_rx": raw.get("rx"),
        "lat": lat,
        "lon": lon,
        "updated": raw.get("lu"),
    }


class _RequestPacer:
    """Enforces a minimum gap between request *starts*, globally, no matter
    how many workers are calling it concurrently. A semaphore alone bounds
    how many requests are in flight at once; it does nothing to bound how
    fast new ones start, and Feedline's 429s are a request-rate limit, not
    a concurrency one — confirmed live (see REPEATER_SWEEP_PACING_MS)."""

    def __init__(self, interval_s: float) -> None:
        self.interval_s = interval_s
        self.lock = asyncio.Lock()
        self.next_ok = 0.0

    async def wait(self) -> None:
        async with self.lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            delay = max(0.0, self.next_ok - now)
            self.next_ok = max(now, self.next_ok) + self.interval_s
        if delay:
            await asyncio.sleep(delay)


async def _fetch_repeater_point(
    session: ClientSession, sem: asyncio.Semaphore, pacer: _RequestPacer, lat: float, lon: float
) -> list[dict[str, Any]] | None:
    url = FEEDLINE_BASE + FEEDLINE_ENDPOINT.format(lat=lat, lon=lon)
    params = {"rangeMiles": REPEATER_SWEEP_RANGE_MI, "count": REPEATER_SWEEP_COUNT}
    attempt = 0
    rate_limit_attempt = 0
    async with sem:
        while True:
            attempt += 1
            await pacer.wait()
            try:
                async with session.get(
                    url, params=params, timeout=ClientTimeout(total=REPEATER_SWEEP_TIMEOUT_S)
                ) as resp:
                    if resp.status == 429:
                        rate_limit_attempt += 1
                        if rate_limit_attempt > REPEATER_SWEEP_429_MAX_RETRIES:
                            log.warning(
                                "repeater sweep (%s, %s): still rate-limited (429) after %d attempt(s), giving up",
                                lat, lon, rate_limit_attempt,
                            )
                            return None
                        backoff = REPEATER_SWEEP_429_BACKOFF_S
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            try:
                                backoff = max(backoff, float(retry_after))
                            except ValueError:
                                pass
                        log.info("repeater sweep (%s, %s): rate-limited (429) — backing off %.0fs",
                                 lat, lon, backoff)
                        await asyncio.sleep(backoff)
                        continue
                    resp.raise_for_status()
                    body = await resp.json(content_type=None)
                if isinstance(body, list):
                    return body
                log.warning("repeater sweep (%s, %s): unexpected response shape (%s)",
                            lat, lon, type(body).__name__)
                return None
            except Exception as exc:  # noqa: BLE001 — one bad point must not sink the sweep
                if attempt > REPEATER_SWEEP_RETRIES:
                    log.warning("repeater sweep (%s, %s): failed after %d attempt(s): %s",
                                lat, lon, attempt, exc)
                    return None
                await asyncio.sleep(1.0)


class RepeaterCache:
    """Holds the merged repeater set in memory — the only copy the HTTP
    handler ever reads from. The on-disk file exists purely so a restart has
    something to serve immediately instead of an empty panel until the next
    sweep completes; once running, the sweep task updates this object
    directly and nothing re-reads the file."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.records: dict[str, dict[str, Any]] = {}
        self.generated: str | None = None
        self.source_label: str | None = None
        self.sweep_points_total = 0
        self.sweep_points_ok = 0
        self.last_sweep_ok: bool | None = None
        self.last_sweep_error: str | None = None

    def load_from_disk(self) -> None:
        p = Path(REPEATER_CACHE_FILE)
        if not p.is_file():
            log.info("repeater cache: no existing %s — starting empty until the first sweep", p)
            return
        try:
            payload = json.loads(p.read_text())
            records = {str(r["id"]): r for r in payload.get("repeaters", []) if r.get("id") is not None}
        except Exception as exc:  # noqa: BLE001 — a corrupt file must not crash startup
            log.warning("repeater cache: could not read %s: %s — starting empty", p, exc)
            return
        with self.lock:
            self.records = records
            self.generated = payload.get("generated")
            self.source_label = payload.get("source")
            self.sweep_points_total = payload.get("sweep_points_total", 0)
            self.sweep_points_ok = payload.get("sweep_points_ok", 0)
        log.info("repeater cache: loaded %d repeater(s) from disk, generated %s", len(records), self.generated)

    def save_to_disk(self) -> None:
        with self.lock:
            payload = {
                "generated": self.generated,
                "source": self.source_label,
                "sweep_points_total": self.sweep_points_total,
                "sweep_points_ok": self.sweep_points_ok,
                "repeaters": list(self.records.values()),
            }
        p = Path(REPEATER_CACHE_FILE)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, p)  # atomic on the same filesystem — no reader ever sees a half-written file

    def apply_sweep(self, records: dict[str, dict[str, Any]], ok_points: int, total_points: int) -> None:
        with self.lock:
            self.records = records
            self.generated = iso(time.time())
            self.source_label = "Feedline Labs (Greyline API), app.feedlinelabs.com — no key required"
            self.sweep_points_total = total_points
            self.sweep_points_ok = ok_points
            self.last_sweep_ok = True
            self.last_sweep_error = None
        self.save_to_disk()

    def mark_sweep_failed(self, reason: str) -> None:
        with self.lock:
            self.last_sweep_ok = False
            self.last_sweep_error = reason

    def query(
        self, lat: float, lon: float, radius_km: float, service: str | None, count: int
    ) -> dict[str, Any]:
        with self.lock:
            records = list(self.records.values())
            generated = self.generated
        rows = []
        for r in records:
            if service and str(r.get("service") or "").lower() != service.lower():
                continue
            km = haversine_km(lat, lon, r["lat"], r["lon"])
            if km > radius_km:
                continue
            rows.append({**r, "distance_km": round(km, 1)})
        rows.sort(key=lambda r: r["distance_km"])
        rows = rows[:count]
        generated_ts = _parse_iso(generated)
        age_hours = (time.time() - generated_ts) / 3600.0 if generated_ts else None
        return {
            "generated": generated,
            "source": self.source_label,
            "age_hours": round(age_hours, 1) if age_hours is not None else None,
            "stale": (age_hours is not None and age_hours > REPEATER_MAX_AGE_HOURS) or generated is None,
            "center": {"lat": lat, "lon": lon},
            "radius_km": radius_km,
            "count": len(rows),
            "repeaters": rows,
        }


REPEATER_CACHE = RepeaterCache()


async def run_repeater_sweep() -> None:
    points = repeater_sample_points()
    total = len(points)
    sem = asyncio.Semaphore(REPEATER_SWEEP_CONCURRENCY)
    pacer = _RequestPacer(REPEATER_SWEEP_PACING_MS / 1000.0)
    merged: dict[str, dict[str, Any]] = {}
    ok = 0
    done = 0
    log.info("repeater sweep: starting, %d sample point(s)", total)
    t0 = time.time()
    async with ClientSession(headers={"User-Agent": REPEATER_SWEEP_USER_AGENT}) as session:

        # At concurrency 1 a sweep can now take the better part of an hour,
        # so each point logs as it finishes (success or not) — enough to
        # watch it progress live via `journalctl -u aprs-relay -f` instead
        # of waiting for the whole thing to either finish or fail.
        async def _fetch_and_log(lat: float, lon: float) -> list[dict[str, Any]] | None:
            nonlocal done
            res = await _fetch_repeater_point(session, sem, pacer, lat, lon)
            done += 1
            outcome = f"{len(res)} repeater(s)" if res is not None else "failed"
            log.info("repeater sweep: point %d/%d done (%s, %s) -> %s", done, total, lat, lon, outcome)
            return res

        results = await asyncio.gather(*(_fetch_and_log(lat, lon) for lat, lon in points))
    for res in results:
        if res is None:
            continue
        ok += 1
        for raw in res:
            n = repeater_normalise(raw)
            if n is not None:
                merged[str(n["id"])] = n
    min_ok = max(1, int(len(points) * REPEATER_SWEEP_MIN_OK_FRACTION))
    if ok < min_ok or not merged:
        reason = f"only {ok}/{len(points)} sample point(s) answered (need >= {min_ok}), {len(merged)} repeater(s) found"
        log.error("repeater sweep: looks bad (%s) — leaving the existing cache untouched", reason)
        REPEATER_CACHE.mark_sweep_failed(reason)
        return
    REPEATER_CACHE.apply_sweep(merged, ok, len(points))
    log.info(
        "repeater sweep: done in %.0fs — %d repeater(s) from %d/%d points",
        time.time() - t0, len(merged), ok, len(points),
    )


async def repeater_sweep_loop() -> None:
    if not REPEATERS_ENABLED:
        return
    REPEATER_CACHE.load_from_disk()
    if REPEATER_SWEEP_ON_STARTUP:
        await run_repeater_sweep()
    while True:
        jitter = 1.0 + REPEATER_SWEEP_JITTER_FRACTION * (2 * (os.urandom(1)[0] / 255) - 1)
        await asyncio.sleep(REPEATER_SWEEP_INTERVAL_HOURS * 3600 * jitter)
        await run_repeater_sweep()


# --------------------------------------------------------------------------
# HTTP surface


# ==========================================================================
# Grid Watch feeds
#
# Same problem as NetLogger, four more times: CISA's KEV catalog, the DOE
# 202(c) order register and NERC's alert and assessment pages all answer any
# ordinary client and none of them sends an Access-Control-Allow-Origin
# header, so a browser fetch() to them is blocked before it starts. This
# process polls them server-side on a slow clock and serves the results back
# as JSON with the same permissive CORS the rest of the relay uses.
#
# One of them is more than a proxy. /kev.json does the join a browser would
# otherwise make four extra round trips for: it splits the catalog into the
# two tiers a grid brief actually wants and attaches an EPSS score to every
# entry. That split matters — see the comment on ICS_VENDORS.
# ==========================================================================

def urllib_quote(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_.~" else "%%%02X" % b
                   for c in s for b in [ord(c)])


GRIDWATCH_USER_AGENT = os.environ.get(
    "GRIDWATCH_USER_AGENT",
    "comms-watch-relay/1.0 (self-hosted dashboard relay; polite scheduled polling)")

GRIDWATCH_ENABLED = _env_bool("GRIDWATCH_ENABLED", True)
KEV_URL = os.environ.get(
    "KEV_URL",
    "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json")
EPSS_URL = os.environ.get("EPSS_URL", "https://api.first.org/data/v1/epss")
DOE_URL = os.environ.get("DOE_URL", "https://www.energy.gov/ceser/2026-doe-202c-orders")
NERC_ALERTS_URL = os.environ.get(
    "NERC_ALERTS_URL", "https://www.nerc.com/pa/rrm/bpsa/Pages/Alerts.aspx")
NERC_RA_URL = os.environ.get(
    "NERC_RA_URL", "https://www.nerc.com/pa/RAPA/ra/Pages/default.aspx")

KEV_POLL_SECONDS = max(900, int(_env_float("KEV_POLL_HOURS", 1.0) * 3600))
DOE_POLL_SECONDS = max(3600, int(_env_float("DOE_POLL_HOURS", 6.0) * 3600))
NERC_POLL_SECONDS = max(3600, int(_env_float("NERC_POLL_HOURS", 6.0) * 3600))

DIGEST_TOKEN = os.environ.get("DIGEST_TOKEN", "").strip()
DIGEST_MAX = _env_int("DIGEST_MAX", 400)
DIGEST_PATH = Path(os.environ.get(
    "DIGEST_PATH", str(Path(__file__).with_name("digests.jsonl"))))

BRIEFMAIL_ENABLED = _env_bool("BRIEFMAIL_ENABLED", False)
BRIEFMAIL_MAILDIR = os.environ.get("BRIEFMAIL_MAILDIR", "").strip()
BRIEFMAIL_IMAP_HOST = os.environ.get("BRIEFMAIL_IMAP_HOST", "").strip()
BRIEFMAIL_IMAP_PORT = _env_int("BRIEFMAIL_IMAP_PORT", 993)
BRIEFMAIL_IMAP_SSL = _env_bool("BRIEFMAIL_IMAP_SSL", True)
BRIEFMAIL_IMAP_USER = os.environ.get("BRIEFMAIL_IMAP_USER", "").strip()
BRIEFMAIL_IMAP_PASSWORD = os.environ.get("BRIEFMAIL_IMAP_PASSWORD", "")
BRIEFMAIL_IMAP_FOLDER = os.environ.get("BRIEFMAIL_IMAP_FOLDER", "INBOX").strip() or "INBOX"
BRIEFMAIL_ARCHIVE_FOLDER = os.environ.get("BRIEFMAIL_ARCHIVE_FOLDER", "").strip()
BRIEFMAIL_POLL_SECONDS = max(60, int(_env_float("BRIEFMAIL_POLL_MINUTES", 10.0) * 60))
BRIEFMAIL_SUBJECT_MATCH = os.environ.get(
    "BRIEFMAIL_SUBJECT_MATCH", "Daily Preparedness Briefing").strip()
BRIEFMAIL_FROM_ALLOW = [a.strip().lower() for a in
                        os.environ.get("BRIEFMAIL_FROM_ALLOW", "").split(",") if a.strip()]
BRIEFMAIL_SECRET = os.environ.get("BRIEFMAIL_SECRET", "").strip()
BRIEFMAIL_KEEP_TEXT = _env_bool("BRIEFMAIL_KEEP_TEXT", True)
BRIEFMAIL_MAX_BYTES = _env_int("BRIEFMAIL_MAX_BYTES", 6_000_000)
BRIEFMAIL_BACKFILL = _env_int("BRIEFMAIL_BACKFILL", 20)

# Vendors whose products are the plant itself. Matching is on the KEV
# `vendorProject` field ALONE and never on vendor+product concatenated:
# substring-matching the pair looks equivalent and quietly fills this tier
# with false positives — "abb" matches inside "Zabbix", "ge " inside a dozen
# unrelated product names. The tier is small by nature (a handful of entries
# out of ~1,700), and that is the point: it barely moves, so an addition to
# it is worth noticing on its own.
ICS_VENDORS = {
    "siemens", "schneider electric", "rockwell", "rockwell automation", "abb",
    "hitachi energy", "emerson", "honeywell", "mitsubishi electric", "advantech",
    "moxa", "unitronics", "delta electronics", "automated logic",
    "johnson controls", "trane", "yokogawa", "omron", "wago", "phoenix contact",
    "schweitzer engineering laboratories", "osisoft", "aveva", "iconics",
    "red lion", "tridium", "codesys", "general electric", "ge digital", "festo",
    "beckhoff", "inductive automation",
}
# Remote-access and edge appliances: not plant equipment, but what sits
# between the internet and plant equipment. Several hundred entries, so this
# tier gets an ordering — EPSS, i.e. probability of exploitation in the next
# 30 days — where the OT tier is short enough to read whole.
EDGE_VENDORS = {
    "ivanti", "fortinet", "citrix", "palo alto networks", "sonicwall",
    "pulse secure", "f5", "cisco", "vmware", "progress", "barracuda",
    "check point", "juniper", "sophos", "array networks", "zyxel", "qnap",
}


class FeedState:
    """One polled upstream. Holds the last good payload and, separately,
    whether the last attempt succeeded — a dashboard needs to tell a feed
    that is quiet from one that is broken, and collapsing those two into a
    single value is how a stale number ends up being read as current."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.lock = threading.Lock()
        self.payload: Any = None
        self.generated: str | None = None
        self.last_poll_ok: bool | None = None
        self.last_error: str | None = None
        self.last_attempt: str | None = None

    def set(self, payload: Any) -> None:
        with self.lock:
            self.payload = payload
            self.generated = iso(time.time())
            self.last_poll_ok = True
            self.last_error = None
            self.last_attempt = self.generated

    def fail(self, err: str) -> None:
        with self.lock:
            self.last_poll_ok = False
            self.last_error = err
            self.last_attempt = iso(time.time())

    def envelope(self) -> dict[str, Any]:
        with self.lock:
            age = None
            g = _parse_iso(self.generated)
            if g is not None:
                age = round(time.time() - g)
            return {
                "generated": self.generated,
                "age_s": age,
                "ok": self.last_poll_ok,
                "last_error": self.last_error,
                "last_attempt": self.last_attempt,
                "have_data": self.payload is not None,
            }


KEV_STATE = FeedState("kev")
DOE_STATE = FeedState("doe202c")
NERC_STATE = FeedState("nerc")


async def _get(session: ClientSession, url: str, as_json: bool = False) -> Any:
    async with session.get(url, timeout=ClientTimeout(total=90),
                           headers={"User-Agent": GRIDWATCH_USER_AGENT}) as r:
        r.raise_for_status()
        return (await r.json(content_type=None)) if as_json else (await r.text())


async def fetch_epss(session: ClientSession, cves: list[str]) -> dict[str, float]:
    """EPSS in batches of 100. Partial results are returned as partial: a CVE
    with no score comes back absent, which the dashboard renders as unknown,
    never as zero."""
    out: dict[str, float] = {}
    cves = [c for c in cves if c]
    for i in range(0, len(cves), 100):
        chunk = cves[i:i + 100]
        url = "%s?cve=%s&limit=200" % (EPSS_URL, ",".join(chunk))
        try:
            data = await _get(session, url, as_json=True)
            for row in data.get("data", []):
                try:
                    out[row["cve"]] = float(row["epss"])
                except (KeyError, TypeError, ValueError):
                    continue
        except Exception as e:  # one bad batch must not lose the other 200
            log.warning("EPSS batch %d failed: %s", i // 100, e)
        await asyncio.sleep(0.3)
    return out


async def kev_poll_loop(session: ClientSession) -> None:
    if not GRIDWATCH_ENABLED:
        return
    while True:
        try:
            cat = await _get(session, KEV_URL, as_json=True)
            vulns = cat.get("vulnerabilities", [])
            tier_a, tier_b = [], []
            for v in vulns:
                vend = (v.get("vendorProject") or "").strip().lower()
                bucket = tier_a if vend in ICS_VENDORS else (
                    tier_b if vend in EDGE_VENDORS else None)
                if bucket is None:
                    continue
                bucket.append({
                    "cve": v.get("cveID"),
                    "vendor": v.get("vendorProject"),
                    "product": v.get("product"),
                    "name": v.get("vulnerabilityName"),
                    "added": v.get("dateAdded"),
                    "due": v.get("dueDate"),
                    # CISA writes "Unknown" here far more often than "Known".
                    # Passed through verbatim: Unknown is not "no".
                    "ransomware": v.get("knownRansomwareCampaignUse"),
                    "notes": v.get("shortDescription"),
                })
            scores = await fetch_epss(session, [r["cve"] for r in tier_a + tier_b])
            for r in tier_a + tier_b:
                r["epss"] = scores.get(r["cve"])
            tier_a.sort(key=lambda r: r["added"] or "")
            tier_b.sort(key=lambda r: (r["epss"] is None, -(r["epss"] or 0)))
            KEV_STATE.set({
                "catalog_version": cat.get("catalogVersion"),
                "released": cat.get("dateReleased"),
                "total_entries": cat.get("count", len(vulns)),
                "epss_scored": sum(1 for r in tier_a + tier_b if r["epss"] is not None),
                "tier_a": {
                    "label": "Operational technology",
                    "count": len(tier_a),
                    "entries": tier_a,
                },
                "tier_b": {
                    "label": "Utility perimeter",
                    "count": len(tier_b),
                    "high_epss": sum(1 for r in tier_b if (r["epss"] or 0) >= 0.9),
                    "entries": tier_b,
                },
            })
            log.info("KEV %s: tier A %d, tier B %d, %d scored",
                     cat.get("catalogVersion"), len(tier_a), len(tier_b), len(scores))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            KEV_STATE.fail("%s: %s" % (type(e).__name__, e))
            log.warning("KEV poll failed: %s", e)
        await asyncio.sleep(KEV_POLL_SECONDS)


# DOE writes each order's paragraph in a stable shape, and the phrases below
# are read off the register rather than guessed at. An order keeping a unit
# that would otherwise close says "remain available to operate"; one
# authorising extra dispatch during an event says "dispatch specified units".
# The matched phrase is returned alongside the label, because a
# classification with no evidence behind it is not worth having.
_ORDER_ID = re.compile(r"No\.\s*(202-2\d-\d+[A-Z]?)")
_ORDER_LEAD = re.compile(
    r"On (\w+ \d+, \d{4}) , the U\.S\. Department of Energy \(DOE\) (\w+)")
_ORDER_TO = re.compile(r"\bto\s+(?:the\s+)?([A-Z][^,.]{3,70}?)(?:\s*\(|,|\.)")
_ORDER_THRU = re.compile(
    r"(?:effective|in effect)[^.]{0,140}?through\s+([A-Z]\w+ \d+, \d{4})", re.I)
_ORDER_CHARACTER = [
    ("retirement deferral",
     r"remains? available to operate|shall not (?:be )?(?:retire|deactivat)"
     r"|defer(?:ral|ring)? of (?:the )?(?:planned )?(?:retirement|deactivation)"),
    ("retirement deferral", r"to run Unit \d+ at the [A-Z][\w ]+Generating Station"),
    ("event dispatch",
     r"dispatch specified (?:generation )?units|order their operation as needed"),
    ("capacity assurance", r"operate specified generation units to meet anticipated"),
]


def _strip_html(raw: str) -> str:
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    for a, b in (("&amp;", "&"), ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"'),
                 ("&rsquo;", "'"), ("&ldquo;", '"'), ("&rdquo;", '"'), ("&mdash;", "-")):
        txt = txt.replace(a, b)
    return re.sub(r"\s+", " ", txt)


def parse_doe_register(raw: str) -> dict[str, Any]:
    txt = _strip_html(raw)
    ids_on_page = sorted(set(re.findall(r"202-2\d-\d+[A-Z]?", txt)))
    orders: dict[str, Any] = {}
    # Split on the recurring lead-in so each order's own paragraph is the only
    # text its fields are read from. Without that split the effective-window
    # sentence, which TRAILS each entry on this page, gets attributed to the
    # next order down.
    for part in re.split(
            r"(?=On \w+ \d+, \d{4} , the U\.S\. Department of Energy)", txt):
        lead = _ORDER_LEAD.match(part)
        oid = _ORDER_ID.search(part) if lead else None
        if not (lead and oid):
            continue
        body = part[:1400]
        character, evidence = "unclassified", None
        for label, pat in _ORDER_CHARACTER:
            m = re.search(pat, body, re.I)
            if m:
                character, evidence = label, m.group(0)[:120]
                break
        to = _ORDER_TO.search(part)
        thru = _ORDER_THRU.search(part)
        orders[oid.group(1)] = {
            "id": oid.group(1),
            "date": lead.group(1),
            "action": lead.group(2),
            "recipient": to.group(1).strip() if to else None,
            "through": thru.group(1) if thru else None,
            "character": character,
            "character_evidence": evidence,
            "excerpt": body[:420].strip(),
        }
    return {
        "ids_on_page": len(ids_on_page),
        "parsed": len(orders),
        "coverage_note": ("%d of %d order IDs on the page parsed into complete "
                          "records; the rest are present but their lead-in did not "
                          "match, and are counted rather than detailed."
                          % (len(orders), len(ids_on_page))),
        "orders": sorted(orders.values(), key=lambda o: o["id"]),
    }


async def doe_poll_loop(session: ClientSession) -> None:
    if not GRIDWATCH_ENABLED:
        return
    while True:
        try:
            DOE_STATE.set(parse_doe_register(await _get(session, DOE_URL)))
            log.info("DOE register: %d orders parsed", DOE_STATE.payload["parsed"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            DOE_STATE.fail("%s: %s" % (type(e).__name__, e))
            log.warning("DOE poll failed: %s", e)
        await asyncio.sleep(DOE_POLL_SECONDS)


# NERC's alert and assessment pages build their lists in the browser, so
# scraping the HTML yields a LinkedIn link and a footer address. The site's
# own React bundle calls /api/search, which is a real JSON endpoint returning
# titles, modified dates, file types and URLs — and the documents live under
# predictable /globalassets/ paths. Query that, filter by path, sort by date.
# (An earlier version of this relay reported "page could not be read" here.
# That was honest but premature: the data was reachable, one layer down.)
NERC_SEARCH = "https://www.nerc.com/api/search"
NERC_QUERIES = {
    "alerts": (["NERC Alert", "Alert Level 1", "Alert Level 2", "Alert Level 3"],
               "/programs/bpsa/alerts/"),
    "assessments": (["Long-Term Reliability Assessment", "Summer Reliability Assessment",
                     "Winter Reliability Assessment", "Reliability Assessment"],
                    ("/programs/rapa/ra/", "/our-work/assessments/")),
}
NERC_PAGES = _env_int("NERC_SEARCH_PAGES", 3)

_NERC_DATE_FMTS = ("%B %d, %Y", "%b %d, %Y")


def _nerc_date(s: str | None):
    for f in _NERC_DATE_FMTS:
        try:
            return datetime.strptime((s or "").strip(), f).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def _nerc_collect(session: ClientSession, queries, prefixes) -> list[dict[str, Any]]:
    if isinstance(prefixes, str):
        prefixes = (prefixes,)
    found: dict[str, dict[str, Any]] = {}
    for q in queries:
        for pg in range(1, NERC_PAGES + 1):
            url = "%s?q=%s&pg=%d" % (NERC_SEARCH, urllib_quote(q), pg)
            try:
                data = await _get(session, url, as_json=True)
            except Exception as e:
                log.warning("NERC search %r page %d failed: %s", q, pg, e)
                break
            items = data.get("items") or []
            if not items:
                break
            for it in items:
                r = it.get("documentResult") or it.get("pageResult") or {}
                u = r.get("url") or ""
                if not any(p in u for p in prefixes):
                    continue
                dt = _nerc_date(r.get("modifiedDate"))
                found[u] = {
                    "title": r.get("title"),
                    "date": r.get("modifiedDate"),
                    "iso": dt.date().isoformat() if dt else None,
                    "_sort": dt.timestamp() if dt else 0,
                    "type": r.get("extension"),
                    "href": ("https://www.nerc.com" + u) if u.startswith("/") else u,
                }
            await asyncio.sleep(0.4)   # a search API is not a firehose
    rows = sorted(found.values(), key=lambda r: r["_sort"], reverse=True)
    for r in rows:
        r.pop("_sort", None)
    return rows


async def nerc_poll_loop(session: ClientSession) -> None:
    if not GRIDWATCH_ENABLED:
        return
    while True:
        out: dict[str, Any] = {}
        try:
            for key, (queries, prefixes) in NERC_QUERIES.items():
                rows = await _nerc_collect(session, queries, prefixes)
                out[key] = {
                    "source": NERC_SEARCH,
                    "usable": bool(rows),
                    "note": None if rows else (
                        "NERC's search API answered but returned nothing under the expected "
                        "document paths. That means the query or the paths have moved, NOT that "
                        "there are no current %s." % key),
                    "count": len(rows),
                    "documents": rows[:40],
                }
            NERC_STATE.set(out)
            log.info("NERC: %d alerts, %d assessments",
                     out["alerts"]["count"], out["assessments"]["count"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            NERC_STATE.fail("%s: %s" % (type(e).__name__, e))
            log.warning("NERC poll failed: %s", e)
        await asyncio.sleep(NERC_POLL_SECONDS)


# ---- briefing digests ----------------------------------------------------
# The structured block a daily grid brief emits. Kept so tomorrow's run can
# label each item NEW, ONGOING or NOT-SEEN against today's — a daily brief
# with no memory of yesterday restates everything at equal weight forever.

DIGEST_LOCK = threading.Lock()


def digest_append(obj: dict[str, Any]) -> str:
    received = iso(time.time())
    obj = dict(obj)
    obj["_received"] = received
    with DIGEST_LOCK:
        DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DIGEST_PATH.open("a") as f:
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        # Trim in place rather than growing without bound.
        try:
            lines = DIGEST_PATH.read_text().splitlines()
            if len(lines) > DIGEST_MAX:
                DIGEST_PATH.write_text("\n".join(lines[-DIGEST_MAX:]) + "\n")
        except OSError as e:
            log.warning("digest trim failed: %s", e)
    return received


def digest_read(limit: int) -> list[dict[str, Any]]:
    try:
        lines = DIGEST_PATH.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


async def handle_kev(request: web.Request) -> web.Response:
    env = KEV_STATE.envelope()
    if KEV_STATE.payload is None:
        return web.json_response(
            dict(env, error="KEV has not been fetched successfully yet"), status=503)
    return web.json_response(dict(env, **KEV_STATE.payload))


async def handle_doe(request: web.Request) -> web.Response:
    env = DOE_STATE.envelope()
    if DOE_STATE.payload is None:
        return web.json_response(
            dict(env, error="the DOE register has not been fetched successfully yet"),
            status=503)
    return web.json_response(dict(env, **DOE_STATE.payload))


async def handle_nerc(request: web.Request) -> web.Response:
    env = NERC_STATE.envelope()
    if NERC_STATE.payload is None:
        return web.json_response(
            dict(env, error="NERC has not been fetched successfully yet"), status=503)
    return web.json_response(dict(env, **NERC_STATE.payload))


# ---- ISO/RTO operational alerts -------------------------------------------
# Grid alerts are a different thing from outage counts. An outage map says
# what already broke; these say the system is tight, and they are the part a
# research pass reliably misses — on 2026-09-09 CAISO had Restricted
# Maintenance Operations in force for Southern California and a full web
# search for "CAISO alerts" surfaced none of it. It was sitting in a
# machine-readable OASIS report the whole time.
#
# Every ISO names its own states differently, so each feed is normalised onto
# one five-step scale. The normalisation is the useful part and also the
# dangerous part: collapsing "defer discretionary maintenance" and "shed firm
# load" onto one number is exactly the kind of flattening that makes a
# dashboard lie. So every tier here is reported WITH the operator's own
# wording and the phrase that produced the tier. A tier with no cause behind
# it is not displayed.
#
# Coverage is honest about itself. Five of eight ISOs have no public alert
# feed reachable without credentials, and those are reported as unavailable
# with the reason, never as normal.

ISO_TIERS = {
    0: "normal",
    1: "advisory",            # notice, watch, restricted maintenance
    2: "conservation",        # voluntary load reduction asked of the public
    3: "emergency",           # EEA 1-2, reserves short, emergency generation
    4: "firm_load_shed",      # EEA 3, rotating outages imminent or underway
}

# Ordered most severe first: the first pattern that matches decides, so a
# message naming both EEA 3 and a conservation appeal is graded on the EEA 3.
_ISO_PATTERNS = [
    (4, "firm load shed", re.compile(
        r"\bEEA\s*(?:Level\s*)?3\b|Energy Emergency Alert\s*(?:Level\s*)?3"
        r"|rotating (?:outage|blackout)|firm load (?:shed|curtail)"
        r"|Stage\s*3\s*Emergency", re.I)),
    (3, "emergency", re.compile(
        r"\bEEA\s*(?:Level\s*)?[12]\b|Energy Emergency Alert\s*(?:Level\s*)?[12]"
        r"|Stage\s*2\s*Emergency|Maximum Generation (?:Emergency|Action)"
        r"|Capacity Emergency|Transmission Emergency"
        r"|Load Management (?:Action|Alert)|Emergency Load Reduction", re.I)),
    (2, "conservation", re.compile(
        r"Flex Alert|Conservation (?:Appeal|Request|Call)"
        r"|(?:asked|asking|urged|urging) .{0,40}conserve"
        r"|voluntary (?:load |energy )?(?:reduction|conservation)", re.I)),
    (1, "advisory", re.compile(
        r"Restricted Maintenance Operations|\bRMO\b"
        r"|Conservative Operations|Maximum Generation (?:Alert|Warning|Watch)"
        r"|Hot Weather (?:Alert|Advisory)|Cold Weather (?:Alert|Advisory|Warning)"
        r"|Capacity (?:Advisory|Alert|Watch)|Voltage Reduction"
        r"|Power Warning|Grid Warning|Grid Watch|Alert Notification"
        r"|Reserve (?:Advisory|Shortage Warning)|Stage\s*1", re.I)),
]


def iso_grade(text: str) -> tuple[int, str, str]:
    """(tier, label, the phrase that decided it). Tier 0 means nothing in the
    text matched — which is reported as 'no declaration found in this
    message', never as 'the grid is fine'."""
    for tier, label, pat in _ISO_PATTERNS:
        m = pat.search(text or "")
        if m:
            return tier, label, m.group(0)
    return 0, "normal", ""


ISO_STATE = FeedState("isoalerts")

ISOALERTS_ENABLED = _env_bool("ISOALERTS_ENABLED", True)
ISOALERTS_POLL_SECONDS = max(300, int(_env_float("ISOALERTS_POLL_MINUTES", 10.0) * 60))
PJM_API_KEY = os.environ.get("PJM_API_KEY", "").strip()
PJM_API_KEY_HEADER = os.environ.get(
    "PJM_API_KEY_HEADER", "Ocp-Apim-Subscription-Key").strip()
ISONE_API_USER = os.environ.get("ISONE_API_USER", "").strip()
ISONE_API_PASSWORD = os.environ.get("ISONE_API_PASSWORD", "")

CAISO_OASIS = "http://oasis.caiso.com/oasisapi/SingleZip"
ERCOT_PRC = "https://www.ercot.com/api/1/services/read/dashboards/daily-prc.json"
ERCOT_SUPPLY = "https://www.ercot.com/api/1/services/read/dashboards/supply-demand.json"
NYISO_OPERMSG = "http://mis.nyiso.com/public/htm/OperMessages/CurrentOperMessages.htm"
PJM_API_BASE = "https://api.pjm.com/api/v1"
ISONE_API_BASE = "https://webservices.iso-ne.com/api/v1.1"


async def _iso_get_bytes(session: ClientSession, url: str,
                         headers: dict | None = None) -> bytes:
    h = {"User-Agent": GRIDWATCH_USER_AGENT,
         "Accept": "application/json,text/csv,application/xml;q=0.9,*/*;q=0.8"}
    h.update(headers or {})
    async with session.get(url, timeout=ClientTimeout(total=90), headers=h) as r:
        r.raise_for_status()
        return await r.read()


# -- CAISO -----------------------------------------------------------------
# ATL_OSM is the operator system message log. It carries real declarations
# mixed in with transmission-limit housekeeping and maintenance notices, so
# ungraded messages are counted but not shown: a dashboard that lists "WebOMS
# maintenance window" beside "Restricted Maintenance Operations" has taught
# the reader to ignore it.

_CAISO_EFFECTIVE = re.compile(
    r"effective\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})"
    r"(?:\s*(?:-|through|to)\s*(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}))?", re.I)


# CAISO stamps these in Pacific Prevailing Time. Convert with the zone rather
# than a fixed offset — a hardcoded -7 is correct today and wrong in November,
# which is exactly when a winter declaration would matter.
def _caiso_epoch(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        naive = datetime.strptime(stamp, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    try:
        from zoneinfo import ZoneInfo
        return naive.replace(tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    except Exception:
        return naive.replace(tzinfo=timezone.utc).timestamp() + 7 * 3600


def _caiso_when(text: str) -> tuple[str | None, str | None]:
    m = _CAISO_EFFECTIVE.search(text or "")
    if not m:
        return None, None
    def iso_(d, t):
        if not d:
            return None
        mm, dd, yy = d.split("/")
        return "%s-%s-%sT%s" % (yy, mm, dd, t)
    return iso_(m.group(1), m.group(2)), iso_(m.group(3), m.group(4))


async def iso_caiso(session: ClientSession) -> dict[str, Any]:
    now = time.time()
    start = time.strftime("%Y%m%dT00:00-0000", time.gmtime(now - 3 * 86400))
    end = time.strftime("%Y%m%dT00:00-0000", time.gmtime(now + 86400))
    url = ("%s?queryname=ATL_OSM&startdatetime=%s&enddatetime=%s&version=1"
           % (CAISO_OASIS, start, end))
    raw = await _iso_get_bytes(session, url)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = z.namelist()[0]
        if "INVALID" in name.upper():
            raise RuntimeError("OASIS rejected the ATL_OSM request (%s)" % name)
        body = z.read(name).decode("utf-8", "replace")

    alerts, ungraded = [], 0
    for blob in re.findall(r"<ATLS_DATA>(.*?)</ATLS_DATA>", body, re.S):
        def field(tag):
            m = re.search(r"<(?:\w+:)?%s>(.*?)</(?:\w+:)?%s>" % (tag, tag), blob, re.S)
            return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        text = field("MSG_TEXT") or field("MSG_SUBJECT")
        when = field("MSG_TIME")
        tier, label, phrase = iso_grade(text)
        if tier == 0:
            ungraded += 1
            continue
        eff_from, eff_to = _caiso_when(text)
        # A declaration that starts tomorrow is NOT in force. Testing only the
        # end bound reported next-day RMOs as active, which is the same class
        # of quiet lie as showing a failed fetch as a calm grid.
        status, in_force = "unknown", None
        b0, b1 = _caiso_epoch(eff_from), _caiso_epoch(eff_to)
        now_ = time.time()
        if b0 is not None or b1 is not None:
            if b0 is not None and now_ < b0:
                status, in_force = "scheduled", False
            elif b1 is not None and now_ > b1:
                status, in_force = "ended", False
            else:
                status, in_force = "in_force", True
        alerts.append({
            "id": "caiso-%s-%s" % (re.sub(r"[^a-z0-9]+", "-", label)[:24],
                                   (eff_from or when or "")[:10]),
            "tier": tier, "tier_label": label, "matched_phrase": phrase,
            "issued": when or None,
            "effective_from": eff_from, "effective_to": eff_to,
            "in_force": in_force, "status": status,
            "text": text[:600],
            "cite": "http://oasis.caiso.com/oasisapi/SingleZip?queryname=ATL_OSM",
        })
    return {"iso": "CISO", "name": "CAISO", "ok": True, "source": "OASIS ATL_OSM",
            "alerts": alerts, "messages_ungraded": ungraded,
            "as_of": iso(time.time())}


# -- ERCOT -----------------------------------------------------------------

async def iso_ercot(session: ClientSession) -> dict[str, Any]:
    d = json.loads(await _iso_get_bytes(session, ERCOT_PRC))
    cc = d.get("current_condition") or {}
    if "state" not in cc and "eea_level" not in cc:
        raise RuntimeError("daily-prc.json returned an unexpected shape "
                           "(no state or eea_level) — treat as unknown, not normal")
    eea = cc.get("eea_level")
    note = str(cc.get("condition_note") or "")
    state = str(cc.get("state") or "")
    tier, label, phrase = iso_grade(state + " " + note + " " + (cc.get("title") or ""))
    if isinstance(eea, int) and eea > 0:
        tier = 4 if eea >= 3 else 3
        label = ISO_TIERS[tier]
        phrase = "eea_level=%d, reported by ERCOT" % eea
    alerts = []
    if tier > 0:
        alerts.append({
            "id": "ercot-%s-%s" % (label, time.strftime("%Y-%m-%d", time.gmtime())),
            "tier": tier, "tier_label": label, "matched_phrase": phrase,
            "issued": cc.get("lastUpdated"), "effective_from": None,
            "effective_to": None, "in_force": True,
            "text": (cc.get("title") or "") + " — " + note,
            "cite": ERCOT_PRC,
        })
    out = {"iso": "ERCO", "name": "ERCOT", "ok": True,
           "source": "daily-prc.json", "alerts": alerts,
           "operator_state": state, "eea_level": eea,
           "note": note, "prc_mw": cc.get("prc_value"),
           "as_of": cc.get("lastUpdated") or iso(time.time())}
    # Headroom is a stress signal, not an alert, and is labelled as such.
    try:
        sd = json.loads(await _iso_get_bytes(session, ERCOT_SUPPLY))
        rows = [r for r in (sd.get("data") or []) if r.get("demand")]
        if rows:
            last = rows[-1]
            out["capacity_mw"] = last.get("capacity")
            out["demand_mw"] = last.get("demand")
            out["headroom_mw"] = (last.get("capacity") or 0) - (last.get("demand") or 0)
            out["headroom_as_of"] = last.get("timestamp")
    except Exception as e:
        out["headroom_error"] = "%s: %s" % (type(e).__name__, e)
    return out


# -- NYISO -----------------------------------------------------------------
# The dispatcher notice board, found because a briefing run stumbled onto it
# after this relay had already written NYISO off as having no alert feed.
# It is plain HTML under 3 KB, sectioned by notice type, and it carries the
# operational ones: min oil burn declarations, scheduling suspensions,
# dispatcher notices. Market pricing confirmations dominate by volume and are
# counted rather than shown — a board where four of five entries say "prices
# are correct" trains the reader to skip it.

# The board is a plain <h3> heading per section followed by a two-column
# table, one row per notice. Parse that structure rather than flattening the
# page to text: a message like "disabled beginning 09-Sep 14:00 until
# 09-Sep 15:00" contains timestamps of its own, and a text-level split on
# timestamps tore that single notice into three fragments, two of them empty.
_NY_H3 = re.compile(r"<h3[^>]*>\s*(.*?)\s*</h3>", re.I | re.S)
_NY_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_NY_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.I | re.S)
# Operational content NYISO publishes that the generic tier patterns miss.
_NY_EXTRA = re.compile(
    r"Min(?:imum)? Oil Burn|scheduling disabled|Thunderstorm Alert|"
    r"Major Emergency|Energy Warning|Supplemental Resource|"
    r"Gas (?:Curtailment|Restriction)|Transmission (?:Emergency|Constraint)", re.I)


def _ny_cell(raw: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


async def iso_nyiso(session: ClientSession) -> dict[str, Any]:
    doc = (await _iso_get_bytes(session, NYISO_OPERMSG)).decode("utf-8", "replace")
    heads = list(_NY_H3.finditer(doc))
    alerts, other = [], 0
    for k, h in enumerate(heads):
        name = _ny_cell(h.group(1))
        body = doc[h.end():heads[k + 1].start() if k + 1 < len(heads) else len(doc)]
        for tr in _NY_TR.finditer(body):
            tds = [_ny_cell(x) for x in _NY_TD.findall(tr.group(1))]
            if len(tds) < 2 or tds[0].lower() == "insert time":
                continue        # the header row
            when, msg = tds[0], tds[1].strip(" .")
            if not msg:
                continue
            tier, label, phrase = iso_grade(msg)
            if tier == 0:
                x = _NY_EXTRA.search(msg)
                if x:
                    tier, label, phrase = 1, "advisory", x.group(0)
            if tier == 0:
                other += 1      # pricing confirmations, mostly
                continue
            alerts.append({
                "id": "nyiso-%s-%s" % (re.sub(r"[^a-z0-9]+", "-", label.lower())[:24],
                                       when[:11]),
                "tier": tier, "tier_label": label, "matched_phrase": phrase,
                "issued": when, "effective_from": None, "effective_to": None,
                "in_force": None, "status": "unknown",
                "section": name, "text": msg[:600], "cite": NYISO_OPERMSG,
            })
    return {"iso": "NYIS", "name": "NYISO", "ok": True,
            "source": "dispatcher notice board", "alerts": alerts,
            "messages_ungraded": other, "as_of": iso(time.time())}


# -- PJM -------------------------------------------------------------------
# Keyless PJM does not work. insidelines.pjm.com serves a captcha
# interstitial to any scripted client, rss.pjm.com fails certificate
# verification outright, and the emergency-procedures page 404s. So this is
# key-gated, and says so rather than reporting an empty PJM as a calm PJM.

async def iso_pjm(session: ClientSession) -> dict[str, Any]:
    if not PJM_API_KEY:
        raise PermissionError(
            "PJM_API_KEY is not set. A free non-member key comes from "
            "accountmanager.pjm.com, then apiportal.pjm.com; see relay.env. "
            "No keyless PJM alert feed exists.")
    url = "%s/gen_by_fuel?rowCount=1&format=json" % PJM_API_BASE
    hdr = {PJM_API_KEY_HEADER: PJM_API_KEY}
    # A cheap call first, purely to establish that the key and header name
    # are right — the guide does not document the header, so a wrong guess
    # would otherwise surface as "PJM has no alerts".
    await _iso_get_bytes(session, url, hdr)
    raw = await _iso_get_bytes(
        session, "%s/emergency_procedures?rowCount=50&format=json" % PJM_API_BASE, hdr)
    d = json.loads(raw)
    items = d.get("items") if isinstance(d, dict) else d
    alerts = []
    for it in (items or []):
        text = " ".join(str(v) for v in it.values() if isinstance(v, str))
        tier, label, phrase = iso_grade(text)
        if tier == 0:
            continue
        alerts.append({
            "id": "pjm-%s-%s" % (re.sub(r"[^a-z0-9]+", "-", label)[:24],
                                 str(it.get("effective_date") or "")[:10]),
            "tier": tier, "tier_label": label, "matched_phrase": phrase,
            "issued": it.get("publish_date") or it.get("effective_date"),
            "effective_from": it.get("effective_date"),
            "effective_to": it.get("termination_date"),
            "in_force": None if not it.get("termination_date") else None,
            "text": text[:600],
            "cite": "%s/emergency_procedures" % PJM_API_BASE,
        })
    return {"iso": "PJM", "name": "PJM", "ok": True,
            "source": "Data Miner 2 emergency_procedures",
            "alerts": alerts, "as_of": iso(time.time())}


async def iso_isone(session: ClientSession) -> dict[str, Any]:
    if not ISONE_API_USER:
        raise PermissionError(
            "ISONE_API_USER / ISONE_API_PASSWORD are not set. ISO-NE web "
            "services need a free registered account; no keyless feed exists.")
    import base64
    tok = base64.b64encode(
        ("%s:%s" % (ISONE_API_USER, ISONE_API_PASSWORD)).encode()).decode()
    raw = await _iso_get_bytes(session, "%s/current/system/load.json" % ISONE_API_BASE,
                               {"Authorization": "Basic " + tok})
    json.loads(raw)
    return {"iso": "ISNE", "name": "ISO-NE", "ok": True,
            "source": "ISO-NE web services", "alerts": [],
            "note": "authenticated and reachable; no alert endpoint wired yet",
            "as_of": iso(time.time())}


# ISOs with no public alert path found. Named individually with the reason,
# because "not listed" and "listed as quiet" are different claims and only
# one of them is true.
ISO_NO_FEED = [
    {"iso": "MISO", "name": "MISO",
     "reason": "the MISO RTWD DataBroker answers but returns {\"error\": \"no data\"} "
               "for every messageType tried, and misoenergy.org 403s, so the current "
               "API index cannot be read"},
    {"iso": "SWPP", "name": "SPP",
     "reason": "SPP's grid-conditions page is a Tableau embed with no JSON behind it"},
    {"iso": "SOCO", "name": "Southern Company / SERC",
     "reason": "not an ISO with public operational postings"},
]

ISO_SOURCES = [("CISO", iso_caiso), ("ERCO", iso_ercot), ("NYIS", iso_nyiso),
               ("PJM", iso_pjm), ("ISNE", iso_isone)]


async def isoalerts_poll_loop(session: ClientSession) -> None:
    if not GRIDWATCH_ENABLED or not ISOALERTS_ENABLED:
        return
    while True:
        results, unavailable = [], list(ISO_NO_FEED)
        for code, fn in ISO_SOURCES:
            try:
                results.append(await fn(session))
            except PermissionError as e:
                unavailable.append({"iso": code, "name": code,
                                    "reason": str(e), "needs_credentials": True})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                unavailable.append({"iso": code, "name": code,
                                    "reason": "%s: %s" % (type(e).__name__, e),
                                    "needs_credentials": False})
        live = [a for r in results for a in r.get("alerts", [])]
        worst = max([a["tier"] for a in live], default=0)
        ISO_STATE.set({
            "isos": results,
            "unavailable": unavailable,
            "alerts": sorted(live, key=lambda a: (-a["tier"], a.get("issued") or "")),
            "worst_tier": worst,
            "worst_tier_label": ISO_TIERS[worst],
            "covered": len(results),
            "not_covered": len(unavailable),
            "tiers": ISO_TIERS,
            "reading_note": (
                "worst_tier is the highest tier across the ISOs that answered. "
                "It says nothing about the %d that did not — an ISO with no "
                "reachable feed is absent from this number, not counted as calm."
                % len(unavailable)),
        })
        if live:
            log.info("iso alerts: %d live, worst tier %d (%s); %d ISO(s) unavailable",
                     len(live), worst, ISO_TIERS[worst], len(unavailable))
        else:
            log.info("iso alerts: none live across %d ISO(s); %d unavailable",
                     len(results), len(unavailable))
        await asyncio.sleep(ISOALERTS_POLL_SECONDS)


async def handle_isoalerts(request: web.Request) -> web.Response:
    env = ISO_STATE.envelope()
    if ISO_STATE.payload is None:
        return web.json_response(
            dict(env, error="ISO alerts have not been fetched successfully yet"),
            status=503)
    return web.json_response(dict(env, **ISO_STATE.payload))


# ---- reading the daily brief out of a mailbox -----------------------------
# The brief is written somewhere else and arrives as email. Opening a port to
# whatever writes it needs a port that is free and a certificate that is
# valid; a mailbox needs neither, and the mailbox already exists.
#
# Three things about the real mail shaped this, all of them checked against
# actual bytes rather than assumed:
#
#   1. It is HTML-only. There is no text/plain part, so a reader that takes
#      the plain part alone gets an empty string and reports success.
#   2. Gmail rewrites every link to https://www.google.com/url?q=<real>.
#      Left alone, 120 citations across 72 hosts collapse to one host.
#   3. A forwarded copy arrives quoted, so '>' prefixes have to come off
#      before anything looks for a JSON block.
#
# The trust model is the one the transport forces. Anyone can send mail to
# any address, so the sender proves nothing: the control is a shared secret
# carried INSIDE the structured block, and a block without it is refused.
# The From allowlist is there to stop accidents, not attacks, and the code
# says so rather than implying otherwise.

_GOOGLE_WRAP = re.compile(
    r"https?://(?:www\.)?google\.[a-z.]+/url\?[^\s\"'<>)\]]+", re.I)
_MAIL_BLOCK_END = re.compile(
    r"(?i)</(?:p|div|li|tr|h[1-6]|table|section|blockquote|pre)\s*>")
_MAIL_DROP = re.compile(r"(?is)<(script|style|head)[^>]*>.*?</\1\s*>")
_MAIL_QUOTE = re.compile(r"^(?:>\s?)+", re.M)
# Markers, loosely. The equals-sign fencing is what the prompt asks for, but
# a brief in the wild turned up emitting "BEGIN STRUCTURED BLOCK" with no
# fencing at all, and refusing to read it would have been pedantry — the
# intent is unmistakable. Anchored to whole lines so the phrase appearing in
# prose cannot open a block.
_MAIL_BEGIN = re.compile(r"^[=\-\s]*BEGIN\s+STRUCTURED(?:\s+BLOCK)?[=\-\s]*$", re.I | re.M)
_MAIL_END = re.compile(r"^[=\-\s]*END\s+STRUCTURED(?:\s+BLOCK)?[=\-\s]*$", re.I | re.M)
_MAIL_KV = re.compile(r"^([A-Za-z][\w .\-]{0,60}?)\s*:\s*(.*)$")
_MAIL_SMART = {"“": '"', "”": '"', "‘": "'", "’": "'",
               "–": "-", "—": "-"}

_MAIL_ID_PATTERNS = [
    ("cve", re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I)),
    ("icsa", re.compile(r"\bICSA?-\d{2}-\d{3}-\d{2}[A-Z]?\b", re.I)),
    ("icsma", re.compile(r"\bICSMA-\d{2}-\d{3}-\d{2}\b", re.I)),
    ("doe_202c", re.compile(r"\b202-\d{2}-\d{1,3}[A-Z]?\b")),
    ("cisa_aa", re.compile(r"\bAA\d{2}-\d{3}[A-Z]\b")),
    ("ferc_docket", re.compile(r"\b(?:E[LR]|R[MP])\d{2}-\d{1,4}(?:-\d{3})?\b")),
]
_MAIL_HEADING = re.compile(r"^(\d{1,2})\.\s+([A-Z][A-Z0-9 &/,'–\-]{4,70})\s*$", re.M)
_MAIL_DATE = re.compile(r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})")
_MAIL_MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}


def mail_unwrap_links(text: str) -> str:
    def _sub(m):
        q = parse_qs(urlparse(m.group(0)).query)
        for key in ("q", "url"):
            if q.get(key):
                return unquote(q[key][0])
        return m.group(0)
    return _GOOGLE_WRAP.sub(_sub, text)


def mail_html_to_text(src: str) -> str:
    s = _MAIL_DROP.sub(" ", src)
    s = re.sub(r"(?i)<wbr\s*/?>", "", s)          # Gmail splits long tokens
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = _MAIL_BLOCK_END.sub("\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    for ch in ("​", "﻿", "­"):
        s = s.replace(ch, "")
    s = s.replace(" ", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def mail_unquote_forward(text: str) -> str:
    """A forwarded copy arrives with '> ' on every line."""
    if len(_MAIL_QUOTE.findall(text)) < 5:
        return text
    return _MAIL_QUOTE.sub("", text)


def mail_body_text(msg) -> tuple[str, str]:
    plain, htm = None, None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if (part.get_content_disposition() or "").lower() == "attachment":
            continue
        ct = part.get_content_type()
        try:
            if ct == "text/plain" and plain is None:
                plain = part.get_content()
            elif ct == "text/html" and htm is None:
                htm = part.get_content()
        except Exception:      # a part with a charset Python cannot decode
            continue
    if plain and plain.strip():
        return mail_unquote_forward(plain), "text/plain"
    if htm:
        return mail_unquote_forward(mail_html_to_text(htm)), "text/html"
    return "", "none"


def _mail_unwrap_json(raw: str) -> str:
    """Repair JSON that a mail client hard-wrapped inside a string value.

    Transports fold long lines at around 78 columns whatever the sender
    intended, and a newline inside a JSON string is an invalid control
    character, so the whole block fails to parse over a formatting artifact
    nobody chose. This walks the text tracking string state — honouring
    backslash escapes so an escaped quote does not look like the end of a
    string — and turns raw newlines and tabs INSIDE strings into single
    spaces. Structure outside strings is untouched, so a genuinely malformed
    block still fails rather than being silently coerced into something.
    """
    out, in_str, esc = [], False, False
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if esc:
            out.append(c)
            esc = False
        elif c == "\\" and in_str:
            out.append(c)
            esc = True
        elif c == '"':
            in_str = not in_str
            out.append(c)
        elif in_str and c in "\r\n\t":
            # Collapse the break and any indentation that followed it.
            j = i
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if out and out[-1] != " ":
                out.append(" ")
            i = j
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def mail_find_structured(text: str):
    """Return (parsed, error). error is None only on success."""
    b = _MAIL_BEGIN.search(text)
    if not b:
        return None, "no BEGIN STRUCTURED marker in the message body"
    e = _MAIL_END.search(text, b.end())
    raw = (text[b.end():e.start()] if e else text[b.end():]).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    if not raw:
        return None, "the structured block is empty"
    # Repairs are tried in order, least invasive first, and only ever after
    # the previous attempt has failed. Applying any of them unconditionally
    # would rewrite legitimate content with nothing to show it happened.
    def _smart(t):
        for k, v in _MAIL_SMART.items():
            t = t.replace(k, v)
        return t

    repair = None
    try:
        obj = json.loads(raw)
    except ValueError as first:
        obj = None
        for name, fn in (("rewrapped hard-wrapped string values", _mail_unwrap_json),
                         ("straightened curled quotes", _smart),
                         ("both", lambda t: _smart(_mail_unwrap_json(t)))):
            try:
                obj = json.loads(fn(raw))
                repair = name
                break
            except ValueError:
                continue
        if obj is None:
            # Not JSON. A minimal "key: value" block still carries the token
            # and the date, which is worth having — but it carries no items
            # and no load_claims, so it is reported as the degraded thing it
            # is rather than being dressed up as a structured run.
            kv, ok = {}, True
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = _MAIL_KV.match(line)
                if not m:
                    ok = False
                    break
                kv[m.group(1).strip().lower().replace(" ", "_")] = m.group(2).strip()
            if ok and kv:
                kv["_format"] = "key-value"
                return kv, None
            return None, "structured block is not valid JSON: %s" % first
    if repair and isinstance(obj, dict):
        # Collapsing a break inside a string leaves the space it stood in for,
        # so a value the generator put on its own line comes back padded. A
        # cite with spaces around it is not a URL any more.
        def _tidy(v):
            if isinstance(v, str):
                return re.sub(r"\s+", " ", v).strip()
            if isinstance(v, list):
                return [_tidy(x) for x in v]
            if isinstance(v, dict):
                return {k: _tidy(x) for k, x in v.items()}
            return v
        obj = _tidy(obj)
        # Say so. A block that only parsed after repair is worth knowing about
        # — it means the sending side is emitting something the transport
        # mangles, and the next change to either could break it outright.
        obj.setdefault("_repaired", repair)
    if not isinstance(obj, dict):
        return None, "structured block is a %s, expected an object" % type(obj).__name__
    if not e:
        obj.setdefault("_warning", "no END STRUCTURED marker; parsed to end of body")
    return obj, None


def mail_brief_date(subject: str, text: str):
    for src in (subject, text[:400]):
        m = _MAIL_DATE.search(src or "")
        if m and m.group(1) in _MAIL_MONTHS:
            return "%04d-%02d-%02d" % (
                int(m.group(3)), _MAIL_MONTHS[m.group(1)], int(m.group(2)))
    return None


def mail_scan_text(text: str) -> dict[str, Any]:
    """What can be read out of the prose without interpreting it.

    Identifiers only. A CVE number either appears in the text or it does
    not, which makes a day-over-day diff possible before the prompt is
    redeployed. Nothing numeric is lifted out of a sentence: a load figure
    scraped from prose would sit in the same field as one the briefing
    asserted, and the two are not the same thing.
    """
    sections = []
    heads = list(_MAIL_HEADING.finditer(text))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        sections.append({"n": int(m.group(1)), "title": m.group(2).strip(),
                         "chars": end - m.end()})
    ids: dict[str, list[str]] = {}
    for kind, pat in _MAIL_ID_PATTERNS:
        seen, out = set(), []
        for hit in pat.findall(text):
            u = hit.upper()
            if u not in seen:
                seen.add(u)
                out.append(u)
        if out:
            ids[kind] = out
    hosts: dict[str, int] = {}
    for u in re.findall(r"https?://[^\s\"'<>)\]]+", text):
        h = (urlparse(u).hostname or "").lower()
        if h.startswith("www."):
            h = h[4:]
        if h:
            hosts[h] = hosts.get(h, 0) + 1
    return {
        "sections": sections,
        "identifiers": ids,
        "identifier_count": sum(len(v) for v in ids.values()),
        "source_hosts": [{"host": h, "n": n} for h, n in
                         sorted(hosts.items(), key=lambda kv: (-kv[1], kv[0]))],
        "chars": len(text),
    }


def mail_parse_briefing(raw: bytes) -> dict[str, Any]:
    """RFC-822 bytes in, a digest record out. `accepted` says whether it
    should be stored, and `reason` always says why not."""
    msg = email.message_from_bytes(raw, policy=_email_policy.default)
    subject = str(msg.get("Subject", "") or "")
    sender = str(msg.get("From", "") or "")
    text, part = mail_body_text(msg)
    text = mail_unwrap_links(text)

    rec: dict[str, Any] = {
        "mail": {
            "subject": subject,
            "from": sender,
            "date": str(msg.get("Date", "") or ""),
            "message_id": str(msg.get("Message-ID", "") or ""),
            "body_part": part,
        },
        "date": mail_brief_date(subject, text),
        "ingested_via": "mailbox",
    }

    if not text.strip():
        rec["accepted"] = False
        rec["reason"] = "no readable body (parts: %s)" % ", ".join(
            p.get_content_type() for p in msg.walk()
            if p.get_content_maintype() != "multipart") or "none"
        return rec

    obj, err = mail_find_structured(text)
    if obj is not None:
        supplied = str(obj.get("token") or obj.get("relay_token") or "")
        if BRIEFMAIL_SECRET:
            if len(supplied) != len(BRIEFMAIL_SECRET) or not all(
                    a == b for a, b in zip(supplied, BRIEFMAIL_SECRET)):
                rec["accepted"] = False
                rec["reason"] = ("the structured block carries no matching token; "
                                 "mail is trivially forged, so an untokened block "
                                 "is not stored")
                return rec
        obj.pop("token", None)
        obj.pop("relay_token", None)
        keyvalue = obj.pop("_format", None) == "key-value"
        for k, v in obj.items():
            if k != "mail":
                rec[k] = v
        rec["token_verified"] = bool(BRIEFMAIL_SECRET)
        if not rec.get("date"):
            rec["date"] = obj.get("date") or obj.get("briefing_date") or obj.get("run_date")
        if keyvalue:
            # The token checked out; the payload did not arrive. Scan the
            # prose so the run still contributes identifiers, and say why
            # the reconciliation panels stay empty.
            rec["structured"] = False
            rec["parser"] = "structured-keyvalue"
            rec["structured_error"] = (
                "the block is a plain key/value list, not the JSON object the "
                "prompt specifies, so it carries no items and no load_claims")
            rec["scan"] = mail_scan_text(text)
            rec["accepted"] = True
        else:
            rec["structured"] = True
            rec["parser"] = "structured-block"
            rec["accepted"] = True
    else:
        # No block yet — store what is exact and label it as a scan, so the
        # dashboard can say "this run predates the structured prompt" rather
        # than showing an empty panel that looks like a quiet day.
        rec["structured"] = False
        rec["parser"] = "text-scan"
        rec["structured_error"] = err
        rec["scan"] = mail_scan_text(text)
        rec["accepted"] = True
        rec["unverified"] = bool(BRIEFMAIL_SECRET)
    if BRIEFMAIL_KEEP_TEXT:
        rec["text"] = text
    return rec


def mail_sender_ok(rec: dict[str, Any]) -> bool:
    if not BRIEFMAIL_FROM_ALLOW:
        return True
    frm = (rec.get("mail", {}).get("from") or "").lower()
    addr = email.utils.parseaddr(frm)[1] or frm
    return any(a in addr or a in frm for a in BRIEFMAIL_FROM_ALLOW)


class BriefmailState:
    """What the mailbox reader last did. A reader that has stopped finding
    mail looks exactly like a quiet week unless it says so itself."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_attempt: str | None = None
        self.last_ok: bool | None = None
        self.last_error: str | None = None
        self.last_message: str | None = None
        self.stored = 0
        self.refused = 0
        self.skipped = 0
        self.recent: list[dict[str, Any]] = []

    def note(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def record(self, verdict: str, rec: dict[str, Any], why: str = "") -> None:
        m = rec.get("mail", {})
        with self.lock:
            if verdict == "stored":
                self.stored += 1
                self.last_message = iso(time.time())
            elif verdict == "refused":
                self.refused += 1
            else:
                self.skipped += 1
            self.recent.insert(0, {
                "at": iso(time.time()),
                "verdict": verdict,
                "why": why,
                "subject": m.get("subject"),
                "from": m.get("from"),
                "date": rec.get("date"),
                "parser": rec.get("parser"),
                "structured": rec.get("structured"),
            })
            del self.recent[12:]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "enabled": BRIEFMAIL_ENABLED,
                "source": BRIEFMAIL_SOURCE_DESC,
                "poll_seconds": BRIEFMAIL_POLL_SECONDS,
                "secret_required": bool(BRIEFMAIL_SECRET),
                "from_allow": BRIEFMAIL_FROM_ALLOW,
                "keep_text": BRIEFMAIL_KEEP_TEXT,
                "last_attempt": self.last_attempt,
                "last_ok": self.last_ok,
                "last_error": self.last_error,
                "last_message_stored": self.last_message,
                "stored": self.stored,
                "refused": self.refused,
                "skipped": self.skipped,
                "recent": list(self.recent),
            }


if BRIEFMAIL_IMAP_HOST:
    BRIEFMAIL_SOURCE_DESC = "imap %s on %s:%d, folder %s" % (
        BRIEFMAIL_IMAP_USER or "<no user set>", BRIEFMAIL_IMAP_HOST,
        BRIEFMAIL_IMAP_PORT, BRIEFMAIL_IMAP_FOLDER)
elif BRIEFMAIL_MAILDIR:
    BRIEFMAIL_SOURCE_DESC = "maildir %s" % BRIEFMAIL_MAILDIR
else:
    BRIEFMAIL_SOURCE_DESC = "not configured"

BRIEFMAIL_STATE = BriefmailState()


def briefmail_status() -> dict[str, Any]:
    return BRIEFMAIL_STATE.snapshot()


# How good a stored record is. A message re-read after the parser improves
# should be able to replace a worse copy of itself — dedupe that only ever
# says "already stored" freezes the first, worst reading in place, and the
# only way out is editing the store by hand.
_DIGEST_RANK = {"structured-block": 3, "structured-keyvalue": 2, "text-scan": 1}


def digest_rank(row: dict[str, Any]) -> int:
    return _DIGEST_RANK.get(row.get("parser") or "", 0)


def briefmail_known_ids() -> dict[str, int]:
    """Message-ID -> quality of the stored record, so a re-read can tell
    'seen this already' from 'seen a worse version of this already'."""
    out: dict[str, int] = {}
    for row in digest_read(DIGEST_MAX):
        mid = (row.get("mail") or {}).get("message_id")
        if mid:
            out[mid] = max(out.get(mid, 0), digest_rank(row))
    return out


def digest_replace(message_id: str, obj: dict[str, Any]) -> bool:
    """Swap the stored record for this Message-ID. Rewrites the file rather
    than appending: two records for one run would make tomorrow's diff
    compare a day against itself."""
    received = iso(time.time())
    obj = dict(obj)
    obj["_received"] = received
    obj["_replaced_earlier_parse"] = True
    with DIGEST_LOCK:
        try:
            lines = DIGEST_PATH.read_text().splitlines()
        except FileNotFoundError:
            return False
        out, hit = [], False
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                out.append(line)
                continue
            if (row.get("mail") or {}).get("message_id") == message_id and not hit:
                out.append(json.dumps(obj, separators=(",", ":")))
                hit = True
            else:
                out.append(line)
        if hit:
            DIGEST_PATH.write_text("\n".join(out) + "\n")
        return hit


def briefmail_consider(raw: bytes, known: dict[str, int]) -> tuple[str, str]:
    """Parse one message and store it if it qualifies. Returns
    (verdict, why) where verdict is stored / refused / skipped."""
    if len(raw) > BRIEFMAIL_MAX_BYTES:
        return "skipped", "message is %d bytes, over BRIEFMAIL_MAX_BYTES" % len(raw)
    try:
        rec = mail_parse_briefing(raw)
    except Exception as e:
        return "skipped", "could not parse: %s: %s" % (type(e).__name__, e)

    subj = rec.get("mail", {}).get("subject") or ""
    if BRIEFMAIL_SUBJECT_MATCH and BRIEFMAIL_SUBJECT_MATCH.lower() not in subj.lower():
        BRIEFMAIL_STATE.record("skipped", rec, "subject does not match")
        return "skipped", "subject does not match"
    if not mail_sender_ok(rec):
        BRIEFMAIL_STATE.record("skipped", rec, "sender not in BRIEFMAIL_FROM_ALLOW")
        return "skipped", "sender not in BRIEFMAIL_FROM_ALLOW"
    mid = rec.get("mail", {}).get("message_id")
    if mid and mid in known:
        new_rank = digest_rank(rec) if rec.get("accepted") else 0
        if new_rank <= known[mid]:
            BRIEFMAIL_STATE.record("skipped", rec, "already stored")
            return "skipped", "already stored"
        # This read is better than the stored one — a parser fix, or a
        # transport artifact that repaired this time. Replace it.
        if digest_replace(mid, rec):
            known[mid] = new_rank
            why = "re-parsed as %s, replacing the stored copy" % rec.get("parser")
            BRIEFMAIL_STATE.record("stored", rec, why)
            log.info("briefmail: upgraded %s (%s)", rec.get("date") or mid, why)
            return "stored", why
    if not rec.get("accepted"):
        why = rec.get("reason") or "refused"
        BRIEFMAIL_STATE.record("refused", rec, why)
        log.warning("briefmail: refused %r — %s", subj[:70], why)
        return "refused", why

    digest_append(rec)
    if mid:
        known[mid] = digest_rank(rec)
    why = ("structured block" if rec.get("structured")
           else "text scan (%d identifiers) — %s" % (
               rec.get("scan", {}).get("identifier_count", 0),
               rec.get("structured_error")))
    BRIEFMAIL_STATE.record("stored", rec, why)
    log.info("briefmail: stored %s (%s)", rec.get("date") or subj[:40], why)
    return "stored", why


# -- maildir ---------------------------------------------------------------
# The simple case, and the better one where it is available: mail delivered
# on this same box, read straight off the disk, no credentials anywhere.

def briefmail_sweep_maildir() -> int:
    base = Path(BRIEFMAIL_MAILDIR)
    new, cur = base / "new", base / "cur"
    if not new.is_dir() and not cur.is_dir():
        # Say which of the three plausible mistakes this is. "not a maildir"
        # on its own sends someone hunting through Dovecot config when the
        # real problem is that they typed an address into a path.
        if "@" in BRIEFMAIL_MAILDIR and "/" not in BRIEFMAIL_MAILDIR:
            raise RuntimeError(
                "BRIEFMAIL_MAILDIR is %r, which looks like an email address. It wants "
                "the filesystem path of the maildir on this box — the directory that "
                "CONTAINS new/, cur/ and tmp/. If the mailbox lives somewhere else, "
                "clear BRIEFMAIL_MAILDIR and set BRIEFMAIL_IMAP_HOST/USER/PASSWORD "
                "instead, with the address as the username." % BRIEFMAIL_MAILDIR)
        if not base.exists():
            raise RuntimeError(
                "BRIEFMAIL_MAILDIR %s does not exist (relative paths resolve against "
                "the service's working directory, so use an absolute path)" % base)
        if base.is_file():
            raise RuntimeError(
                "BRIEFMAIL_MAILDIR %s is a file, not a directory — that is an mbox. "
                "This reader speaks maildir only; switch the delivery agent to maildir "
                "or read the mailbox over IMAP instead." % base)
        try:
            kids = sorted(x.name for x in base.iterdir())[:8]
        except OSError as e:
            raise RuntimeError(
                "BRIEFMAIL_MAILDIR %s cannot be listed (%s). The relay runs as its own "
                "service user, which needs read and write access to the maildir — it "
                "renames messages out of new/ once read." % (base, e))
        raise RuntimeError(
            "BRIEFMAIL_MAILDIR %s has no new/ or cur/, so it is not a maildir. It "
            "contains: %s. Point at the directory one level up if those are mailbox "
            "folders, or at the Maildir inside it." % (base, ", ".join(kids) or "nothing"))
    archive = Path(BRIEFMAIL_ARCHIVE_FOLDER) if BRIEFMAIL_ARCHIVE_FOLDER else None
    if archive:
        archive.mkdir(parents=True, exist_ok=True)
    known = briefmail_known_ids()
    n = 0
    for f in sorted(p for p in new.iterdir() if p.is_file()) if new.is_dir() else []:
        try:
            raw = f.read_bytes()
        except OSError as e:
            log.warning("briefmail: cannot read %s: %s", f.name, e)
            continue
        verdict, _why = briefmail_consider(raw, known)
        n += 1
        # Out of new/ either way: leaving it there means re-reading it every
        # ten minutes and logging the same refusal forever.
        try:
            if archive:
                f.rename(archive / f.name)
            else:
                cur.mkdir(parents=True, exist_ok=True)
                f.rename(cur / (f.name if ":" in f.name else f.name + ":2,S"))
        except OSError as e:
            log.warning("briefmail: read %s but could not move it (%s); "
                        "it will be skipped as a duplicate next pass", f.name, e)
    return n


# -- imap ------------------------------------------------------------------

def briefmail_sweep_imap() -> int:
    cls = imaplib.IMAP4_SSL if BRIEFMAIL_IMAP_SSL else imaplib.IMAP4
    M = cls(BRIEFMAIL_IMAP_HOST, BRIEFMAIL_IMAP_PORT)
    try:
        if not BRIEFMAIL_IMAP_SSL:
            try:
                M.starttls()
            except Exception as e:
                # Say it rather than quietly sending the password in clear.
                raise RuntimeError(
                    "BRIEFMAIL_IMAP_SSL is false and STARTTLS failed (%s); "
                    "refusing to send credentials over a plaintext link" % e)
        M.login(BRIEFMAIL_IMAP_USER, BRIEFMAIL_IMAP_PASSWORD)
        typ, _ = M.select(BRIEFMAIL_IMAP_FOLDER)
        if typ != "OK":
            raise RuntimeError("cannot select folder %r" % BRIEFMAIL_IMAP_FOLDER)
        typ, data = M.search(None, "UNSEEN")
        if typ != "OK":
            raise RuntimeError("SEARCH UNSEEN failed")
        nums = (data[0] or b"").split()
        # UNSEEN alone is too brittle to be the only way in. Opening the
        # message in webmail, or a sweep that skipped it under a filter set
        # wrong at the time, both clear \Seen forever and the brief becomes
        # permanently invisible with nothing in the log to say so. So when
        # UNSEEN is empty, re-examine the most recent few messages; the
        # Message-ID check makes that idempotent, and a re-read costs a fetch.
        scanned_read = False
        if not nums and BRIEFMAIL_BACKFILL > 0:
            typ, data = M.search(None, "ALL")
            if typ == "OK":
                allnums = (data[0] or b"").split()
                nums = allnums[-BRIEFMAIL_BACKFILL:]
                scanned_read = bool(nums)
        known = briefmail_known_ids()
        n = 0
        for num in nums:
            typ, payload = M.fetch(num, "(RFC822)")
            if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                log.warning("briefmail: fetch of message %s failed", num.decode())
                continue
            verdict, _why = briefmail_consider(payload[0][1], known)
            n += 1
            # Only mark read once the message has actually been dealt with.
            # Flagging one that was passed over for a subject or sender
            # mismatch would bury it the moment that setting is corrected —
            # which is exactly how a briefing goes missing after a rename.
            if verdict in ("stored", "refused"):
                M.store(num, "+FLAGS", "\\Seen")
            if verdict == "stored" and BRIEFMAIL_ARCHIVE_FOLDER:
                typ, _ = M.copy(num, BRIEFMAIL_ARCHIVE_FOLDER)
                if typ == "OK":
                    M.store(num, "+FLAGS", "\\Deleted")
                else:
                    log.warning("briefmail: could not copy to %r; leaving it in %r",
                                BRIEFMAIL_ARCHIVE_FOLDER, BRIEFMAIL_IMAP_FOLDER)
        if BRIEFMAIL_ARCHIVE_FOLDER:
            M.expunge()
        if scanned_read and n:
            log.info("briefmail: nothing unread; re-examined the last %d "
                     "message(s) and deduped by Message-ID", n)
        return n
    finally:
        try:
            M.logout()
        except Exception:
            pass


def briefmail_sweep_once() -> int:
    # A named IMAP host is an unambiguous statement that the mailbox is
    # somewhere else. A maildir path is far more often left over from an
    # earlier attempt, and a local directory nothing delivers to sweeps
    # cleanly forever — indistinguishable from a quiet mailbox. So the
    # remote host wins, and the startup warning says the local path is
    # being ignored rather than leaving it to be discovered.
    if BRIEFMAIL_IMAP_HOST:
        return briefmail_sweep_imap()
    if BRIEFMAIL_MAILDIR:
        return briefmail_sweep_maildir()
    raise RuntimeError(
        "BRIEFMAIL_ENABLED is true but neither BRIEFMAIL_MAILDIR nor "
        "BRIEFMAIL_IMAP_HOST is set in relay.env")


async def briefmail_poll_loop() -> None:
    if not BRIEFMAIL_ENABLED:
        return
    if not BRIEFMAIL_SECRET:
        log.warning("briefmail: BRIEFMAIL_SECRET is unset — any message that "
                    "reaches this mailbox can write a digest")
    if BRIEFMAIL_MAILDIR and BRIEFMAIL_IMAP_HOST:
        # Silent precedence is how someone spends an evening watching a local
        # maildir sweep cleanly while their mail sits on a server the relay
        # was never told to contact. An empty directory and an unreachable
        # one look identical from the log.
        log.warning("briefmail: both BRIEFMAIL_MAILDIR (%s) and BRIEFMAIL_IMAP_HOST "
                    "(%s) are set. Reading the IMAP host; the maildir path is "
                    "ignored. Blank BRIEFMAIL_MAILDIR to silence this.",
                    BRIEFMAIL_MAILDIR, BRIEFMAIL_IMAP_HOST)
    log.info("briefmail: reading %s every %d min",
             BRIEFMAIL_SOURCE_DESC, BRIEFMAIL_POLL_SECONDS // 60)
    while True:
        BRIEFMAIL_STATE.note(last_attempt=iso(time.time()))
        try:
            # imaplib and file IO both block; keep them off the event loop.
            n = await asyncio.to_thread(briefmail_sweep_once)
            BRIEFMAIL_STATE.note(last_ok=True, last_error=None)
            if n:
                log.info("briefmail: examined %d message(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            BRIEFMAIL_STATE.note(last_ok=False,
                                 last_error="%s: %s" % (type(e).__name__, e))
            log.warning("briefmail: sweep failed: %s", e)
        await asyncio.sleep(BRIEFMAIL_POLL_SECONDS)


async def handle_briefmail(request: web.Request) -> web.Response:
    """Status only. No credentials appear here and none should — this route
    is open to the same browsers that read the dashboards."""
    return web.json_response(briefmail_status())


async def handle_digest_get(request: web.Request) -> web.Response:
    try:
        limit = max(1, min(int(request.query.get("limit", "30")), DIGEST_MAX))
    except ValueError:
        limit = 30
    rows = digest_read(limit)
    return web.json_response({
        "generated": iso(time.time()),
        "count": len(rows),
        "write_enabled": bool(DIGEST_TOKEN),
        "digests": rows,
    })


async def handle_digest_post(request: web.Request) -> web.Response:
    if not DIGEST_TOKEN:
        return web.json_response(
            {"error": "digest writes are disabled: DIGEST_TOKEN is unset in relay.env"},
            status=503)
    supplied = request.headers.get("X-Relay-Token", "")
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = supplied or auth[7:].strip()
    # Constant-time-ish: compare full length regardless of where it diverges.
    if len(supplied) != len(DIGEST_TOKEN) or not all(
            a == b for a, b in zip(supplied, DIGEST_TOKEN)):
        return web.json_response({"error": "bad or missing token"}, status=401)
    if request.content_length and request.content_length > 4_000_000:
        return web.json_response({"error": "digest too large"}, status=413)
    try:
        obj = await request.json()
    except Exception as e:
        return web.json_response({"error": "body is not JSON: %s" % e}, status=400)
    if not isinstance(obj, dict):
        return web.json_response({"error": "digest must be a JSON object"}, status=400)
    received = digest_append(obj)
    return web.json_response({"stored": True, "received": received})

# --------------------------------------------------------------------------

START_TS = time.time()


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, x-relay-token, authorization"
    resp.headers["Vary"] = "Origin"
    return resp


def _parse_recenter_request(request: web.Request) -> tuple[float, float] | None:
    """A dashboard names a location by passing ?grid=EM73 or ?lat=..&lon=..
    Used both to move the live APRS-IS filter (/aprs.json) and to pick the
    query centre for /repeaters.json. Returns (lat, lon) if the request
    named a valid, parseable location, else None."""
    grid = request.query.get("grid", "").strip()
    if grid:
        return grid_to_lat_lon(grid)
    lat_s, lon_s = request.query.get("lat"), request.query.get("lon")
    if lat_s is not None and lon_s is not None:
        try:
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            return None
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return (lat, lon)
    return None


async def handle_aprs(request: web.Request) -> web.Response:
    wanted = _parse_recenter_request(request)
    if wanted is not None:
        maybe_recenter(*wanted)
    with FILTER_STATE.lock:
        lat, lon = FILTER_STATE.center
        radius = FILTER_STATE.radius_km
    # "grid" only means relay.env's own GRID setting — once a dashboard has
    # recentred the filter elsewhere, that string no longer describes where
    # this response's center actually is, so it's reported only while the
    # two still agree.
    startup = center_or_none()
    grid_matches = startup is not None and (
        abs(lat - startup[0]) < 0.01 and abs(lon - startup[1]) < 0.01)
    return web.json_response({
        "generated": iso(time.time()),
        "grid": GRID if (GRID and grid_matches) else None,
        "center": {"lat": lat, "lon": lon},
        "radius_km": radius,
        "connected": APRS_STATE.connected,
        "enabled": APRS_ENABLED,
        "note": None if APRS_ENABLED else (
            "APRS_ENABLED is false in relay.env, so this relay is not listening to "
            "APRS-IS. No stations is a configuration state, not a quiet band."),
        "stations": APRS_STATE.snapshot(STATION_MAX_AGE_SECONDS),
    })


async def handle_netlogger(request: web.Request) -> web.Response:
    return web.json_response({
        "generated": NETLOGGER_STATE.generated,
        "ok": NETLOGGER_STATE.last_poll_ok,
        "nets": NETLOGGER_STATE.nets,
    })


async def handle_repeaters(request: web.Request) -> web.Response:
    center = _parse_recenter_request(request)
    if center is None:
        center = center_or_none()  # same default /aprs.json uses before any recentre
    if center is None:
        return web.json_response({
            "error": "no search centre: pass ?grid= or ?lat=&lon=, or set GRID in "
                     "relay.env. This relay has no configured location.",
            "count": 0, "repeaters": [],
        }, status=400)
    lat, lon = center
    try:
        radius_km = float(request.query.get("radius_km", REPEATER_DEFAULT_RADIUS_KM))
    except ValueError:
        radius_km = REPEATER_DEFAULT_RADIUS_KM
    try:
        count = int(request.query.get("count", REPEATER_DEFAULT_COUNT))
    except ValueError:
        count = REPEATER_DEFAULT_COUNT
    service = request.query.get("service") or None
    return web.json_response(REPEATER_CACHE.query(lat, lon, radius_km, service, count))


async def handle_health(request: web.Request) -> web.Response:
    with FILTER_STATE.lock:
        lat, lon = FILTER_STATE.center
        radius = FILTER_STATE.radius_km
    with REPEATER_CACHE.lock:
        rep_count = len(REPEATER_CACHE.records)
        rep_generated = REPEATER_CACHE.generated
        rep_last_ok = REPEATER_CACHE.last_sweep_ok
        rep_last_error = REPEATER_CACHE.last_sweep_error
    rep_generated_ts = _parse_iso(rep_generated)
    rep_age_hours = (time.time() - rep_generated_ts) / 3600.0 if rep_generated_ts else None
    return web.json_response({
        "uptime_s": int(time.time() - START_TS),
        "aprs_connected": APRS_STATE.connected,
        "aprs_last_error": APRS_STATE.last_error,
        "aprs_stations": len(APRS_STATE.stations),
        "aprs_center": {"lat": lat, "lon": lon},
        "aprs_radius_km": radius,
        "netlogger_ok": NETLOGGER_STATE.last_poll_ok,
        "netlogger_last_error": NETLOGGER_STATE.last_error,
        "netlogger_nets": len(NETLOGGER_STATE.nets),
        "netlogger_last_poll": NETLOGGER_STATE.generated,
        "repeater_count": rep_count,
        "repeater_cache_generated": rep_generated,
        "repeater_cache_age_hours": round(rep_age_hours, 1) if rep_age_hours is not None else None,
        "repeater_cache_stale": (rep_age_hours is not None and rep_age_hours > REPEATER_MAX_AGE_HOURS) or rep_generated is None,
        "repeater_last_sweep_ok": rep_last_ok,
        "gridwatch_enabled": GRIDWATCH_ENABLED,
        "kev": KEV_STATE.envelope(),
        "doe202c": DOE_STATE.envelope(),
        "nerc": NERC_STATE.envelope(),
        "isoalerts": ISO_STATE.envelope(),
        "digest_write_enabled": bool(DIGEST_TOKEN),
        "digests_stored": len(digest_read(DIGEST_MAX)),
        "briefmail": briefmail_status(),
        "repeater_last_sweep_error": rep_last_error,
    })


async def on_startup(app: web.Application) -> None:
    app["session"] = ClientSession()
    app["netlogger_task"] = asyncio.create_task(netlogger_poll_loop(app["session"]))
    app["repeater_task"] = asyncio.create_task(repeater_sweep_loop())
    if GRIDWATCH_ENABLED:
        app["kev_task"] = asyncio.create_task(kev_poll_loop(app["session"]))
        app["doe_task"] = asyncio.create_task(doe_poll_loop(app["session"]))
        app["nerc_task"] = asyncio.create_task(nerc_poll_loop(app["session"]))
        app["isoalerts_task"] = asyncio.create_task(isoalerts_poll_loop(app["session"]))
    if BRIEFMAIL_ENABLED:
        app["briefmail_task"] = asyncio.create_task(briefmail_poll_loop())


async def on_cleanup(app: web.Application) -> None:
    app["netlogger_task"].cancel()
    app["repeater_task"].cancel()
    for k in ("kev_task", "doe_task", "nerc_task", "isoalerts_task",
              "briefmail_task"):
        if k in app:
            app[k].cancel()
    await app["session"].close()


def build_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.add_routes([
        web.get("/aprs.json", handle_aprs),
        web.get("/netlogger.json", handle_netlogger),
        web.get("/repeaters.json", handle_repeaters),
        web.get("/kev.json", handle_kev),
        web.get("/doe202c.json", handle_doe),
        web.get("/nerc.json", handle_nerc),
        web.get("/digest.json", handle_digest_get),
        web.post("/digest.json", handle_digest_post),
        web.get("/briefmail.json", handle_briefmail),
        web.get("/isoalerts.json", handle_isoalerts),
        web.get("/health", handle_health),
    ])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    global FILTER_STATE
    if APRS_ENABLED and not CALLSIGN:
        raise SystemExit(
            "APRS_ENABLED is true but CALLSIGN is unset in relay.env. The callsign "
            "is how the relay logs in to APRS-IS (receive-only, passcode derived "
            "from it). Set it, or set APRS_ENABLED=false if you are running this "
            "for the Grid Watch feeds only.")
    if not (APRS_ENABLED or NETLOGGER_ENABLED or REPEATERS_ENABLED or GRIDWATCH_ENABLED):
        raise SystemExit("Every feed is disabled in relay.env — nothing to serve.")
    # A centre is only needed by the feeds that are geographic.
    center = resolve_center() if (APRS_ENABLED or REPEATERS_ENABLED) else (0.0, 0.0)
    FILTER_STATE = FilterState(center=center, radius_km=RADIUS_KM)

    if APRS_ENABLED:
        t = threading.Thread(target=aprs_worker, daemon=True, name="aprs-worker")
        t.start()
    else:
        log.info("APRS-IS disabled (APRS_ENABLED=false) — /aprs.json will report "
                 "not connected with no stations, which is the truth rather than "
                 "an empty area")

    log.info("Listening on %s:%s (CORS origin: %s)", LISTEN_HOST, LISTEN_PORT, ALLOWED_ORIGIN)
    web.run_app(build_app(), host=LISTEN_HOST, port=LISTEN_PORT, print=None)


if __name__ == "__main__":
    main()

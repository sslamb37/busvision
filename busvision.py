#!/usr/bin/env python3
"""
busvision.py — query the bus-vision.jp real-time bus tracker.

Reverse-engineered from https://bus-vision.jp/skbus/ (Ryobi Systems "bus-vision"
platform). Lets you:

  1. search stops by name          -> stop codes
  2. list buses approaching a stop -> with a live "arriving in N min" status
  3. get a bus's live GPS location -> lat/lon on a map

Data flow discovered from the site:
  * Stop search + small lookups go through the Teeda/Seasar ajax endpoint
    POST /skbus/view/teeda.ajax  with form params component=<page> action=<method>.
  * approach.html server-renders the list of approaching buses, each carrying a
    planForecastResultCd / routeCd / orderNum that identifies a specific run.
  * mapApproach.html renders that run's map, embedding the live vehicle position
    in hidden inputs busLatitude / busLongitude. Those are integers = degrees * 360000.

Note the response of teeda.ajax is *not* strict JSON (unquoted keys), so we
parse it leniently.

Usage:
    python3 busvision.py stops <name>
    python3 busvision.py approaching --from <stopCd> --to <stopCd>
    python3 busvision.py approaching --from-name <name> --to-name <name>
    python3 busvision.py locate  <the mapApproach URL or its query params>   # advanced
    python3 busvision.py track   --from-name <name> --to-name <name> [--interval 20]

Defaults target the "skbus" customer site (customerCd=1). Override with --customer
and --base if you point this at another bus-vision deployment.
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://bus-vision.jp/skbus/view"
DEFAULT_CUSTOMER = "1"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# Vehicle coordinates are stored as integer degrees * COORD_SCALE.
COORD_SCALE = 360000.0

# pykakasi mis-reads some place names; override readings here (shared with
# fetch_stops.py). Key = kanji substring, value = correct hiragana.
MANUAL_READINGS = {
    "八柱": "やばしら",     # pykakasi: やはしら
    "五香": "ごこう",
    "秋山": "あきやま",
    "松飛台": "まつひだい",
    "大町": "おおまち",
    "北初富": "きたはつとみ",
    "若芝": "わかしば",     # pykakasi: じゃくし
}


class Romanizer:
    """Kanji/kana -> Hepburn romaji, for showing Japanese-only site data (route
    names, stop names, destinations) in the English UI.

    Uses pykakasi if available; degrades gracefully to returning the original
    text (so EN mode just shows Japanese) if it isn't installed. Applies
    MANUAL_READINGS and a few Japanese-specific touch-ups, and caches results.
    """

    def __init__(self):
        self._kks = None
        self._cache = {}
        self._ok = None

    def available(self):
        if self._ok is None:
            try:
                import pykakasi
                self._kks = pykakasi.kakasi()
                self._ok = True
            except Exception:
                self._ok = False
        return self._ok

    def romanize(self, text):
        if not text or not self.available():
            return text
        if text in self._cache:
            return self._cache[text]
        try:
            # '八柱駅行き' -> "for Yabashira Sta." reads better than "...iki"
            bound = False
            work = text
            if work.endswith("行き"):
                work = work[:-2]
                bound = True
            for kanji, reading in MANUAL_READINGS.items():
                if kanji in work:
                    work = work.replace(kanji, reading)
            parts = self._kks.convert(work)
            words = [p["hepburn"].capitalize() for p in parts if p["hepburn"].strip()]
            out = " ".join(words)
            out = re.sub(r"(?<=\d) (?=\d)", "", out)      # "2 1" -> "21"
            # tidy spacing around separators/brackets the site uses
            out = re.sub(r"\s*[－—–-]\s*", " – ", out)
            out = re.sub(r"\s*（\s*", " (", out)
            out = re.sub(r"\s*）\s*", ") ", out)
            out = re.sub(r"\(\s+", "(", out)
            out = re.sub(r"\s+\)", ")", out)
            out = re.sub(r"\s{2,}", " ", out).strip()
            out = out.replace("Eki", "Sta.")
            if bound:
                out = "for " + out
            self._cache[text] = out
            return out
        except Exception:
            self._cache[text] = text
            return text


class BusVision:
    def __init__(self, base=DEFAULT_BASE, customer=DEFAULT_CUSTOMER, lang="0"):
        self.base = base.rstrip("/")
        self.customer = customer
        self.lang = lang

    # ---- low level ------------------------------------------------------
    def _get(self, path, params=None):
        url = f"{self.base}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")

    def _ajax(self, component, action, extra=None):
        """POST to teeda.ajax and parse the (loose-JSON) response."""
        params = {
            "customerCd": self.customer,
            "lang": self.lang,
            "component": component,
            "action": action,
            "time": "x",
        }
        if extra:
            params.update(extra)
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            f"{self.base}/teeda.ajax",
            data=data,
            headers={"User-Agent": UA,
                     "X-Requested-With": "XMLHttpRequest",
                     "Content-type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
        return _loose_json(raw)

    # ---- public API -----------------------------------------------------
    def search_stops(self, name, *, direction="from", other_cd="-1"):
        """Return [{'value': stopCd, 'label': stopName}, ...] matching `name`."""
        if direction == "from":
            extra = {"stopNmFrom": name, "stopCdTo": other_cd}
            action = "ajaxGetStopFrom"
        else:
            extra = {"stopNmTo": name, "stopCdFrom": other_cd}
            action = "ajaxGetStopTo"
        res = self._ajax("searchStopPage", action, extra)
        return res or []

    def resolve_stop(self, name, *, direction="from"):
        """Best-effort single stop code for a name (exact match preferred)."""
        hits = self.search_stops(name, direction=direction)
        if not hits:
            raise LookupError(f"no stop matched {name!r}")
        for h in hits:
            if h.get("label") == name:
                return h
        return hits[0]

    def approaching(self, stop_from, stop_to):
        """List buses approaching, traveling stop_from -> stop_to.

        Returns list of dicts with the run identifiers + human status text.
        """
        html = self._get("approach.html", {
            "stopCdFrom": stop_from,
            "stopCdTo": stop_to,
            "searchHour": "", "searchMinute": "", "searchAD": "-1",
            "lang": self.lang, "searchVehicleTypeCd": "",
        })
        return _parse_approach(html)

    def locate(self, run):
        """Given a run dict (from approaching()), fetch its live map position."""
        params = {
            "stopCdFrom": run["stopCdFrom"], "stopCdTo": run["stopCdTo"],
            "planForecastResultCd": run["planForecastResultCd"],
            "orderNumFrom": run["orderNumFrom"], "orderNumTo": run["orderNumTo"],
            "searchHour": "", "searchMinute": "", "searchAD": "-1",
            "searchVehicleTypeCd": "", "searchCorpCd": "",
            "revYmd": run["revYmd"], "routeCd": run["routeCd"],
            "updownCd": run["updownCd"], "loopFlg": run.get("loopFlg", "false"),
            "lang": self.lang,
        }
        html = self._get("mapApproach.html", params)
        return _parse_map(html)

    def live_buses(self, stop_from, stop_to, *, dedupe=True):
        """Approaching buses with live GPS, one (soonest) per route number.

        Groups by the display route number (the leading "12" / "14" in the
        route name). When dedupe is True, keeps only the earliest-arriving run
        per number. Returns a list of merged run+location dicts that have a
        live position.
        """
        runs = self.approaching(stop_from, stop_to)
        # order by ETA minutes so "first seen" per group == soonest
        runs.sort(key=lambda r: _eta_minutes(r.get("eta")))
        chosen = []
        seen_numbers = set()
        for r in runs:
            num = _route_number(r.get("route"))
            if dedupe and num is not None and num in seen_numbers:
                continue
            loc = self.locate(r)
            if not loc or loc.get("lat") is None:
                continue  # no live position yet — skip
            if dedupe and num is not None:
                seen_numbers.add(num)
            chosen.append({
                **r,
                "routeNumber": num,
                "isLoop": _is_loop(r, loc),
                "location": loc,
            })
        return chosen


class Tracker:
    """Sticky tracking of buses for one journey (stop_from -> stop_to).

    approach.html only lists buses that have NOT yet reached the boarding stop
    (the ones you can still catch). Once a bus passes your stop it drops off
    that list — but its run can still be located directly. This tracker
    remembers every run it has seen and keeps polling its live position until
    the run finishes, so a bus doesn't vanish as it nears/passes your stop.

    A bus is dropped when its run finishes (no live position) or `linger_after_
    arrival` seconds after it reaches the destination stop, whichever comes
    first — so it doesn't linger on the map indefinitely past your alight stop.

    Call poll() once per refresh; it returns the current display list.
    """

    def __init__(self, bv, stop_from, stop_to, *, dedupe=True,
                 linger_after_arrival=60):
        self.bv = bv
        self.stop_from = stop_from
        self.stop_to = stop_to
        self.dedupe = dedupe
        self.linger_after_arrival = linger_after_arrival
        self._tracked = {}   # planForecastResultCd -> run dict
        self._arrived = {}    # planForecastResultCd -> monotonic time it arrived

    def poll(self):
        runs = self.bv.approaching(self.stop_from, self.stop_to)
        running = [r for r in runs if not r.get("predeparture")]
        predep = [r for r in runs if r.get("predeparture")]
        running.sort(key=lambda r: _eta_minutes(r.get("eta")))
        predep.sort(key=lambda r: _departure_minutes(r.get("boardTime")))

        # 1) running buses (not yet at boarding stop), deduped by route number
        to_locate = []
        approaching_plans = set()
        seen_numbers = set()
        for r in running:
            num = _route_number(r.get("route"))
            if self.dedupe and num is not None and num in seen_numbers:
                continue
            if self.dedupe and num is not None:
                seen_numbers.add(num)
            to_locate.append(r)
            approaching_plans.add(r.get("planForecastResultCd"))

        # 2) sticky runs we were already tracking that left the approach list
        #    (i.e. departed our stop) — keep following them.
        for plan, r in self._tracked.items():
            if plan not in approaching_plans:
                to_locate.append(r)

        # 3) locate everything; a run with no live position has finished -> drop
        out = []
        new_tracked = {}
        new_arrived = {}
        now = time.monotonic()
        for r in to_locate:
            plan = r.get("planForecastResultCd")
            loc = self.bv.locate(r)
            if not loc or loc.get("lat") is None:
                continue  # finished / no position -> stop tracking it

            departed = plan not in approaching_plans

            # boundary: drop the bus `linger_after_arrival`s after it reaches
            # the destination (alight stop, or the final stop of the route).
            # Only meaningful once it has departed the boarding stop — otherwise
            # a loop line sitting at its start stop (which can equal the alight
            # stop) would look "arrived" before the ride has begun.
            arrived_at = self._arrived.get(plan)
            if arrived_at is None and departed and _at_destination(loc):
                arrived_at = now
            if arrived_at is not None:
                if now - arrived_at >= self.linger_after_arrival:
                    continue  # lingered long enough past destination -> drop
                new_arrived[plan] = arrived_at

            out.append({
                **r,
                "routeNumber": _route_number(r.get("route")),
                "isLoop": _is_loop(r, loc),
                "departed": departed,
                "arrived": arrived_at is not None,
                "location": loc,
            })
            new_tracked[plan] = r
        self._tracked = new_tracked
        self._arrived = new_arrived
        # show soonest / already-departed first
        out.sort(key=lambda b: (not b["departed"], _eta_minutes(b.get("eta"))))

        # 4) pre-departure buses (no GPS): show the soonest per route number, but
        #    only for a route number that has no live bus already shown.
        live_numbers = {b.get("routeNumber") for b in out}
        seen_predep = set()
        for r in predep:
            num = _route_number(r.get("route"))
            if num in live_numbers or num in seen_predep:
                continue
            seen_predep.add(num)
            out.append({
                **r,
                "routeNumber": num,
                "isLoop": _is_loop(r, {"route_stops": [],
                                       "boardStop": r.get("boardStop"),
                                       "alightStop": r.get("alightStop")}),
                "departed": False,
                "arrived": False,
                "location": None,   # no GPS — panel-only, no map marker
            })
        return out


# ---- parsing helpers ----------------------------------------------------
def _at_destination(loc):
    """True once the bus has reached the journey's destination.

    The destination is the alight stop (降); we also treat reaching the final
    stop of the route as arrival, in case the alight name isn't matched.
    """
    cur = loc.get("currentStop")
    if not cur:
        return False
    if loc.get("alightStop") and cur == loc["alightStop"]:
        return True
    stops = loc.get("route_stops", [])
    if stops and cur == stops[-1]["name"]:
        return True
    # index-based fallback: current stop is the last in the sequence
    idx = loc.get("currentStopIndex")
    if idx is not None and stops and idx >= len(stops) - 1:
        return True
    return False


def _is_loop(run, loc):
    """True if this is a loop line or the ride wraps around the stop sequence.

    Detected either by the route name (循環 = "loop") or the site's own loopFlg,
    or geometrically: the boarding stop appears at/after the alighting stop in
    the route's stopName array (so you ride "around" rather than straight).
    """
    if str(run.get("loopFlg", "")).lower() == "true":
        return True
    route = run.get("route") or ""
    if "循環" in route or "循环" in route:
        return True
    names = [s["name"] for s in loc.get("route_stops", [])]
    board, alight = loc.get("boardStop"), loc.get("alightStop")
    if board in names and alight in names:
        if names.index(board) >= names.index(alight):
            return True
    return False


def _route_number(route):
    """Leading display number of a route name ('14 新松戸駅－...' -> '14')."""
    if not route:
        return None
    m = re.match(r"\s*(\d+)", route)
    return m.group(1) if m else None


def _eta_minutes(eta):
    """Sortable minutes-until-arrival from an ETA blurb (unknown -> large)."""
    if not eta:
        return 9999
    m = re.search(r"あと(\d+)分", eta)
    if m:
        return int(m.group(1))
    if "間もなく" in eta:
        return 0
    return 9998  # "発車予定" etc. — hasn't departed yet, sort after live ETAs


def _departure_minutes(board_time):
    """Sortable key from a 'HH:MM' scheduled departure (unknown -> large)."""
    if not board_time:
        return 99999
    m = re.match(r"(\d{1,2}):(\d{2})", board_time)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 99999


def eta_minutes(eta):
    """Public: minutes-to-arrival as an int, or None if not a countdown.

    Handles both the JP blurb (あとN分…) and the English one (…in N minute(s)).
    """
    if not eta:
        return None
    m = re.search(r"あと(\d+)分", eta) or re.search(r"in\s+(\d+)\s+minute", eta)
    if m:
        return int(m.group(1))
    if "間もなく" in eta or "arrive soon" in eta.lower():
        return 0
    return None


def delay_minutes(delay):
    """Signed delay in minutes: +late / 0 on-time / None if unknown.

    Parses '約N分遅れ' (late), 'ほぼ定刻'/'定刻' (on time). English deployments
    return English phrases we pass through as-is when unparsed.
    """
    if not delay:
        return None
    m = re.search(r"約?(\d+)分遅れ", delay) or re.search(r"(\d+)\s*min.*(late|delay)", delay)
    if m:
        return int(m.group(1))
    if "定刻" in delay or "on time" in delay.lower():
        return 0
    return None


def _loose_json(text):
    """teeda.ajax returns JS-ish objects with unquoted keys. Quote them, parse."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # quote bare object keys:  {value:1,label:"x"} -> {"value":1,"label":"x"}
    fixed = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', text)
    return json.loads(fixed)


def _hidden(html, name):
    m = re.search(r'name="%s"\s+value="([^"]*)"' % re.escape(name), html)
    return m.group(1) if m else None


def _split_array_field(val):
    """Fields like '[a, b, c]' -> ['a','b','c']."""
    if val is None:
        return []
    val = val.strip()
    if val.startswith("["):
        val = val[1:]
    if val.endswith("]"):
        val = val[:-1]
    return [p.strip() for p in val.split(",")]


def _parse_approach(html):
    """Extract each approaching bus from approach.html.

    Two kinds of entries appear:
      * running buses — carry a mapApproach.html link with the run ids (and GPS
        via locate()); parsed from the block preceding the link.
      * pre-departure buses (発車前) — scheduled but not yet departed the origin,
        so they have NO map link and NO GPS. We still surface them (with a
        `predeparture` flag) so the journey isn't shown as "no buses" when the
        next few departures are simply waiting to leave.
    """
    runs = []
    seen = set()

    # 1) running buses (have a map link)
    linked_spans = []
    for m in re.finditer(r'mapApproach\.html\?([^"\']+)', html):
        qs = m.group(1).replace("&amp;", "&")
        params = dict(urllib.parse.parse_qsl(qs))
        key = params.get("planForecastResultCd")
        if not key or key in seen:
            continue
        seen.add(key)
        info = _parse_item(html[max(0, m.start() - 3600):m.start()])
        runs.append({
            "planForecastResultCd": params.get("planForecastResultCd"),
            "routeCd": params.get("routeCd"),
            "updownCd": params.get("updownCd"),
            "orderNumFrom": params.get("orderNumFrom"),
            "orderNumTo": params.get("orderNumTo"),
            "revYmd": params.get("revYmd"),
            "stopCdFrom": params.get("stopCdFrom"),
            "stopCdTo": params.get("stopCdTo"),
            "loopFlg": params.get("loopFlg", "false"),
            "predeparture": False,
            **info,
        })

    # 2) pre-departure buses (発車前) — one <div id="approachInfo"> per item,
    #    no map link. Slice from each approachInfo to the next (or to a map link).
    infos = [m.start() for m in re.finditer(r'id="approachInfo"', html)]
    for idx, start in enumerate(infos):
        end = infos[idx + 1] if idx + 1 < len(infos) else start + 4000
        block = html[start:end]
        if "mapApproach.html" in block:
            continue  # this item is a running bus, already handled above
        if "発車前" not in block and "発車予定" not in block:
            continue
        runs.append(_parse_predeparture(block))

    return runs


def _tag_text(block, elem_id):
    """Inner text of <... id="elem_id" ...>TEXT</...> (tags stripped)."""
    m = re.search(r'id="%s"[^>]*>(.*?)</' % re.escape(elem_id), block, re.S)
    if not m:
        return None
    txt = _html_unescape(re.sub(r"<[^>]+>", "", m.group(1)))
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt or None


def _parse_predeparture(block):
    """Parse a 発車前 (not-yet-departed) item into a run dict (no GPS)."""
    approach = _tag_text(block, "approachInfo")     # "15:25に新松戸駅を発車予定"
    times = _tag_text(block, "passTimeInfo")         # "15:25発 ⇒ 終点着（予定）"
    route = _tag_text(block, "routeNm")
    dest = _tag_text(block, "destNm")
    board = _tag_text(block, "stopNmFrom")
    board_time = _tag_text(block, "passTimeFromText")  # scheduled departure "15:25"
    alight = _tag_text(block, "stopNmTo")
    # boarding stop code from its timetable link
    mcd = re.search(r'stopFromTimetable"[^>]*href="timetable\.html\?stopCd=(\d+)', block)
    board_cd = mcd.group(1) if mcd else None
    delay = None
    dm = re.search(r"約\d+分遅れ|ほぼ定刻", block)
    if dm:
        delay = dm.group(0)
    status_bits = [b for b in (approach, route, dest, times) if b]
    return {
        "planForecastResultCd": None,   # no run id (not departed)
        "routeCd": None, "updownCd": None,
        "orderNumFrom": None, "orderNumTo": None, "revYmd": None,
        "stopCdFrom": board_cd, "stopCdTo": None, "loopFlg": "false",
        "predeparture": True,
        "eta": approach,                 # "…に…を発車予定"
        "times": times,
        "route": route,
        "destination": dest,
        "location": board,               # sits at the boarding stop
        "boardStop": board,
        "boardTime": board_time,
        "alightStop": alight,
        "delay": delay,
        "status": " / ".join(status_bits) if status_bits else "発車前",
    }


def _parse_item(fragment):
    """Pull ETA / times / route / destination / location / delay from a block."""
    txt = re.sub(r"<[^>]+>", " ", fragment)
    txt = _html_unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()

    def find(pat):
        mm = re.search(pat, txt)
        return mm.group(0).strip() if mm else None

    eta = find(r"あと\d+分で到着予定") or find(r"\d{1,2}:\d{2}に[^ ]+を発車予定") \
        or find(r"間もなく到着")
    times = find(r"\d{1,2}:\d{2}発\s*⇒\s*(?:\d{1,2}:\d{2}|終点)着（[^）]*）")
    route = find(r"\d+\s*　?[^ ]+[－-][^ ]+")
    dest = find(r"[^ ]+行き")
    delay = find(r"約\d+分遅れ") or find(r"ほぼ定刻") or find(r"定刻")
    # current bus location: the stop name right after the destination
    location = None
    if dest:
        after = txt.split(dest, 1)[1].strip()
        loc_m = re.match(r"([^ （(]+)", after)
        if loc_m and "個前" not in loc_m.group(1):
            location = loc_m.group(1)
    status_bits = [b for b in (eta, route, dest, times, delay) if b]
    return {
        "eta": eta,
        "times": times,
        "route": route,
        "destination": dest,
        "location": location,
        "delay": delay,
        "status": " / ".join(status_bits) if status_bits else txt[:120],
    }


def _parse_map(html):
    """Extract live vehicle position + stop list from mapApproach.html."""
    lat_raw = _hidden(html, "busLatitude")
    lon_raw = _hidden(html, "busLongitude")
    if lat_raw in (None, "", "0") and lon_raw in (None, "", "0"):
        return None  # no live position (run may be finished / not yet started)
    lat = int(lat_raw) / COORD_SCALE if lat_raw else None
    lon = int(lon_raw) / COORD_SCALE if lon_raw else None
    stops = _split_array_field(_hidden(html, "stopName"))
    pass_times = _split_array_field(_hidden(html, "stopPassTime"))
    # kbn per stop: 3 = actual/already passed, 2 = predicted/upcoming, 1 = plan
    kbns = _split_array_field(_hidden(html, "planForecastResultKbn"))
    current_stop = _hidden(html, "vehicleStopName")

    # Build the ordered stop list with a "passed" flag and the current index.
    route_stops = []
    current_index = None
    for i, name in enumerate(stops):
        kbn = kbns[i] if i < len(kbns) else None
        route_stops.append({
            "index": i,
            "name": name,
            "time": pass_times[i] if i < len(pass_times) else None,
            "kbn": kbn,
            "passed": kbn == "3",
        })
        if current_stop and name == current_stop and current_index is None:
            current_index = i
    # Fall back: current stop = last one already passed (kbn==3).
    if current_index is None and route_stops:
        passed_idx = [s["index"] for s in route_stops if s["passed"]]
        if passed_idx:
            current_index = max(passed_idx)

    return {
        "lat": lat,
        "lon": lon,
        "raw_lat": lat_raw,
        "raw_lon": lon_raw,
        "vehicleName": _hidden(html, "vehicleName"),
        "vehicleTypeName": _hidden(html, "vehicleTypeName"),
        "currentStop": current_stop,
        "currentStopIndex": current_index,
        "passTime": _hidden(html, "vehiclePassTime"),
        "approach": _hidden(html, "vehicleapproachItems"),
        "boardStop": _hidden(html, "stopOnName"),
        "alightStop": _hidden(html, "stopOffName"),
        "zoom": _hidden(html, "zoomLevel"),
        "route_stops": route_stops,
        "maps_url": (f"https://www.google.com/maps?q={lat},{lon}"
                     if lat and lon else None),
    }


def _html_unescape(s):
    import html as _h
    return _h.unescape(s)


# ---- CLI ----------------------------------------------------------------
def _cmd_stops(bv, args):
    hits = bv.search_stops(args.name, direction=args.direction)
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return
    if not hits:
        print("(no matches)")
        return
    for h in hits:
        print(f"{h['value']:>8}  {h['label']}")


def _resolve_endpoints(bv, args):
    if args.from_name:
        f = bv.resolve_stop(args.from_name, direction="from")
        stop_from = f["value"]
        print(f"# from: {f['label']} ({stop_from})", file=sys.stderr)
    else:
        stop_from = args.from_cd
    if args.to_name:
        t = bv.resolve_stop(args.to_name, direction="to")
        stop_to = t["value"]
        print(f"# to:   {t['label']} ({stop_to})", file=sys.stderr)
    else:
        stop_to = args.to_cd
    if not stop_from or not stop_to:
        sys.exit("error: need both endpoints (--from/--to or --from-name/--to-name)")
    return stop_from, stop_to


def _cmd_approaching(bv, args):
    stop_from, stop_to = _resolve_endpoints(bv, args)
    runs = bv.approaching(stop_from, stop_to)
    if args.json:
        print(json.dumps(runs, ensure_ascii=False, indent=2))
        return
    if not runs:
        print("(no buses currently approaching)")
        return
    for i, r in enumerate(runs, 1):
        print(f"[{i}] plan={r['planForecastResultCd']} route={r['routeCd']} "
              f"updown={r['updownCd']}")
        print(f"    {r['status']}")


def _cmd_track(bv, args):
    stop_from, stop_to = _resolve_endpoints(bv, args)
    while True:
        runs = bv.approaching(stop_from, stop_to)
        if not runs:
            print("(no buses currently approaching)")
        for i, r in enumerate(runs, 1):
            loc = bv.locate(r)
            head = (f"[{i}] {r['status']}")
            if loc:
                print(f"{head}\n    bus#{loc['vehicleName']} @ "
                      f"{loc['lat']:.6f},{loc['lon']:.6f}  "
                      f"near {loc['currentStop']}  {loc['approach'] or ''}")
                print(f"    map: {loc['maps_url']}")
            else:
                print(f"{head}\n    (no live GPS position yet)")
        if not args.interval:
            return
        print(f"--- sleeping {args.interval}s (Ctrl-C to stop) ---")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return


def main():
    p = argparse.ArgumentParser(description="Query bus-vision.jp real-time buses.")
    p.add_argument("--base", default=DEFAULT_BASE, help="site base URL")
    p.add_argument("--customer", default=DEFAULT_CUSTOMER, help="customerCd")
    p.add_argument("--json", action="store_true", help="raw JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stops", help="search stops by name")
    s.add_argument("name")
    s.add_argument("--direction", choices=["from", "to"], default="from")

    def add_endpoints(sp):
        sp.add_argument("--from", dest="from_cd", help="origin stop code")
        sp.add_argument("--to", dest="to_cd", help="destination stop code")
        sp.add_argument("--from-name", help="origin stop name (auto-resolved)")
        sp.add_argument("--to-name", help="destination stop name (auto-resolved)")

    a = sub.add_parser("approaching", help="list approaching buses")
    add_endpoints(a)

    t = sub.add_parser("track", help="list approaching buses + live GPS")
    add_endpoints(t)
    t.add_argument("--interval", type=int, default=0,
                   help="repeat every N seconds (0 = once)")

    args = p.parse_args()
    bv = BusVision(base=args.base, customer=args.customer)

    if args.cmd == "stops":
        _cmd_stops(bv, args)
    elif args.cmd == "approaching":
        _cmd_approaching(bv, args)
    elif args.cmd == "track":
        _cmd_track(bv, args)


if __name__ == "__main__":
    main()

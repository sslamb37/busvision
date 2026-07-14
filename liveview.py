#!/usr/bin/env python3
"""
liveview.py — live map view of bus-vision.jp buses approaching a stop.

Starts a tiny local web server that serves a Leaflet map. The browser polls a
local JSON endpoint (/api/buses) which scrapes bus-vision.jp server-side, so
there are no cross-origin problems and no API key.

Buses are grouped by their display route number (e.g. 12, 14) and only the
soonest-arriving bus of each number is shown.

On launch it opens a search page where you pick From / To stops (searchable by
romaji, kana, or kanji from the embedded stops.json), then shows the live map
for that journey. Run `python3 fetch_stops.py` once to build stops.json.

Usage:
    python3 liveview.py                       # opens the stop picker
    python3 liveview.py --port 8000 --lang ja

Then open http://localhost:8000 in a browser.
"""

import argparse
import json
import os
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from busvision import (BusVision, Tracker, Romanizer,
                       eta_minutes, delay_minutes)


PAGE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>bus-vision live</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin: 0; height: 100%%; font-family: system-ui, sans-serif; }
  #map { position: absolute; inset: 0 0 0 0; }
  #panel {
    position: absolute; z-index: 1000; top: 10px; left: 10px;
    background: rgba(255,255,255,.94); border-radius: 10px; padding: 10px 12px;
    box-shadow: 0 2px 10px rgba(0,0,0,.25); max-width: 380px; font-size: 13px;
    max-height: calc(100vh - 40px); overflow-y: auto;
  }
  #panel h1 { font-size: 14px; margin: 0 0 2px; }
  #changebar { margin: 0 0 6px; }
  #changelink { font-size: 11px; color: #1976d2; text-decoration: none; }
  #panel .route { font-weight: 700; }
  #panel .meta { color: #555; font-size: 12px; margin-top: 2px; }
  .box { border-top: 1px solid #eee; }
  .box-head { padding: 5px 0; cursor: pointer; }
  .box-head:hover { background: #f2f6ff; }
  .box-head .caret { display: inline-block; width: 14px; color: #999; }
  .box-body { padding: 0 0 6px 14px; }
  .box.collapsed .box-body { display: none; }
  .locatebtn { display: inline-block; font-size: 11px; color: #1976d2;
    cursor: pointer; margin-top: 3px; text-decoration: none; }
  #status { color: #888; font-size: 11px; margin-top: 6px; }
  .prog { margin: 4px 0 2px; line-height: 1.6; }
  .prog .stop {
    display: inline-block; font-size: 10px; padding: 1px 5px; margin: 1px;
    border-radius: 8px; background: #eee; color: #999; white-space: nowrap;
  }
  .prog .stop.passed { background: #cfe3cf; color: #4a7a4a; }
  .prog .stop.now { background: #1976d2; color: #fff; font-weight: 700; }
  .prog .stop.upcoming { background: #eef2fa; color: #556; }
  .prog .stop.journey { outline: 2px solid #e65100; outline-offset: -1px; }
  .journey-line { font-size: 12px; color: #333; margin: 3px 0 2px; }
  .journey-line b { color: #e65100; }
  .looptag {
    display: inline-block; font-size: 9px; font-weight: 700; vertical-align: middle;
    background: #ede7f6; color: #6a1b9a; padding: 1px 5px; border-radius: 8px;
  }
  .deptag {
    display: inline-block; font-size: 9px; font-weight: 700; vertical-align: middle;
    background: #fff3e0; color: #e65100; padding: 1px 5px; border-radius: 8px;
  }
  .deptag.arrived { background: #e8f5e9; color: #2e7d32; }
  .deptag.sched { background: #eceff1; color: #546e7a; }
  .box.sched .route { opacity: .8; }
  .box-body .times { font-size: 11px; color: #666; margin: 2px 0 3px; }
  #langbar { float: right; }
  .langbtn {
    font-size: 11px; padding: 2px 8px; border: 1px solid #ccc; background: #fff;
    cursor: pointer; color: #555;
  }
  .langbtn:first-child { border-radius: 6px 0 0 6px; }
  .langbtn:last-child { border-radius: 0 6px 6px 0; border-left: none; }
  .langbtn.active { background: #1976d2; color: #fff; border-color: #1976d2; }
  .busicon.departed { opacity: .6; border-style: dashed; }
  .busicon {
    background: #1976d2; color: #fff; border-radius: 50%%;
    width: 34px; height: 34px; line-height: 34px; text-align: center;
    font-weight: 700; box-shadow: 0 1px 4px rgba(0,0,0,.4);
    border: 2px solid #fff; font-size: 13px;
  }
</style>
</head>
<body>
<div id="map"></div>
<div id="panel">
  <div id="langbar">
    <button id="lang-en" class="langbtn">EN</button><button id="lang-ja" class="langbtn">日本語</button>
  </div>
  <h1 id="title">…</h1>
  <div id="changebar"><a href="/" id="changelink">&#8592;</a></div>
  <div id="list"><em>loading…</em></div>
  <div id="status"></div>
</div>
<script>
const INTERVAL = %(interval)d * 1000;
// journey comes from the URL (?from=CODE&to=CODE&fromName=..&toName=..)
const Q = new URLSearchParams(location.search);
const FROM = Q.get('from'), TO = Q.get('to');
const FROM_JA = Q.get('fromName') || FROM, TO_JA = Q.get('toName') || TO;
// title-case the lowercase romaji from stops.json for display
function titleize(s) { return (s||'').replace(/\b\w/g, c => c.toUpperCase()); }
const FROM_EN = titleize(Q.get('fromEn')), TO_EN = titleize(Q.get('toEn'));
if (!FROM || !TO) { location.href = '/'; }
function journeyNames() {
  return (LANG === 'en' && FROM_EN)
    ? { from: FROM_EN, to: TO_EN } : { from: FROM_JA, to: TO_JA };
}
function updateTitle() {
  const j = journeyNames();
  document.getElementById('title').textContent = j.from + ' → ' + j.to;
}
const map = L.map('map').setView([%(lat).6f, %(lon).6f], 13);
// Esri World Street Map — labels in English/romaji (OSM default is local-language)
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', {
  maxZoom: 19, attribution: 'Tiles © Esri'
}).addTo(map);

const colors = ['#1976d2','#e53935','#43a047','#fb8c00','#8e24aa','#00897b','#6d4c41'];
let markers = {};   // key -> marker
let trails  = {};    // key -> {line, pts:[[lat,lon],...], lastKey}
let firstFit = true;
let lastData = null; // most recent API payload, for re-render on language switch
let expandedRoutes = new Set(); // route keys the user has expanded (default: collapsed)

// ---- UI language (labels WE add). Stop/route names come from the site and
// are only available in Japanese on this deployment, so they pass through. ----
let LANG = localStorage.getItem('bvLang') || '%(lang)s';
const I18N = {
  en: {
    board: 'from', alight: 'to', loop: 'LOOP', departed: 'DEPARTED',
    arrived: 'ARRIVED', bus: 'bus', near: 'near', stop: 'stop', of: 'of',
    enroute: 'en route to', arrivedAt: 'arrived at', noBuses: 'no buses currently approaching',
    updated: 'updated', showOnMap: 'show on map', changeStops: '← change stops',
    scheduled: 'SCHEDULED', departsAt: 'departs from', departsTime: 'departs',
    busesN: n => n + (n === 1 ? ' bus' : ' buses'),
    eta: m => (m === 0 ? 'arriving now' : 'in ' + m + (m === 1 ? ' min' : ' mins')),
    delay: m => (m === 0 ? 'on time' : '+' + m + ' min late'),
  },
  ja: {
    board: '乗', alight: '降', loop: '循環', departed: '出発済',
    arrived: '到着', bus: '車両', near: '付近', stop: '停留所', of: '/',
    enroute: '行き（走行中）', arrivedAt: '到着', noBuses: '接近中のバスはありません',
    updated: '更新', showOnMap: '地図で表示', changeStops: '← 停留所を変更',
    scheduled: '発車前', departsAt: '発車', departsTime: '発車',
    busesN: n => n + '台',
    eta: m => (m === 0 ? 'まもなく到着' : 'あと' + m + '分'),
    delay: m => (m === 0 ? 'ほぼ定刻' : '約' + m + '分遅れ'),
  },
};
function T() { return I18N[LANG]; }

// pick a name in the current UI language: romaji (_en) when English is active
// and available, else the site's Japanese. `obj` is a bus or stop; `field` the
// base key ('route', 'destination', 'location_stop', 'boardStop', ...).
function nm(obj, field) {
  if (!obj) return '';
  const en = obj[field + '_en'];
  if (LANG === 'en' && en) return en;
  return obj[field] || en || '';
}

function setLang(l) {
  LANG = l;
  localStorage.setItem('bvLang', l);
  document.getElementById('lang-en').classList.toggle('active', l === 'en');
  document.getElementById('lang-ja').classList.toggle('active', l === 'ja');
  document.getElementById('changelink').textContent = T().changeStops;
  updateTitle();
  if (lastData) render(lastData);   // re-render instantly, no refetch
}

function colorFor(num) {
  if (num == null) return '#555';
  let h = 0; for (const c of String(num)) h = (h*31 + c.charCodeAt(0)) & 255;
  return colors[h %% colors.length];
}

// build the ordered stop "progress strip" for a bus.
// The route's stopName array is the *route's* full loop, which may not contain
// your journey's board/alight stops (esp. on loop lines) — so we mark those
// explicitly with 乗/降 badges.
function progressHtml(b) {
  if (!b.stops || !b.stops.length) return '';
  const now = b.currentStopIndex;
  const chips = b.stops.map(s => {
    let cls = 'upcoming';
    if (s.index === now) cls = 'now';
    else if (s.passed) cls = 'passed';
    let badge = '';
    if (s.name === b.boardStop) badge = '<b>'+T().board+'</b> ';
    if (s.name === b.alightStop) badge = '<b>'+T().alight+'</b> ';
    const mark = (s.name === b.boardStop || s.name === b.alightStop) ? ' journey' : '';
    return '<span class="stop '+cls+mark+'" title="'+(s.time||'')+'">'+badge+nm(s,'name')+'</span>';
  }).join('');
  // journey header: your board -> alight (always shown, even if a stop isn't
  // in the route's own array, e.g. loop lines)
  const jrny = (b.boardStop || b.alightStop)
    ? '<div class="journey-line"><b>'+T().board+'</b> '+(nm(b,'boardStop')||'?')
      + ' → <b>'+T().alight+'</b> '+(nm(b,'alightStop')||'?')+'</div>'
    : '';
  return jrny + '<div class="prog">'+chips+'</div>';
}

async function refresh() {
  let data;
  try {
    const r = await fetch('/api/buses?from=' + encodeURIComponent(FROM)
                          + '&to=' + encodeURIComponent(TO));
    data = await r.json();
  } catch (e) {
    document.getElementById('status').textContent = 'fetch error: ' + e;
    return;
  }
  if (data.error) {
    document.getElementById('status').textContent = 'error: ' + data.error;
    return;
  }
  lastData = data;
  render(data);
}

// format ETA / delay in the current UI language from numeric fields, falling
// back to the raw site string when we couldn't parse a number.
function etaStr(b) {
  const dest = nm(b, 'alightStop') || nm(b, 'destination');
  if (b.predeparture) {
    const parts = [T().departsTime + ' ' + (b.boardTime || '?')];
    if (b.delayMin != null) parts.push(T().delay(b.delayMin)); else if (b.delay) parts.push(b.delay);
    return parts.join(' · ');
  }
  if (b.arrived) return T().arrivedAt + ' ' + dest;
  if (b.departed) return T().enroute + ' ' + dest;
  const parts = [];
  if (b.etaMin != null) parts.push(T().eta(b.etaMin)); else if (b.eta) parts.push(b.eta);
  if (b.delayMin != null) parts.push(T().delay(b.delayMin)); else if (b.delay) parts.push(b.delay);
  return parts.join(' · ');
}

function render(data) {
  const buses = data.buses || [];
  const seen = new Set();
  const bounds = [];
  const listEl = document.getElementById('list');
  listEl.innerHTML = '';

  buses.forEach(b => {
    const key = b.routeNumber || b.planForecastResultCd;
    seen.add(key);
    const col = colorFor(b.routeNumber);
    const hasPos = (b.lat != null && b.lon != null);  // pre-departure => false
    const ll = hasPos ? [b.lat, b.lon] : null;

    if (hasPos) {
      bounds.push(ll);
      // GPS breadcrumb trail — append when the position actually changed
      if (!trails[key]) {
        trails[key] = { line: L.polyline([], {color: col, weight: 4, opacity: .7}).addTo(map),
                        pts: [], lastKey: null };
      }
      const posKey = b.lat.toFixed(6)+','+b.lon.toFixed(6);
      if (trails[key].lastKey !== posKey) {
        trails[key].pts.push(ll);
        trails[key].line.setLatLngs(trails[key].pts);
        trails[key].lastKey = posKey;
      }
    }

    const progHead = (b.currentStopIndex != null && b.stops && b.stops.length)
      ? ' ('+T().stop+' '+(b.currentStopIndex+1)+' '+T().of+' '+b.stops.length+')' : '';
    const etaText = etaStr(b);
    const depTag = b.arrived
      ? ' <span class="deptag arrived">'+T().arrived+'</span>'
      : b.predeparture ? ' <span class="deptag sched">'+T().scheduled+'</span>'
      : b.departed ? ' <span class="deptag">'+T().departed+'</span>' : '';
    const loopTag = b.isLoop ? ' <span class="looptag">'+T().loop+'</span>' : '';

    if (hasPos) {
      const depClass = b.departed ? ' departed' : '';
      const icon = L.divIcon({
        className: '', iconSize: [34,34], iconAnchor: [17,17],
        html: '<div class="busicon'+depClass+'" style="background:'+col+'">'
              + (b.routeNumber || '?') + '</div>'
      });
      const popup =
        '<b>'+nm(b,'route')+'</b>'+loopTag+depTag
        + '<br>'+nm(b,'destination')
        + '<br>'+T().bus+' #'+(b.vehicleName||'?')
        + '<br>' + etaText
        + '<br>'+T().near+' '+nm(b,'location_stop')+progHead
        + '<br>'+(b.times||'')
        + progressHtml(b);
      if (markers[key]) {
        markers[key].setLatLng(ll).setIcon(icon).setPopupContent(popup);
      } else {
        markers[key] = L.marker(ll, {icon}).addTo(map).bindPopup(popup);
      }
    }

    const ofN = (b.currentStopIndex != null && b.stops && b.stops.length)
      ? ' · '+T().stop+' '+(b.currentStopIndex+1)+'/'+b.stops.length : '';
    // routes are COLLAPSED by default (just the header) so they don't hoard the
    // map; the user expands the ones they care about.
    const expanded = expandedRoutes.has(key);

    const box = document.createElement('div');
    box.className = 'box' + (expanded ? '' : ' collapsed') + (b.predeparture ? ' sched' : '');
    // header (always visible) — click toggles expand
    const head = document.createElement('div');
    head.className = 'box-head';
    const whereTxt = b.predeparture
      ? T().departsAt + ' ' + nm(b,'boardStop')     // "departs from <stop>"
      : T().near + ' ' + nm(b,'location_stop') + ofN;
    head.innerHTML =
      '<span class="caret">' + (expanded ? '▾' : '▸') + '</span>'
      + '<span class="route" style="color:'+col+'">'+(nm(b,'route')||b.routeNumber||'?')+'</span>'
      + loopTag + depTag
      + '<div class="meta">'+etaText
      + (b.vehicleName ? ' · #'+b.vehicleName : '')
      + ' · '+whereTxt+'</div>';
    head.onclick = () => {
      if (expandedRoutes.has(key)) expandedRoutes.delete(key);
      else expandedRoutes.add(key);
      render(lastData);   // re-render with new expand state
    };
    // body (collapsible) — progress strip (+ locate button for running buses)
    const body = document.createElement('div');
    body.className = 'box-body';
    if (b.times) body.innerHTML = '<div class="times">'+b.times+'</div>';
    body.innerHTML += progressHtml(b);
    if (hasPos) {
      const locate = document.createElement('a');
      locate.className = 'locatebtn';
      locate.textContent = T().showOnMap;
      locate.onclick = (e) => { e.stopPropagation(); map.setView(ll, 16); markers[key].openPopup(); };
      body.appendChild(locate);
    }

    box.appendChild(head);
    box.appendChild(body);
    listEl.appendChild(box);
  });

  // remove markers + trails for buses no longer present
  Object.keys(markers).forEach(k => {
    if (!seen.has(k)) { map.removeLayer(markers[k]); delete markers[k]; }
  });
  Object.keys(trails).forEach(k => {
    if (!seen.has(k)) { map.removeLayer(trails[k].line); delete trails[k]; }
  });

  if (!buses.length) listEl.innerHTML = '<em>'+T().noBuses+'</em>';
  if (firstFit && bounds.length) { map.fitBounds(bounds, {padding:[60,60], maxZoom:15}); firstFit = false; }
  document.getElementById('status').textContent =
    T().updated + ' ' + new Date().toLocaleTimeString() + ' · ' + T().busesN(buses.length);
}

document.getElementById('lang-en').onclick = () => setLang('en');
document.getElementById('lang-ja').onclick = () => setLang('ja');
setLang(LANG);   // set initial active button
refresh();
setInterval(refresh, INTERVAL);
</script>
</body>
</html>
"""


# ---- the from/to search page shown at startup --------------------------------
SEARCH_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>bus-vision · choose stops</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 520px; margin: 40px auto;
         padding: 0 16px; color: #222; }
  h1 { font-size: 20px; }
  .sub { color: #666; font-size: 13px; margin-bottom: 20px; }
  .field { margin: 14px 0; position: relative; }
  label { display: block; font-size: 12px; color: #555; margin-bottom: 4px; }
  input { width: 100%; box-sizing: border-box; font-size: 15px; padding: 9px 10px;
          border: 1px solid #bbb; border-radius: 8px; }
  input.picked { border-color: #2e7d32; background: #f4faf4; }
  .sugg { position: absolute; z-index: 5; left: 0; right: 0; background: #fff;
          border: 1px solid #ccc; border-top: none; border-radius: 0 0 8px 8px;
          max-height: 260px; overflow-y: auto; display: none; }
  .sugg div { padding: 7px 10px; cursor: pointer; font-size: 14px; }
  .sugg div:hover, .sugg div.active { background: #e8f0fe; }
  .sugg .r { color: #888; font-size: 11px; margin-left: 6px; }
  button#go { margin-top: 18px; width: 100%; padding: 11px; font-size: 15px;
    background: #1976d2; color: #fff; border: none; border-radius: 8px; cursor: pointer; }
  button#go:disabled { background: #bbb; cursor: default; }
  .hint { font-size: 12px; color: #888; margin-top: 6px; }
  .swap { text-align: center; margin: 2px 0; }
  .swap button { background: none; border: none; color: #1976d2; cursor: pointer; font-size: 13px; }
</style>
</head>
<body>
<h1>Where are you going?</h1>
<div class="sub">Type a stop name — English (romaji), kana, or kanji all work.
  e.g. <code>yabashira</code>, <code>やばしら</code>, or <code>八柱</code>.</div>

<div class="field">
  <label>From / 乗車</label>
  <input id="from" autocomplete="off" placeholder="boarding stop" />
  <div class="sugg" id="from-sugg"></div>
</div>
<div class="swap"><button id="swap">&#8645; swap</button></div>
<div class="field">
  <label>To / 降車</label>
  <input id="to" autocomplete="off" placeholder="destination stop" />
  <div class="sugg" id="to-sugg"></div>
</div>

<button id="go" disabled>Show live buses →</button>
<div class="hint" id="hint"></div>

<script>
let STOPS = [];
const picked = { from: null, to: null };

fetch('/api/stops').then(r => r.json()).then(d => { STOPS = d.stops || []; });

function search(q) {
  q = q.trim().toLowerCase();
  if (!q) return [];
  const res = [];
  for (const s of STOPS) {
    if (s.name.includes(q) || s.kana.includes(q) || s.romaji.includes(q)) {
      // rank: prefix matches first
      const rank = (s.romaji.startsWith(q) || s.name.startsWith(q)
                    || s.kana.startsWith(q)) ? 0 : 1;
      res.push({ s, rank });
    }
    if (res.length > 60) break;
  }
  res.sort((a, b) => a.rank - b.rank);
  return res.slice(0, 25).map(x => x.s);
}

function wire(which) {
  const input = document.getElementById(which);
  const box = document.getElementById(which + '-sugg');
  let active = -1, items = [];

  function close() { box.style.display = 'none'; active = -1; }
  function show(list) {
    items = list;
    if (!list.length) { close(); return; }
    box.innerHTML = list.map((s, i) =>
      '<div data-i="'+i+'">'+s.name+'<span class="r">'+s.romaji+'</span></div>').join('');
    box.style.display = 'block';
    box.querySelectorAll('div').forEach(d => {
      d.onclick = () => choose(list[+d.dataset.i]);
    });
  }
  function choose(s) {
    picked[which] = s;
    input.value = s.name;
    input.classList.add('picked');
    close();
    updateGo();
  }
  input.addEventListener('input', () => {
    picked[which] = null; input.classList.remove('picked'); updateGo();
    show(search(input.value));
  });
  input.addEventListener('keydown', (e) => {
    if (box.style.display !== 'block') return;
    const divs = box.querySelectorAll('div');
    if (e.key === 'ArrowDown') { active = Math.min(active+1, items.length-1); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { active = Math.max(active-1, 0); e.preventDefault(); }
    else if (e.key === 'Enter') { if (active >= 0) { choose(items[active]); e.preventDefault(); } return; }
    else if (e.key === 'Escape') { close(); return; }
    divs.forEach((d, i) => d.classList.toggle('active', i === active));
    if (active >= 0) divs[active].scrollIntoView({block:'nearest'});
  });
  input.addEventListener('blur', () => setTimeout(close, 150));
}
wire('from'); wire('to');

document.getElementById('swap').onclick = () => {
  const a = picked.from, b = picked.to;
  picked.from = b; picked.to = a;
  const fi = document.getElementById('from'), ti = document.getElementById('to');
  fi.value = b ? b.name : ''; ti.value = a ? a.name : '';
  fi.classList.toggle('picked', !!b); ti.classList.toggle('picked', !!a);
  updateGo();
};

function updateGo() {
  const ok = picked.from && picked.to && picked.from.code !== picked.to.code;
  const go = document.getElementById('go');
  go.disabled = !ok;
  document.getElementById('hint').textContent =
    (picked.from && picked.to && picked.from.code === picked.to.code)
    ? 'From and To must be different stops.' : '';
}

document.getElementById('go').onclick = () => {
  if (!picked.from || !picked.to) return;
  const p = new URLSearchParams({
    from: picked.from.code, to: picked.to.code,
    fromName: picked.from.name, toName: picked.to.name,
    fromEn: picked.from.romaji || '', toEn: picked.to.romaji || '',
  });
  location.href = '/map?' + p.toString();
};
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    # class attrs set in main()
    bv = None
    stops_json = b"{}"        # embedded stop list, pre-serialized
    trackers = None           # (from,to) -> busvision.Tracker (one per journey)
    trackers_lock = None      # guards the trackers dict
    romanizer = None          # busvision.Romanizer (kanji -> romaji for EN UI)
    interval = 20
    linger = 60
    lang = "en"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/buses"):
            self._api()
        elif path == "/api/stops":
            self._send(200, self.stops_json, "application/json; charset=utf-8")
        elif path in ("/", "/index.html", "/search"):
            self._send(200, SEARCH_PAGE.encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/map":
            html = PAGE % {
                "interval": self.interval,
                "lat": 35.81, "lon": 139.94,   # generic center; page re-fits
                "lang": self.lang,
            }
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def _get_tracker(self, stop_from, stop_to):
        """One Tracker per (from,to) journey, created on demand and reused so
        sticky tracking state persists across polls."""
        key = (stop_from, stop_to)
        with self.trackers_lock:
            tr = self.trackers.get(key)
            if tr is None:
                tr = Tracker(self.bv, stop_from, stop_to,
                             linger_after_arrival=self.linger)
                self.trackers[key] = tr
            return tr

    def _api(self):
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            stop_from = (qs.get("from") or [None])[0]
            stop_to = (qs.get("to") or [None])[0]
            if not stop_from or not stop_to:
                self._send(400, b'{"error":"missing from/to"}',
                           "application/json"); return
            tracker = self._get_tracker(stop_from, stop_to)
            buses = tracker.poll()
            rom = self.romanizer.romanize   # kanji -> romaji for the EN UI
            out = []
            for b in buses:
                loc = b["location"] or {}   # pre-departure buses have no location
                stops = loc.get("route_stops", [])
                # attach a romaji name to each stop so EN mode can swap it
                stops_en = [{**s, "name_en": rom(s.get("name"))} for s in stops]
                # board/alight live on loc for running buses, on the run dict
                # itself for pre-departure ones
                board = loc.get("boardStop") or b.get("boardStop")
                alight = loc.get("alightStop") or b.get("alightStop")
                # current-location stop: real GPS stop, else the boarding stop
                cur = loc.get("currentStop") or (b.get("boardStop")
                                                 if b.get("predeparture") else None)
                out.append({
                    "routeNumber": b.get("routeNumber"),
                    "route": b.get("route"),
                    "route_en": rom(b.get("route")),
                    "destination": b.get("destination"),
                    "destination_en": rom(b.get("destination")),
                    "eta": b.get("eta"),
                    "delay": b.get("delay"),
                    "times": b.get("times"),
                    # numeric forms so the UI can render ETA/delay in either
                    # language (raw eta/delay strings above are site-language)
                    "etaMin": eta_minutes(b.get("eta")),
                    "delayMin": delay_minutes(b.get("delay")),
                    "approach": loc.get("approach"),
                    "planForecastResultCd": b.get("planForecastResultCd"),
                    "vehicleName": loc.get("vehicleName"),
                    "location_stop": cur,
                    "location_stop_en": rom(cur),
                    "currentStopIndex": loc.get("currentStopIndex"),
                    "lat": loc.get("lat"),
                    "lon": loc.get("lon"),
                    "stops": stops_en,
                    # your journey's own board/alight stops (may sit outside the
                    # route's stopName array, e.g. on loop lines)
                    "boardStop": board,
                    "boardStop_en": rom(board),
                    "boardTime": b.get("boardTime"),
                    "alightStop": alight,
                    "alightStop_en": rom(alight),
                    "isLoop": b.get("isLoop"),
                    # pre-departure: scheduled but not yet left origin (no GPS)
                    "predeparture": b.get("predeparture", False),
                    # true once the bus has passed the boarding stop (it has
                    # left approach.html but we keep tracking it to destination)
                    "departed": b.get("departed", False),
                    # true once it has reached the destination (about to be
                    # dropped after the linger window)
                    "arrived": b.get("arrived", False),
                })
            body = json.dumps({"buses": out}, ensure_ascii=False).encode("utf-8")
        except Exception as e:  # surface errors to the page
            body = json.dumps({"error": str(e)}).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")


def load_stops():
    """Load the embedded stop index (stops.json). Returns pre-serialized bytes."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stops.json")
    try:
        with open(path, "rb") as f:
            data = f.read()
        n = json.loads(data).get("count", "?")
        print(f"loaded {n} stops from stops.json", file=sys.stderr)
        return data
    except FileNotFoundError:
        print("warning: stops.json not found — run `python3 fetch_stops.py` "
              "first for search. Serving an empty list.", file=sys.stderr)
        return b'{"count":0,"stops":[]}'


def main():
    p = argparse.ArgumentParser(description="Live map of approaching buses.")
    # Cloud hosts (Render/Railway/etc.) inject $PORT and expect the app to bind
    # 0.0.0.0. We honour those env vars as defaults so no flags are needed there.
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                   help="bind address (use 0.0.0.0 when deployed)")
    p.add_argument("--interval", type=int, default=20,
                   help="browser refresh seconds")
    p.add_argument("--linger", type=int, default=60,
                   help="seconds to keep a bus on the map after it reaches "
                        "the destination")
    p.add_argument("--lang", choices=["en", "ja"],
                   default=os.environ.get("LANG_DEFAULT", "en"),
                   help="default UI language (toggle in-page anytime)")
    p.add_argument("--base", default=None, help="site base URL override")
    p.add_argument("--customer", default=None, help="customerCd override")
    p.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = p.parse_args()
    # never auto-open a browser when running headless in the cloud
    if os.environ.get("PORT"):
        args.no_open = True
        if args.host == "127.0.0.1":
            args.host = "0.0.0.0"

    kw = {}
    if args.base:
        kw["base"] = args.base
    if args.customer:
        kw["customer"] = args.customer

    Handler.bv = BusVision(**kw)
    Handler.stops_json = load_stops()
    Handler.romanizer = Romanizer()
    if not Handler.romanizer.available():
        print("note: pykakasi not installed — EN mode will show Japanese stop/"
              "route names. `pip install pykakasi` for romaji.", file=sys.stderr)
    Handler.trackers = {}
    Handler.trackers_lock = threading.Lock()
    Handler.interval = args.interval
    Handler.linger = args.linger
    Handler.lang = args.lang

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    shown_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    url = f"http://{shown_host}:{args.port}"
    print(f"serving bus-vision live view on {args.host}:{args.port}  ({url})")
    print("open it and choose your From / To stops")
    print("Ctrl-C to stop")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()

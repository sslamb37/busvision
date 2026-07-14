#!/usr/bin/env python3
"""
fetch_stops.py — harvest the full bus-vision.jp stop list and build a search
index (kanji + hiragana reading + romaji), embedded as stops.json.

The site's stop search matches on the *kana reading* of a stop name (typing
"やばしら" finds 八柱駅), but the API only returns code+label, not the reading.
So we:
  1. enumerate every stop by querying each hiragana syllable and merging by code
  2. derive a hiragana reading + Hepburn romaji for each name with pykakasi
  3. apply MANUAL_READINGS overrides for names pykakasi gets wrong (place-name
     readings are irregular, e.g. 八柱 = yabashira not yahashira)

Run once (or when the timetable revision changes):
    python3 fetch_stops.py            # writes stops.json
    python3 fetch_stops.py --check X  # print index entries matching X

Requires pykakasi (pip install pykakasi). The generated stops.json has NO
runtime dependency — liveview.py just loads it.
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

# the reading-correction table is shared with busvision.py so the harvester and
# the live romanizer stay consistent
from busvision import MANUAL_READINGS

BASE = "https://bus-vision.jp/skbus/view"
CUSTOMER = "1"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stops.json")

# Full hiragana syllabary incl. voiced/semi-voiced/small kana — every reading
# contains at least one of these, so querying them all surfaces every stop.
KANA = ("あいうえおかきくけこがぎぐげごさしすせそざじずぜぞたちつてとだぢづでど"
        "なにぬねのはひふへほばびぶべぼぱぴぷぺぽまみむめもやゆよらりるれろわをん")


def ajax_stop_search(text):
    data = urllib.parse.urlencode({
        "customerCd": CUSTOMER, "stopCdTo": "-1", "lang": "0",
        "component": "searchStopPage", "action": "ajaxGetStopFrom",
        "time": "x", "stopNmFrom": text,
    }).encode()
    req = urllib.request.Request(f"{BASE}/teeda.ajax", data=data, headers={
        "User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})
    raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    raw = raw.strip()
    if not raw:
        return []
    fixed = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', raw)
    return json.loads(fixed)


def harvest():
    stops = {}
    for i, c in enumerate(KANA):
        try:
            for s in ajax_stop_search(c):
                stops[str(s["value"])] = s["label"]
        except Exception as e:
            print(f"  warn: query {c!r} failed: {e}", file=sys.stderr)
        if (i + 1) % 10 == 0:
            print(f"  ...{i+1}/{len(KANA)} syllables, {len(stops)} stops so far",
                  file=sys.stderr)
    return stops


def build_index(stops):
    import pykakasi
    kks = pykakasi.kakasi()

    def convert(name):
        # apply manual reading overrides on the raw name where possible
        parts = kks.convert(name)
        hira = "".join(p["hira"] for p in parts)
        romaji = "".join(p["hepburn"] for p in parts)
        # fix known-bad readings by substring
        for kanji, reading in MANUAL_READINGS.items():
            if kanji in name:
                fixed_parts = kks.convert(name.replace(kanji, reading))
                hira = "".join(p["hira"] for p in fixed_parts)
                romaji = "".join(p["hepburn"] for p in fixed_parts)
                break
        return hira, romaji.lower()

    index = []
    for code, name in sorted(stops.items(), key=lambda kv: kv[1]):
        hira, romaji = convert(name)
        index.append({
            "code": code,
            "name": name,       # Japanese display name (from the site)
            "kana": hira,       # hiragana reading (for kana search)
            "romaji": romaji,   # Hepburn romaji (for alphabet search)
        })
    return index


def main():
    p = argparse.ArgumentParser(description="Harvest bus-vision stops -> stops.json")
    p.add_argument("--check", help="after building, print entries matching this")
    p.add_argument("--out", default=OUT)
    args = p.parse_args()

    print("Harvesting stop list (querying kana syllabary)...", file=sys.stderr)
    stops = harvest()
    print(f"Found {len(stops)} unique stops. Building romaji/kana index...",
          file=sys.stderr)
    index = build_index(stops)

    payload = {"customerCd": CUSTOMER, "count": len(index), "stops": index}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=0)
    print(f"Wrote {len(index)} stops -> {args.out}", file=sys.stderr)

    if args.check:
        q = args.check.lower()
        for s in index:
            if q in s["name"] or q in s["kana"] or q in s["romaji"]:
                print(f"  {s['code']}  {s['name']}  [{s['kana']} / {s['romaji']}]")


if __name__ == "__main__":
    main()

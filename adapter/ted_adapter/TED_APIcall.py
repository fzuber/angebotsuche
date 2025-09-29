#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TED Fulltext Search -> CSV (publication_number, date, title, link)

- Verwendet KEYWORDS aus config.py (Projekt-Root).
- Einheitliche CLI wie BKMS wo sinnvoll: --output, --days-back, --sleep, --debug, --preview(probe),
  sowie TED-spezifisch: --country, --page-limit, --max-pages, --max-items, --probe.
- Output überschreibt immer dieselbe Datei (kein Datum).
"""

import argparse
import csv
import os
import time
from datetime import date, timedelta
from typing import List, Optional
from pathlib import Path

# --- Projekt-Root für config.py verfügbar machen ---
import sys
from pathlib import Path as _Path
ROOT = _Path(__file__).resolve().parents[2]  # Projektwurzel
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # KEYWORDS aus Projekt-Root
import requests

API_URL = "https://api.ted.europa.eu/v3/notices/search"
UA = "Offerwatch-TED/2.0 (+https://ted.europa.eu)"

# KEYWORDS kommen aus config.py
KEYWORDS = list(config.KEYWORDS)

session = requests.Session()
session.headers.update({
    "User-Agent": UA,
    "Accept": "application/json",
    "Content-Type": "application/json",
})

def parse_args():
    ap = argparse.ArgumentParser(
        description="TED fulltext search -> CSV (publication_number, date, title, link)"
    )
    script_dir = Path(__file__).resolve().parent
    ap.add_argument("--output", type=Path, default=script_dir / "ted_ft_results.csv",
                    help="Zieldatei CSV (überschreibt)")
    ap.add_argument("--days-back", type=int, default=7,
                    help="Zeitraum in Tagen rückwärts")
    ap.add_argument("--country", type=str, default="",
                    help="ISO-3 Land (z. B. DEU) oder leer für alle")
    ap.add_argument("--page-limit", type=int, default=80,
                    help="Items pro Seite (API limit)")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="0 = unbegrenzt; sonst maximal so viele Seiten holen")
    ap.add_argument("--max-items", type=int, default=0,
                    help="0 = unbegrenzt; sonst nach N Items stoppen")
    ap.add_argument("--sleep", type=float, default=0.6,
                    help="Pause zwischen API-Calls (s)")
    ap.add_argument("--debug", action="store_true",
                    help="Detail-Logs pro Seite")
    ap.add_argument("--probe", action="store_true",
                    help="Nur ND zählen (keine Ausgabe-Datei erstellen)")
    # Alias für Konsistenz (BKMS hat --throttle)
    ap.add_argument("--throttle", type=float, help="(Alias) wie --sleep")
    return ap.parse_args()

def items_from(data: dict) -> List[dict]:
    return data.get("notices") or data.get("results") or []

def normalize_title(t, preferred=("DE", "EN", "MUL")) -> str:
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        for lang in preferred:
            v = t.get(lang)
            if isinstance(v, str) and v.strip():
                return v
        for v in t.values():
            if isinstance(v, str) and v.strip():
                return v
    return ""

def build_ft_query(days_back: int, country_3: Optional[str]) -> str:
    since = (date.today() - timedelta(days=days_back)).strftime("%Y%m%d")
    def qkw(s: str) -> str:
        s = s.strip()
        # einfache Escapes; Phrasen in Anführungszeichen
        if " " in s:
            s = s.replace('"', '\\"')
            return f"\"{s}\""
        return s
    ft_or = " OR ".join(qkw(k) for k in KEYWORDS)
    q = f"(FT = ({ft_or})) AND (PD >= {since})"
    if country_3:
        q += f" AND RC IN ({country_3})"
    return q

def api_post(body: dict, note: str, retries: int = 2, backoff: float = 1.0) -> Optional[dict]:
    for attempt in range(retries + 1):
        r = session.post(API_URL, json=body, timeout=45)
        head = (r.text or "")[:140].replace("\n", " ")
        print(f"{note}: {r.status_code} {r.reason} | {head}...")
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            try:
                wait = float(ra) if ra and ra.strip() else backoff
            except Exception:
                wait = backoff
            print(f"   429 rate limited, warte {wait:.1f}s")
            time.sleep(wait)
            backoff *= 1.8
            continue
        try:
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
            else:
                print(f"   Fehler: {e}")
                return None

def iter_ft(days_back: int,
            country_3: Optional[str],
            page_limit: int,
            max_pages: int,
            max_items: int,
            sleep_between_calls: float,
            debug: bool,
            probe: bool) -> List[dict]:
    query = build_ft_query(days_back, country_3)
    print("Query:", query)

    token = None
    page = 0
    total = 0
    all_items: List[dict] = []

    fields = ["ND"] if probe else ["ND", "notice-title", "publication-date", "links"]

    while True:
        page += 1
        body = {
            "query": query,
            "fields": fields,
            "limit": page_limit,
            "scope": "ALL",
            "paginationMode": "ITERATION",
            "checkQuerySyntax": False,
        }
        if token:
            body["iterationNextToken"] = token

        t0 = time.time()
        data = api_post(body, f"ITER p{page}")
        dt = time.time() - t0
        if not data:
            if debug:
                print(f"[DEBUG] Abbruch: keine Daten auf p{page} (Laufzeit {dt:.2f}s)")
            break

        items = items_from(data)
        n = len(items)
        total += n
        next_token = data.get("iterationNextToken")

        if debug:
            print(f"[DEBUG] Seite {page}: {n} Items, kumuliert {total}, token={'JA' if next_token else 'NEIN'}, req={dt:.2f}s")

        if not probe:
            all_items.extend(items)

        if max_items > 0 and total >= max_items:
            if debug:
                print(f"[DEBUG] Item-Limit erreicht ({max_items}).")
            if not probe:
                all_items = all_items[:max_items]
            break

        if not next_token or n == 0:
            if debug:
                print("[DEBUG] Ende der Iteration (kein token oder 0 Items).")
            break

        if max_pages > 0 and page >= max_pages:
            if debug:
                print(f"[DEBUG] Seitenlimit erreicht ({max_pages}).")
            break

        token = next_token
        time.sleep(sleep_between_calls)

    if probe:
        print(f"FT-Gesamt (gezählt): {total}")
        return []

    print(f"FT-Gesamt: {len(all_items)}")
    return all_items

def atomic_write_csv(rows: List[dict], out_path: Path) -> None:
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        else:
            w = csv.DictWriter(f, fieldnames=["publication_number","publication_date","title","link"])
            w.writeheader()
    os.replace(tmp, out_path)
    print(f"CSV gespeichert: {out_path}")

def main():
    args = parse_args()
    if args.throttle is not None:
        args.sleep = args.throttle

    items = iter_ft(
        days_back=args.days_back,
        country_3=(args.country if args.country and args.country.strip() else None),
        page_limit=args.page_limit,
        max_pages=args.max_pages,
        max_items=args.max_items,
        sleep_between_calls=args.sleep,
        debug=args.debug,
        probe=args.probe,
    )

    if args.probe:
        return

    seen = set()
    rows = []
    for it in items:
        nd = it.get("publication-number") or it.get("ND")
        if not nd or nd in seen:
            continue
        seen.add(nd)
        title_raw = it.get("notice-title") or it.get("title") or ""
        title = normalize_title(title_raw)
        rows.append({
            "publication_number": nd,
            "publication_date": it.get("publication-date", ""),
            "title": title,
            "link": f"https://ted.europa.eu/en/notice/-/detail/{nd}",
        })

    print(f"\nTreffer: {len(rows)}")
    for r in rows[:20]:
        title = r.get("title") or ""
        preview_title = str(title)[:120]
        print(f"- {r.get('publication_number','')} | {r.get('publication_date','')} | {preview_title}...")
        print(f"  {r.get('link','')}")

    atomic_write_csv(rows, args.output)

if __name__ == "__main__":
    main()

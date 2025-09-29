#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TED XML Extract -> TED-Schema CSV

- Lädt TED-Notice-XMLs aus ND-Liste (aus TED_APIcall.csv).
- Extrahiert Titel, Kurzbeschreibung, Buyer, Publikationsdatum, Deadline, Link, CPVs.
- Filter: CPV-Whitelist/Präfixe ODER Keyword-Treffer; Deadline in der Zukunft.
- Einheitliche CLI zu BKMS ergänzt: --timezone; Booleans als 'true'/'false'; Output überschreibt.
"""

import argparse
import csv
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Iterable
import threading
import concurrent.futures as cf

# --- Projekt-Root für config.py verfügbar machen ---
import sys
from pathlib import Path as _Path
ROOT = _Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # gemeinsame KEYWORDS/CPV
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import xml.etree.ElementTree as ET
from dateutil.tz import gettz

# TED-Namespaces
NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "efac": "http://data.europa.eu/p27/eforms-ubl-extension-aggregate-components/1",
    "efbc": "http://data.europa.eu/p27/eforms-ubl-extension-basic-components/1",
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
}

PREFERRED_LANGS = ("DEU", "ENG", "EN", "DE")

# --- Config aus Projekt-Root ---
KEYWORD_FALLBACK = list(config.KEYWORDS)
CPV_SET = config.CPV_SET
CPV_PREFIXES = config.CPV_PREFIXES

def shorten(text, maxlen=600):
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= maxlen else text[:maxlen].rstrip() + "…"

def clean_date(dt: str) -> str:
    if not dt:
        return ""
    return dt.split("T")[0].split("+")[0].strip()

def parse_iso_date(d: str):
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        return None

def parse_args():
    ap = argparse.ArgumentParser(description="TED XML Extract -> TED-Schema CSV")
    script_dir = Path(__file__).resolve().parent
    ap.add_argument("--input", type=Path, default=script_dir / "ted_ft_results.csv",
                    help="Pfad zur Eingabe-CSV (aus TED_APIcall.py)")
    ap.add_argument("--output", type=Path, default=script_dir / "ted_final.csv",
                    help="Pfad zur Ausgabe-CSV (überschreibt)")
    ap.add_argument("--workers", type=int, default=12,
                    help="Parallel-Threads für XML-Download/Parsing")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="HTTP Timeout je XML-Request (Sekunden)")
    ap.add_argument("--retries", type=int, default=4,
                    help="Max. Retries je Request (HTTPAdapter)")
    ap.add_argument("--backoff", type=float, default=0.8,
                    help="Backoff-Faktor für Retries (HTTPAdapter)")
    ap.add_argument("--timezone", type=str, default="Europe/Berlin",
                    help="Zeitzone für Deadline-Vergleich")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="Pause nach jedem XML (Sekunden); nur bei 429 nötig – Alias zu throttle")
    ap.add_argument("--debug", action="store_true",
                    help="Detail-Logs")
    ap.add_argument("--preview", action="store_true",
                    help="Erste 10 Zeilen ausgeben")
    # Alias für Kompatibilität
    ap.add_argument("--throttle", type=float, help="(Alias) wie --sleep")
    return ap.parse_args()

_tls = threading.local()

def make_session_xml(retries: int = 4, backoff: float = 0.8) -> requests.Session:
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=100, pool_maxsize=100)
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Offerwatch-TED-XML/2.0",
        "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
    })
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

def get_session_xml(retries: int, backoff: float) -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = make_session_xml(retries=retries, backoff=backoff)
        _tls.session = s
    return s

def pick_lang(elems, preferred=PREFERRED_LANGS):
    """Bevorzugt DE/EN."""
    found = {}
    for el in elems:
        lang = el.attrib.get("languageID") or el.attrib.get("{http://www.w3.org/XML/1998/namespace}lang")
        if el.text:
            found[lang] = el.text.strip()
    for lang in preferred:
        if lang in found:
            return found[lang]
    return ""

def extract_short_description(root):
    # bevorzugt DE
    for el in root.findall(".//cac:TenderingTerms/cbc:Description", NS):
        if el.attrib.get("languageID") == "DEU" and el.text:
            return shorten(el.text)
    for path in [".//cac:ProcurementProjectLot/cbc:Description", ".//cac:ProcurementProject/cbc:Description"]:
        for el in root.findall(path, NS):
            if el.attrib.get("languageID") == "DEU" and el.text:
                return shorten(el.text)
    # Fallback: EN
    for el in root.findall(".//cac:TenderingTerms/cbc:Description", NS):
        if (el.attrib.get("languageID") or "").upper().startswith("EN") and el.text:
            return shorten(el.text)
    for path in [".//cac:ProcurementProjectLot/cbc:Description", ".//cac:ProcurementProject/cbc:Description"]:
        for el in root.findall(path, NS):
            if (el.attrib.get("languageID") or "").upper().startswith("EN") and el.text:
                return shorten(el.text)
    return ""

def extract_buyer_name(root):
    buyers = root.findall(".//efac:Organization", NS)
    for b in buyers:
        name_el = b.find(".//cbc:Name", NS)
        if name_el is not None and name_el.text:
            return name_el.text.strip()
    return ""

def extract_cpvs(root: ET.Element):
    cpvs = set()
    for el in root.iter():
        t = el.tag.lower()
        if t.endswith("itemclassificationcode"):
            ln = (el.attrib.get("listName") or el.attrib.get("listname") or "").upper()
            txt = (el.text or "").strip()
            if txt and (not ln or ln == "CPV"):
                code = "".join(ch for ch in txt if ch.isdigit())
                if len(code) >= 8:
                    cpvs.add(code[:8])
        if t.endswith("cpv_code"):
            code = el.attrib.get("CODE") or el.attrib.get("code")
            if code:
                code = "".join(ch for ch in code if ch.isdigit())
                if len(code) >= 8:
                    cpvs.add(code[:8])
    return sorted(cpvs)

def pass_filter_simple(cpvs, title: str, short_descr: str):
    has_good_cpv = any(
        (c in CPV_SET) or any(c.startswith(p) for p in CPV_PREFIXES)
        for c in (cpvs or [])
    )
    text = f"{(title or '')} {(short_descr or '')}".lower()
    kw_match = any(k.lower() in text for k in KEYWORD_FALLBACK)
    ok = has_good_cpv or kw_match
    return ok, ("true" if has_good_cpv else "false"), ("true" if kw_match else "false")

def fetch_xml(pub_number: str, timeout: float, retries: int, backoff: float) -> Optional[ET.Element]:
    url = f"https://ted.europa.eu/en/notice/{pub_number}/xml"
    r = get_session_xml(retries=retries, backoff=backoff).get(url, timeout=timeout)
    r.raise_for_status()
    return ET.fromstring(r.content)

def process_notice(pub_number: str, timeout: float, retries: int, backoff: float, per_xml_sleep: float,
                   today_fn) -> Optional[dict]:
    try:
        root = fetch_xml(pub_number, timeout=timeout, retries=retries, backoff=backoff)
        if per_xml_sleep:
            time.sleep(per_xml_sleep)
        if root is None:
            return None
    except Exception:
        return None

    titles = root.findall(".//cbc:Name", NS)
    title = pick_lang(titles)
    if not title:
        return None

    short_descr = extract_short_description(root)
    buyer_name = extract_buyer_name(root)

    pub_date = clean_date((root.findtext(".//efbc:PublicationDate", default="", namespaces=NS) or "").strip())

    deadline_date = (root.findtext(".//cbc:EndDate", default="", namespaces=NS) or "").strip()
    deadline_time = (root.findtext(".//cbc:EndTime", default="", namespaces=NS) or "").strip()
    deadline = clean_date(f"{deadline_date} {deadline_time}".strip())

    if deadline:
        d = parse_iso_date(deadline)
        if d and d < today_fn():
            return None

    cpv_codes = extract_cpvs(root)
    ok, cpv_match, keyword_match = pass_filter_simple(cpv_codes, title, short_descr)
    if not ok:
        return None

    return {
        "title": title,
        "buyer_name": buyer_name,
        "short_description": short_descr,
        "publication_date": pub_date,
        "deadline": deadline,
        "link": f"https://ted.europa.eu/en/notice/-/detail/{pub_number}",
        "cpv_codes": ";".join(cpv_codes) if cpv_codes else "",
        "cpv_match": cpv_match,           # "true"/"false"
        "keyword_match": keyword_match,   # "true"/"false"
    }

def atomic_write_csv(rows, out_path: Path) -> None:
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    cols = ["title","buyer_name","short_description","publication_date","deadline",
            "link","cpv_codes","cpv_match","keyword_match"]
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, out_path)
    print(f"Fertig: {len(rows)} Einträge gespeichert in {out_path}")

def iter_pub_numbers(input_csv: Path) -> Iterable[str]:
    with input_csv.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            nd = row.get("publication_number")
            if nd:
                yield nd

def main():
    args = parse_args()
    if args.throttle is not None:
        args.sleep = args.throttle

    input_csv: Path = args.input.resolve()
    output_csv: Path = args.output.resolve()

    if not input_csv.exists():
        print(f"Eingabe-CSV {input_csv} nicht gefunden.")
        return

    pub_numbers = list(iter_pub_numbers(input_csv))
    total = len(pub_numbers)
    print(f"NDs: {total}")

    # lokale "heute"-Funktion mit Zeitzone
    tzname = args.timezone
    today_fn = (lambda: datetime.now(gettz(tzname)).date())

    results = []
    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = [
            ex.submit(process_notice, pn, args.timeout, args.retries, args.backoff, args.sleep, today_fn)
            for pn in pub_numbers
        ]
        for i, fut in enumerate(cf.as_completed(futures), 1):
            rec = fut.result()
            if rec:
                results.append(rec)
            if args.debug and i % 50 == 0:
                print(f"... verarbeitet: {i}/{total}")

    atomic_write_csv(results, output_csv)

    if args.preview and output_csv.exists():
        with output_csv.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                print(
                    f"- {row['title']}\n"
                    f"  {row['short_description']}\n"
                    f"  Veröffentlicht: {row['publication_date']} | Frist: {row['deadline']}\n"
                    f"  Käufer: {row['buyer_name']}\n"
                    f"  CPVs: {row['cpv_codes']} | cpv_match={row['cpv_match']} | keyword_match={row['keyword_match']}\n"
                    f"  Link: {row['link']}\n"
                )
                if i >= 9:
                    break

if __name__ == "__main__":
    main()

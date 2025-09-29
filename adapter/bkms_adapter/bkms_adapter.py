#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BKMS eForms -> TED-Schema CSV

- Lädt eForms-ZIPs der letzten N abgeschlossenen Tage (heute wird ausgelassen).
- Extrahiert Titel, Beschreibung, CPVs, Buyer, Publikationsdatum, Deadline, Link.
- Filter: Keyword-ODER (Titel+Beschreibung) ODER CPV-Whitelist/-Präfixe, Deadline in der Zukunft.
- Parallelisiert das XML-Parsen innerhalb einer ZIP.
- CLI-Parameter an TED angelehnt: --output, --days-back, --timezone, --timeout, --retries,
  --workers, --sleep, --debug, --preview. Aliases: --days (für --days-back), --throttle (für --sleep).
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from zipfile import ZipFile, BadZipFile

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dateutil.tz import gettz
from lxml import etree

# -------------------- Gemeinsame Config --------------------
import sys
from pathlib import Path

# Projekt-Root bestimmen (2 Ebenen über diesem Skript: adapter/bkms_adapter/bkms_adapter.py -> Projektroot)
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # lädt config.py aus dem Projekt-Root

KEYWORDS = config.KEYWORDS
CPV_SET = config.CPV_SET
CPV_PREFIXES = config.CPV_PREFIXES

# -------------------- Basis-Konfiguration --------------------

BASE_URL = "https://oeffentlichevergabe.de/api/notice-exports"
UA = "bkms-eforms-filter/5.0"
TIMEOUT_DEFAULT = 60.0
RETRY_DEFAULT = 3

URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
ISO_DATETIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2})(?::(\d{2}))?)?")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

log = logging.getLogger("bkms")

# -------------------- CLI --------------------

def parse_args():
    ap = argparse.ArgumentParser(description="BKMS eForms -> TED-Schema CSV (vereinheitlichte CLI)")
    script_dir = Path(__file__).resolve().parent

    # Einheitliche Flags (analog TED)
    ap.add_argument("--output", type=Path,
                    default=script_dir / "bkms_like_ted.csv",  # immer gleich, überschreibt alte Datei
                    help="Zieldatei (CSV im TED-Schema)")
    ap.add_argument("--days-back", type=int, default=7,
                    help="Zeitraum in Tagen rückwärts (BKMS lädt die letzten N abgeschlossenen Tage)")
    ap.add_argument("--timezone", type=str, default="Europe/Berlin",
                    help="Zeitzone für Tagesgrenzen")
    ap.add_argument("--timeout", type=float, default=TIMEOUT_DEFAULT,
                    help="HTTP-Timeout in Sekunden")
    ap.add_argument("--retries", type=int, default=RETRY_DEFAULT,
                    help="HTTP-Retries für ZIP-Downloads")
    ap.add_argument("--workers", type=int, default=8,
                    help="Parallel-Worker fürs XML-Parsen (pro ZIP)")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="Pause zwischen Tagen (Sekunden)")
    ap.add_argument("--debug", action="store_true",
                    help="Detail-Logs aktivieren")
    ap.add_argument("--preview", action="store_true",
                    help="Erste 10 Zeilen nach dem Schreiben ausgeben")

    # Aliases für Rückwärtskompatibilität
    ap.add_argument("--days", type=int,
                    help="(Alias) wie --days-back")
    ap.add_argument("--throttle", type=float,
                    help="(Alias) wie --sleep")

    return ap.parse_args()

# -------------------- Zeit / Tage --------------------

def today_local(tzname: str) -> date:
    tz = gettz(tzname)
    return datetime.now(tz).date()

def last_n_completed_days(n: int, tzname: str) -> List[date]:
    """Gestern bis gestern-(n-1)."""
    end = today_local(tzname) - timedelta(days=1)
    return [end - timedelta(days=i) for i in range(n)]

# -------------------- Keyword-Normalisierung --------------------

def norm(s: str) -> str:
    """Kleinbuchstaben + Diakritika entfernen (robuste Suche)."""
    if not isinstance(s, str):
        return ""
    s2 = s.casefold()
    s2 = unicodedata.normalize("NFKD", s2)
    return "".join(ch for ch in s2 if not unicodedata.combining(ch))

def build_kw_regex(keywords: Iterable[str]) -> re.Pattern:
    parts = []
    for k in keywords:
        nk = re.escape(norm(k))
        if nk:
            parts.append(r"(?<!\w)" + nk + r"(?!\w)")
    return re.compile("|".join(parts), re.DOTALL) if parts else re.compile(r"$a")

KW_RE = build_kw_regex(KEYWORDS)

# -------------------- HTTP (Session mit Retry/Pooling) --------------------

def make_session(timeout_connect: float = 10.0, retries: int = 3, backoff: float = 0.8) -> requests.Session:
    rconf = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=rconf, pool_connections=50, pool_maxsize=50)
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/zip, */*"})
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

SESSION = make_session()

def fetch_zip_bytes(url: str, timeout: float) -> Optional[bytes]:
    try:
        r = SESSION.get(url, timeout=timeout)
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.warning("Fehler beim Laden: %s (%s)", url, e)
        return None

# -------------------- XML-Helfer --------------------

def text_of(node) -> str:
    if node is None:
        return ""
    return " ".join(" ".join(node.itertext()).split())
# --- neue Helfer oben im Skript ergänzen ---

def first_lang(nodes, prefer=("DE", "DEU", "de", "en", "EN", "ENG")) -> Optional[str]:
    """Wählt den ersten Node in bevorzugter Sprache (xml:lang/languageID), sonst ersten nicht-leeren."""
    buckets = {}
    for n in nodes:
        lang = (n.get("{http://www.w3.org/XML/1998/namespace}lang")
                or n.get("languageID")
                or "").upper()
        txt = text_of(n)
        if not txt:
            continue
        buckets.setdefault(lang, []).append(txt)
    for p in prefer:
        if p.upper() in buckets and buckets[p.upper()]:
            return buckets[p.upper()][0]
    # Fallback: irgendein Text
    for arr in buckets.values():
        if arr:
            return arr[0]
    return None

def short_sentence_cut(s: str, maxlen: int = 600) -> str:
    """Kürzt an Satzende, wenn möglich (., !, ?) bis maxlen."""
    s = s.strip()
    if len(s) <= maxlen:
        return s
    cutoff = s.rfind(".", 0, maxlen)
    if cutoff < maxlen * 0.6:  # zu früh? dann harter Cut
        return s[:maxlen].rstrip() + "…"
    return s[:cutoff+1].strip()

# --- ersetze deine bisherige extract_title_and_desc komplett durch diese Version ---

def extract_title_and_desc(doc) -> Tuple[str, str]:
    # 1) Titel: bevorzugt Name des Hauptprojekts, dann Lot
    title_paths = (
        "//*[local-name()='ProcurementProject']/*[local-name()='Name']",
        "//*[local-name()='ProcurementProjectLot']//*[local-name()='ProcurementProject']/*[local-name()='Name']",
    )
    title_nodes = []
    for xp in title_paths:
        try:
            ns = doc.xpath(xp)
            if ns:
                title_nodes.extend(ns)
        except Exception:
            pass
    title = first_lang(title_nodes) or ""

    # 2) Beschreibung (BT-24 bevorzugt):
    #    a) Hauptprojekt-Description (BT-24)
    #    b) Lot-Description (BT-24 je Los)
    #    c) TenderingTerms/Description (sekundär, oft weniger präzise)
    #    Außerdem: ausschließen, wenn Vorfahr Award/Evaluation/Access etc. ist.
    exclude_ancestors = {
        "award", "awarding", "criterion", "criteria",
        "evaluation", "access", "document", "noticeurl", "externalreference"
    }

    def nodes_filtered(xp: str):
        try:
            nodes = doc.xpath(xp)
        except Exception:
            nodes = []
        good = []
        for n in nodes:
            ok = True
            a = n.getparent()
            # bis ganz oben: wenn ein Vorfahr "verboten" ist -> raus
            hops = 0
            while a is not None and hops < 6:  # 6 Ebenen reichen hier praktisch
                lname = (a.tag.split("}")[-1] if "}" in a.tag else a.tag).lower()
                if any(bad in lname for bad in exclude_ancestors):
                    ok = False
                    break
                a = a.getparent() if hasattr(a, "getparent") else None
                hops += 1
            if ok and text_of(n):
                good.append(n)
        return good

    desc_candidates = []
    # a) Hauptbeschreibung
    desc_candidates.extend(nodes_filtered(
        "//*[local-name()='ProcurementProject']/*[local-name()='Description']"
    ))
    # b) Lot-Beschreibungen (falls mehrere, nehmen wir die erste sprachpräferiert)
    desc_candidates.extend(nodes_filtered(
        "//*[local-name()='ProcurementProjectLot']//*[local-name()='ProcurementProject']/*[local-name()='Description']"
    ))
    # c) TenderingTerms sekundär
    if not desc_candidates:
        desc_candidates.extend(nodes_filtered(
            "//*[local-name()='TenderingTerms']/*[local-name()='Description']"
        ))

    desc = first_lang(desc_candidates) or ""
    # sanftes Kürzen an Satzende
    desc = short_sentence_cut(desc, maxlen=800)

    return title, desc


def extract_cpvs(doc) -> List[str]:
    codes = set()
    nodes = doc.xpath("//*[local-name()='ItemClassificationCode']")
    for n in nodes:
        raw = (n.text or "").strip() or n.get("value") or n.get("code") or ""
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 8:
            codes.add(digits[:8])
    return sorted(codes)

def extract_publication_date(doc) -> str:
    for xp in (
        "//*[local-name()='PublicationDate']",
        "//*[local-name()='IssueDate']",
        "//*[local-name()='DatePublished']",
    ):
        try:
            nodes = doc.xpath(xp)
        except Exception:
            nodes = []
        if nodes:
            m = ISO_DATETIME_RE.search(text_of(nodes[0]))
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            txt = text_of(nodes[0])[:10]
            if DATE_RE.match(txt):
                return txt
    return ""

def extract_buyer_name(doc) -> str:
    xpaths = [
        "//*[local-name()='ContractingParty']//*[local-name()='PartyName']/*[local-name()='Name']",
        "//*[local-name()='ContractingParty']//*[local-name()='Name']",
        "//*[local-name()='ProcuringEntity']//*[local-name()='PartyName']/*[local-name()='Name']",
        "//*[local-name()='BuyerCustomerParty']//*[local-name()='PartyName']/*[local-name()='Name']",
        "//*[contains(translate(local-name(),'BUYER','buyer'),'buyer')]//*[local-name()='Name']",
        "//*[local-name()='ContractingAuthority']//*[local-name()='Name']",
        "//*[local-name()='Party']/*[local-name()='PartyName']/*[local-name()='Name']",
        "//*[local-name()='PartyName']/*[local-name()='Name']",
    ]
    for xp in xpaths:
        try:
            nodes = doc.xpath(xp)
        except Exception:
            nodes = []
        if nodes:
            name = text_of(nodes[0]).strip()
            if name:
                return re.sub(r"\s+", " ", name)[:300]
    # heuristischer Fallback
    party_like = doc.xpath("//*[local-name()='Name']")
    for n in party_like:
        txt = text_of(n)
        if any(w in txt for w in ("Stadt ", "Gemeinde ", "Universität", "Landes", "Bundes", "Kreis ")):
            return re.sub(r"\s+", " ", txt)[:300]
    return ""

def _combine_date_only(date_txt: str) -> str:
    dm = ISO_DATETIME_RE.search(date_txt or "")
    if not dm:
        if DATE_RE.match((date_txt or "")[:10]):
            return (date_txt or "")[:10]
        return ""
    y, m, d = dm.group(1), dm.group(2), dm.group(3)
    return f"{y}-{m}-{d}"

def extract_deadline(doc) -> str:
    candidates = [
        "//*[local-name()='TenderingProcess']//*[local-name()='TenderSubmissionDeadlinePeriod']/*[local-name()='EndDate']",
        "//*[contains(translate(local-name(),'SUBMISSION','submission'),'submission') and "
        "contains(translate(local-name(),'DEADLINE','deadline'),'deadline')]"
        "//*[local-name()='EndDate' or local-name()='Date' or local-name()='DueDate' or local-name()='DateTime']",
        "//*[local-name()='SubmissionDeadline' or local-name()='DeadlineReceiptTenders' or "
        "local-name()='DueDate' or local-name()='EndDate' or local-name()='Date']",
    ]
    for xp in candidates:
        try:
            d_nodes = doc.xpath(xp)
        except Exception:
            d_nodes = []
        if not d_nodes:
            continue
        d_txt = text_of(d_nodes[0])
        iso = _combine_date_only(d_txt)
        if iso:
            return iso
    m = DATE_RE.search(text_of(doc))
    return m.group(0) if m else ""

def extract_link(doc) -> str:
    xpaths = [
        "//*[local-name()='AccessURL']/*[local-name()='URI']",
        "//*[local-name()='ExternalReference']/*[local-name()='URI']",
        "//*[local-name()='Attachment']/*[local-name()='ExternalReference']/*[local-name()='URI']",
        "//*[local-name()='URI' or local-name()='URL'][contains(normalize-space(text()),'http')]",
    ]
    for xp in xpaths:
        try:
            nodes = doc.xpath(xp)
        except Exception:
            nodes = []
        for n in nodes:
            u = (n.text or "").strip()
            if URL_RE.match(u):
                return u
    raw = etree.tostring(doc, encoding=str, with_tail=False)
    m = URL_RE.search(raw)
    return m.group(0) if m else ""

# -------------------- Filter --------------------

def parse_iso_date(d: str) -> Optional[date]:
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        return None

def keyword_match(title: str, desc: str) -> bool:
    txt = f"{title or ''} {desc or ''}"
    return bool(txt and KW_RE.search(norm(txt)))

def cpv_match(codes: Iterable[str]) -> bool:
    for c in codes:
        if c in CPV_SET or any(c.startswith(pref) for pref in CPV_PREFIXES):
            return True
    return False

# -------------------- Verarbeitung ZIP -> Rows --------------------

def process_xml_bytes(xml_bytes: bytes, today_fn) -> Optional[dict]:
    try:
        doc = etree.fromstring(xml_bytes)
    except Exception:
        return None

    title, desc = extract_title_and_desc(doc)
    cpvs = extract_cpvs(doc)
    pub_date = extract_publication_date(doc)
    buyer = extract_buyer_name(doc)
    deadline = extract_deadline(doc)

    # Deadlines in der Vergangenheit verwerfen (bezogen auf lokale "heute"-Funktion)
    if deadline:
        _d = parse_iso_date(deadline)
        if _d and _d < today_fn():
            return None

    k_hit = keyword_match(title, desc)
    c_hit = cpv_match(cpvs)
    if not (k_hit or c_hit):
        return None

    return {
        "title": (title or "")[:500],
        "buyer_name": (buyer or "")[:300],
        "short_description": (desc or "")[:1000],
        "publication_date": pub_date or "",
        "deadline": deadline or "",
        "link": extract_link(doc) or "",
        "cpv_codes": ";".join(cpvs),
        "cpv_match": str(bool(c_hit)).lower(),     # konsistent: "true"/"false"
        "keyword_match": str(bool(k_hit)).lower(), # konsistent: "true"/"false"
    }

def process_zip_bytes(zip_bytes: bytes, workers: int, today_fn) -> List[dict]:
    rows: List[dict] = []
    try:
        with ZipFile(BytesIO(zip_bytes)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            xml_blobs = []
            for n in names:
                try:
                    xml_blobs.append(zf.read(n))
                except Exception:
                    continue
        if not xml_blobs:
            return rows
        if workers <= 1:
            for b in xml_blobs:
                r = process_xml_bytes(b, today_fn)
                if r:
                    rows.append(r)
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(process_xml_bytes, b, today_fn) for b in xml_blobs]
                for f in as_completed(futs):
                    r = f.result()
                    if r:
                        rows.append(r)
    except BadZipFile:
        log.warning("Defekte ZIP-Datei")
    return rows

# -------------------- Schreiben --------------------

def atomic_write_csv(rows: List[dict], out_path: Path) -> None:
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    cols = [
        "title","buyer_name","short_description","publication_date","deadline",
        "link","cpv_codes","cpv_match","keyword_match"
    ]
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, out_path)
    log.info("CSV geschrieben: %s (Zeilen: %d)", out_path, len(rows))

# -------------------- Main --------------------

def main():
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s"
    )

    # Aliases abbilden
    if args.days is not None:
        args.days_back = args.days
    if args.throttle is not None:
        args.sleep = args.throttle

    # Tage bestimmen (gestern rückwärts)
    days = last_n_completed_days(args.days_back, args.timezone)
    log.info("Tage: %s", ", ".join(d.isoformat() for d in days))

    all_rows: List[dict] = []
    for d in days:
        url = f"{BASE_URL}?pubDay={d.isoformat()}&format=eforms.zip"
        log.debug("Hole: %s", url)
        b = fetch_zip_bytes(url, timeout=args.timeout)
        if not b:
            log.info("Kein Export für %s", d.isoformat())
            if args.sleep:
                time.sleep(args.sleep)
            continue

        # Lokale "heute"-Funktion mit Zeitzone, damit Deadline-Vergleich konsistent bleibt
        tzname = args.timezone
        today_fn = (lambda: datetime.now(gettz(tzname)).date())

        part = process_zip_bytes(b, workers=max(1, args.workers), today_fn=today_fn)
        log.info("%s: %d Treffer", d.isoformat(), len(part))
        all_rows.extend(part)

        if args.sleep:
            time.sleep(args.sleep)

    atomic_write_csv(all_rows, args.output)

    if args.preview and Path(args.output).exists():
        # Kurzer Blick auf die ersten 10 Zeilen
        with Path(args.output).open(newline="", encoding="utf-8") as f:
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
    try:
        main()
    except Exception as e:
        log.error("Abbruch: %s", e)
        sys.exit(1)

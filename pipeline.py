#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Pipeline für TED + BKMS (eForms)
"""

from __future__ import annotations
import sys
import subprocess
import os
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

# ---------- Pfade (robust relativ zum Skript) ----------
ROOT = Path(__file__).resolve().parent
TED_DIR = ROOT / "adapter" / "ted_adapter"
BKMS_DIR = ROOT / "adapter" / "bkms_adapter"
OUT_DIR = ROOT / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Zentrale Dateien
DATESTAMP = datetime.now().date().isoformat()
TED_FT = (TED_DIR / "ted_ft_results.csv").resolve()
TED_FINAL = (TED_DIR / "ted_final.csv").resolve()
BKMS_OUT = (BKMS_DIR / "bkms_like_ted.csv").resolve()
PROMPT_FILE = (ROOT / "review_prompt.txt").resolve()

# Einheitliches Zielschema (TED-Schema)
TED_SCHEMA = [
    "title","buyer_name","short_description","publication_date",
    "deadline","link","cpv_codes","cpv_match","keyword_match",
]

def run_py(script: Path, *args: str) -> None:
    script = script.resolve()
    if not script.exists():
        raise FileNotFoundError(f"Script fehlt: {script}")
    cmd = [sys.executable, str(script), *args]
    print(f"[RUN] {' '.join(cmd)}  (cwd={script.parent})")
    r = subprocess.run(cmd, cwd=script.parent)
    if r.returncode != 0:
        raise RuntimeError(f"Fehler beim Ausführen: {script.name} (Exit {r.returncode})")

def run_py_async(script: Path, *args: str) -> subprocess.Popen:
    script = script.resolve()
    if not script.exists():
        raise FileNotFoundError(f"Script fehlt: {script}")
    cmd = [sys.executable, str(script), *args]
    print(f"[RUN-ASYNC] {' '.join(cmd)}  (cwd={script.parent})")
    proc = subprocess.Popen(cmd, cwd=script.parent)
    return proc

def wait_ok(proc: subprocess.Popen, name: str) -> None:
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"Fehler beim Ausführen: {name} (Exit {rc})")
    print(f"[OK] abgeschlossen: {name}")

def find_latest_bkms_csv() -> Path:
    candidates = sorted(BKMS_DIR.glob("bkms_like_ted_*.csv"))
    if not candidates:
        raise FileNotFoundError("Keine BKMS-CSV gefunden (bkms_adapter/bkms_like_ted_*.csv).")
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    m = re.search(r"\d{4}-\d{2}-\d{2}", latest.name)
    print(f"[INFO] BKMS-Datei gewählt: {latest.name}" + (f" (Datum: {m.group(0)})" if m else ""))
    return latest.resolve()

def read_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in df.columns:
        df[col] = df[col].astype(str)
    return df

def ensure_schema(df: pd.DataFrame, source: str) -> pd.DataFrame:
    for col in TED_SCHEMA:
        if col not in df.columns:
            df[col] = "" if col not in ("cpv_match","keyword_match") else "False"
    def to_bool_series(s: pd.Series) -> pd.Series:
        return s.astype(str).str.strip().str.lower().isin(["true","1","yes","y"])
    df["cpv_match"] = to_bool_series(df["cpv_match"])
    df["keyword_match"] = to_bool_series(df["keyword_match"])
    df = df[TED_SCHEMA].copy()
    df["source"] = source
    return df

def dedupe(df: pd.DataFrame) -> pd.DataFrame:
    def norm(s: pd.Series) -> pd.Series:
        return s.fillna("").str.strip().str.casefold()
    link_series = df.get("link", pd.Series([""]*len(df)))
    key = (
        norm(df["title"]) + " | " + norm(df["buyer_name"]) + " | " +
        norm(df["publication_date"]) + " | " + norm(df["deadline"]) + " | " +
        norm(df["cpv_codes"]) + " | " + norm(link_series)
    )
    before = len(df)
    df = df.assign(_key=key).drop_duplicates(subset=["_key"]).drop(columns=["_key"])
    after = len(df)
    print(f"[INFO] Dedupe: {before} -> {after}")
    return df

def atomic_write_csv(df: pd.DataFrame, target: Path) -> None:
    tmp = target.with_suffix(target.suffix + ".tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(tmp, index=False)
    os.replace(tmp, target)
    print(f"[OK] geschrieben (atomisch): {target}")

def atomic_write_xlsx(df: pd.DataFrame, target: Path) -> None:
    tmp = target.with_suffix(target.suffix + ".tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(tmp, engine="xlsxwriter") as xw:
        df.to_excel(xw, index=False, sheet_name="tenders")
    os.replace(tmp, target)
    print(f"[OK] geschrieben (atomisch): {target}")

def write_review_txt(df: pd.DataFrame, target: Path, prompt_file: Path) -> None:
    """Schreibt eine Textdatei mit Prompt (aus externer Datei) + Funden für die manuelle Review."""
    tmp = target.with_suffix(target.suffix + ".tmp")

    # Prompt aus Datei laden
    try:
        prompt_text = prompt_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        prompt_text = "Prompt nicht gefunden, fallback auf: 'Bitte prüfe die folgenden Ausschreibungen und filtere die irrelevanten für ein Sozialforschungsunternehmen (Bereich Meinungsforschung) heraus.'"

    def clean(text: str, maxlen: int = 800) -> str:
        t = str(text or "").strip()
        t = re.sub(r"\s+", " ", t)
        return (t[:maxlen].rstrip() + "…") if len(t) > maxlen else t

    with tmp.open("w", encoding="utf-8") as f:
        f.write("PROMPT:\n")
        f.write(prompt_text + "\n\n")
        f.write("DATEN:\n")
        for i, row in df.iterrows():
            f.write(f"{i+1}. Titel: {row['title']}\n")
            f.write(f"   Kurzbeschreibung: {clean(row.get('short_description', ''))}\n")
            f.write(f"   Käufer: {row.get('buyer_name','')}\n")
            f.write(f"   Veröffentlicht: {row.get('publication_date','')} | Frist: {row.get('deadline','')}\n")
            f.write(f"   Link: {row.get('link','')}\n\n")

    os.replace(tmp, target)
    print(f"[OK] geschrieben (atomisch): {target}")

def main():
    bkms_proc = run_py_async(BKMS_DIR / "bkms_adapter.py")

    run_py(
        TED_DIR / "TED_APIcall.py",
        "--output", str(TED_FT),
        "--days-back", "7",
        "--country", "",
        "--page-limit", "50",
        "--max-pages", "0",
        "--sleep", "0.6",
    )

    run_py(
        TED_DIR / "ted_xml_extract.py",
        "--input", str(TED_FT),
        "--output", str(TED_FINAL),
    )

    wait_ok(bkms_proc, "bkms_adapter.py")
    try:
        bkms_csv = BKMS_OUT if BKMS_OUT.exists() else find_latest_bkms_csv()
    except FileNotFoundError:
        bkms_csv = find_latest_bkms_csv()

    print(f"[LOAD] TED:  {TED_FINAL}")
    ted_df = ensure_schema(read_csv(TED_FINAL), source="TED")

    print(f"[LOAD] BKMS: {bkms_csv}")
    bkms_df = ensure_schema(read_csv(bkms_csv), source="BKMS")

    combined = pd.concat([ted_df, bkms_df], ignore_index=True)
    combined = dedupe(combined)

    combined["publication_date_sort"] = pd.to_datetime(
        combined["publication_date"], errors="coerce"
    )
    combined = combined.sort_values(
        by=["publication_date_sort","source","title"],
        ascending=[False, True, True]
    ).drop(columns=["publication_date_sort"])

    out_csv = (OUT_DIR / "combined.csv").resolve()
    out_xlsx = (OUT_DIR / "combined.xlsx").resolve()
    out_txt = (OUT_DIR / "combined_for_review.txt").resolve()

    atomic_write_csv(combined, out_csv)
    atomic_write_xlsx(combined, out_xlsx)
    write_review_txt(combined, out_txt, PROMPT_FILE)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

# angebotsuche

------------------------------------------------------------
Angebotssuche – TED + BKMS Pipeline
------------------------------------------------------------

Dieses Projekt lädt wöchentlich Ausschreibungen von TED (EU-Tenders) und BKMS (eForms) herunter.
Die Daten werden dann mit Hilfe gemeinsamer Keywords und CPV-Codes gefiltert, in ein einheitliches Schema überführt und anschließend als CSV-, XLSX- und Textdatei gespeichert. 
Die Textdatei enthält zusätzlich zu den Treffern einen Prompt, sodass sie am Ende von einer KI (z. B. Copilot oder ChatGPT) genutzt werden kann, um eine letzte manuelle/strengere Auswahl der Ergebnisse vorzunehmen. 
Der verwendete Prompt kann jederzeit angepasst werden, ohne den Code ändern zu müssen. Er befindet sich unter: --> project/review_prompt.txt (siehe Abschnitt Konfiguration)

Für eine gezieltere Suche können außerdem die verwendeten Keywords und CPV-Codes in der Datei --> config.py angepasst werden (siehe Abschnitt Konfiguration).

------------------------------------------------------------
Projektstruktur
------------------------------------------------------------
projekt/
    config.py              zentrale Keywords & CPV-Codes
    pipeline.py            Haupt-Pipeline (führt alles zusammen)
    requirements.txt       Python-Abhängigkeiten
    review_prompt.txt      Prompt für finale Filterung durch z.B Copilot
    README.txt             diese Anleitung
    adapter/
        ted_adapter/
            TED_APIcall.py     Volltext-Suche bei TED API
            ted_xml_extract.py Detaildaten per XML, Filterung
        bkms_adapter/
            bkms_adapter.py    BKMS eForms-ZIP-Exporte
    output/
        combined.csv
        combined.xlsx
	combined_for_review.txt

------------------------------------------------------------
Installation
------------------------------------------------------------
1. Python 3.10 oder höher installieren
   Prüfen in PowerShell:
       py --version

2. Projekt klonen oder entpacken
       cd "C:\Pfad\zu\projekt"

3. Virtuelle Umgebung anlegen und aktivieren
       py -m venv .venv
       . .\.venv\Scripts\Activate.ps1

4. Abhängigkeiten installieren
       pip install -r requirements.txt

------------------------------------------------------------
Konfiguration
------------------------------------------------------------
Alle gemeinsamen Filter stehen in config.py im Projekt-Root:

- KEYWORDS: Liste relevanter Stichworte (Deutsch/Englisch)
- CPV_WHITELIST: zugelassene CPV-Codes
- CPV_SET, CPV_PREFIXES: automatisch abgeleitete Filter

Änderungen können jederzeit flexibel gemacht werden (z.B neue Keywords).
Alle Änderungen hier wirken sich sowohl auf TED als auch BKMS aus.

Der für den nachfolgenden Schritt (letzte Filterung mithilfe von KI-Tools) 
relevante Prompt kann ebenfalls beliebig verändert werden. 
Die entsprechende Textdatei befindet sich im Projekt-Root:

projekt/review_prompt.txt 

------------------------------------------------------------
Nutzung
------------------------------------------------------------
Pipeline manuell starten:(automatische monatliche Ausführung 
kann ggf. noch eingerichtet werden)

    . .\.venv\Scripts\Activate.ps1
    python pipeline.py

Die Pipeline startet BKMS und TED-Skripte, vereinheitlicht die Ergebnisse, 
entfernt Dubletten und schreibt:

    output/combined.csv
    output/combined.xlsx
    output/combined_for_review.txt

Für eine Weiterverarbeitung durch eine KI wie Copilot oder 
ChatGPT kann er Inhalt von letzterem als Gesamtes in den
Chat eingefügt werden. 

Einzelne Adapter ausführen (optional):

- TED API (Volltextsuche)
      python adapter/ted_adapter/TED_APIcall.py --days-back 7 --country DEU

- TED XML Extract
      python adapter/ted_adapter/ted_xml_extract.py --input ted_ft_results.csv --output ted_final.csv

- BKMS Adapter
      python adapter/bkms_adapter/bkms_adapter.py --days-back 7 --output bkms_like_ted.csv

Wichtige Parameter:
- --days-back N    Anzahl Tage rückwärts (z. B. 7 = letzte Woche)
- --country DEU    optional: nur ein Land
- --workers N      Anzahl paralleler Threads beim XML-Parsing
- --preview        nur Vorschau der ersten 10 Treffer

------------------------------------------------------------
Abhängigkeiten
------------------------------------------------------------
Die Datei requirements.txt enthält alle benötigten Pakete:

requests>=2.31
urllib3>=1.26
pandas>=2.2
lxml>=4.9
python-dateutil>=2.8
xlsxwriter>=3.1

Installation:
    pip install -r requirements.txt

------------------------------------------------------------
Hinweise
------------------------------------------------------------
- Filterung: Deadlines in der Vergangenheit werden ausgeschlossen.
- Sprache: Titel und Beschreibungen werden bevorzugt auf Deutsch, sonst Englisch genommen.

------------------------------------------------------------
mögliche Automatisierung
------------------------------------------------------------
Windows Task Scheduler:
1. Skript run_pipeline.ps1 im Projekt-Root anlegen:

   $proj = "C:\Pfad\zu\projekt"
   Set-Location $proj
   . .\.venv\Scripts\Activate.ps1
   python pipeline.py >> logs\pipeline_$(Get-Date -Format "yyyy-MM-dd_HH-mm-ss").log 2>&1

2. Geplante Aufgabe erstellen:
   schtasks /Create /TN "Offerwatch Pipeline Weekly" `
     /SC WEEKLY /D MON /ST 07:30 `
     /TR "powershell.exe -ExecutionPolicy Bypass -File \"C:\Pfad\zu\projekt\run_pipeline.ps1\"" `
     /RL HIGHEST /F

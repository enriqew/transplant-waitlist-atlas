"""Silver prebuild: Eurotransplant XLSX → long-format CSV.

Eurotransplant publishes 12 different waitlist reports as XLSX. Each has its
own layout shaped by the report's dimensions (country × year × organ for the
active waitlist, country × year per organ for registrations, removal-reason ×
country × year for removals).

For the MVP we parse only the *gold-standard* multi-year active waitlist
(`10728-33135` — "Active waiting list (at year-end) in All ET, by year, by
country, by organ"). It alone gives us the stock matrix for all 8 ET countries
across 10 years × 5 organs. The remaining 11 reports — per-organ registrations
and removals — will be added in follow-up commits; they require different
layouts and are not strictly required for the world map.

Output (`eurotransplant_long.csv`) — same shape as `optn_long.csv` plus a
`country_iso3` column so dbt staging can read both with one UNION ALL:

    metric_type,country_iso3,year,removal_reason,status,organ,patients
    stock,AUT,2016,,,kidney,587
    stock,BEL,2016,,,heart,117
    ...
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import openpyxl

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = REPO_ROOT / "data" / "raw" / "eurotransplant_waitlist"

ACTIVE_WAITLIST_FILE = "active_waitlist_by_year_by_country_by_organ__10728-33135.xlsx"

# Eurotransplant uses country *names* in the active waitlist file.
#
# Quirk: Luxembourg is an ET member (since 2012) but is NOT broken out in
# 10728-33135 — its patients are presumably rolled into the "All ET total"
# bottom row. Italy is non-ET but occasionally appears with cross-zone exchange
# cases (e.g., a single liver patient in 2024). Keeping ITA in the map so we
# capture those cases honestly.
COUNTRY_NAME_TO_ISO3: dict[str, str] = {
    "Austria": "AUT",
    "Belgium": "BEL",
    "Croatia": "HRV",
    "Germany": "DEU",
    "Hungary": "HUN",
    "Italy": "ITA",
    "Luxembourg": "LUX",
    "Netherlands": "NLD",
    "Slovenia": "SVN",
}

# Organ names in the file are lowercase singular; map straight to the canonical
# taxonomy used in optn_long.csv.
ORGAN_MAP: dict[str, str] = {
    "kidney": "kidney",
    "heart": "heart",
    "lung": "lung",
    "liver": "liver",
    "pancreas": "pancreas",
}


def _latest_snapshot_dir() -> Path:
    if not RAW_ROOT.exists():
        raise SystemExit(f"no Eurotransplant bronze snapshots under {RAW_ROOT}")
    candidates = sorted(p for p in RAW_ROOT.iterdir() if p.is_dir())
    if not candidates:
        raise SystemExit(f"no dated subdirectories under {RAW_ROOT}")
    # Walk newest-first; return first dir that has the required XLSX.
    for candidate in reversed(candidates):
        if (candidate / ACTIVE_WAITLIST_FILE).exists():
            return candidate
    raise SystemExit(
        f"active waitlist XLSX not found under any snapshot in {RAW_ROOT}; "
        "run `python -m ingest.eurotransplant_waitlist --force` to re-download"
    )


def _parse_active_waitlist(xlsx_path: Path, writer: csv.writer) -> int:
    """Parse the per-country blocks in the active waitlist XLSX.

    Layout: title at row 0, blank, then repeating 8-row blocks per country:
        row 0: country header — col[1]=country name, col[2:12]=year strings
        row 1..5: one row per organ — col[1]=organ, col[2:12]=counts
        row 6: "Total patients" sum (skipped — derivable)
        row 7: blank
    A final "All ET total patients" row appears once after all countries.

    Returns rows emitted to the writer.
    """

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))

    emitted = 0
    current_country: str | None = None
    current_years: list[int] = []
    for row in rows:
        if row is None:
            continue
        # Promote ints to int (openpyxl gives us a mix of int/str depending on
        # cell format). Years here are stored as strings — keep as is for the
        # country-header detection, cast to int when emitting.
        col0 = row[0]
        col1 = row[1] if len(row) > 1 else None

        # Country header: col[1] is a known country name and col[2..] look like
        # year strings. e.g. (None, 'Austria', '2016', '2017', ..., '2025', None).
        if isinstance(col1, str) and col1 in COUNTRY_NAME_TO_ISO3:
            tail = row[2:12]
            year_cells = [c for c in tail if c not in (None, "")]
            if all(_looks_like_year(c) for c in year_cells):
                current_country = col1
                current_years = [int(c) for c in tail if _looks_like_year(c)]
                continue

        # Organ row inside the current block.
        if current_country is None or not isinstance(col1, str):
            continue
        organ_key = col1.strip().lower()
        if organ_key == "total patients":
            current_country = None  # block boundary
            current_years = []
            continue
        organ_code = ORGAN_MAP.get(organ_key)
        if organ_code is None:
            continue

        iso3 = COUNTRY_NAME_TO_ISO3[current_country]
        for year, value in zip(current_years, row[2:2 + len(current_years)], strict=False):
            if value is None or value == "":
                continue
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count == 0:
                # 0 means "no patients" — keep as a real observation. (Some
                # countries report 0 for organs they don't do, but that's a
                # silver-layer concern, not parser.)
                pass
            writer.writerow(["stock", iso3, year, "", "", organ_code, count])
            emitted += 1

    return emitted


def _looks_like_year(value: object) -> bool:
    if isinstance(value, int):
        return 1900 <= value <= 2100
    if isinstance(value, str):
        stripped = value.strip()
        return len(stripped) == 4 and stripped.isdigit() and 1900 <= int(stripped) <= 2100
    return False


def main() -> int:
    snapshot = _latest_snapshot_dir()
    active_path = snapshot / ACTIVE_WAITLIST_FILE
    if not active_path.exists():
        print(f"ERROR: missing {active_path}", file=sys.stderr)
        return 1

    out_path = snapshot / "eurotransplant_long.csv"
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["metric_type", "country_iso3", "year", "removal_reason", "status", "organ", "patients"]
        )
        n = _parse_active_waitlist(active_path, writer)

    print(f"wrote {n:,} rows -> {out_path}")
    return 0 if n > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

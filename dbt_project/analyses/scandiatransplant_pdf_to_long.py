"""Silver prebuild: Scandiatransplant Q4 PDFs -> long-format CSV.

Each Q4 report (`sctp_figures_{year}_4Q.pdf`) contains "Waiting list statistics
January 1st, {year+1}" — the year-end snapshot for `year`.

The kidney section has an explicit country-level structure with subtotals:
  3 Danish centers  -> DNK subtotal  (position 3, 0-indexed)
  4 Swedish centers -> SWE subtotal  (position 8)
  Oslo              -> NOR           (position 9, single center = national total)
  Helsinki          -> FIN           (position 10)
  Tartu             -> EST           (position 11)
  SCDT total                         (position 12, skipped)

Both "Kidney active" and "Kidney on hold" rows follow this 13-value layout.
They are summed to produce the total waitlist stock per country.

Other organs (liver, heart, lung, pancreas) only have center-level data;
per-country extraction is unreliable due to multi-center patient listings.

Output schema:
    metric_type,country_iso3,year,organ,patients
    stock,DNK,2023,kidney,553
    stock,SWE,2023,kidney,563
    ...
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import pdfplumber

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = REPO_ROOT / "data" / "raw" / "scandiatransplant_waitlist"

# Column index (0-based, in the 13-value kidney row) for each country's total.
# Structure: [Aarhus, Odense, Copenhagen, DNK-total,
#             Skane, Goteborg, Uppsala, Stockholm, SWE-total,
#             Oslo, Helsinki, Tartu, SCDT-total]
COUNTRY_POSITIONS: dict[str, int] = {
    "DNK": 3,
    "SWE": 8,
    "NOR": 9,
    "FIN": 10,
    "EST": 11,
}
EXPECTED_COLS = 13

# Q4 filename pattern: sctp_figures_{year}_4Q.pdf
_FILENAME_RE = re.compile(r"sctp_figures_(\d{4})_4Q\.pdf", re.IGNORECASE)

# Kidney row labels (case-insensitive, with flexible internal whitespace).
_KIDNEY_ACTIVE_LABEL = "Kidney active"
_KIDNEY_ON_HOLD_LABEL = "Kidney on hold"

# Pages to search (0-indexed). Page 3 of the report = index 2.
_CANDIDATE_PAGES = [2, 1, 3, 4]


def _latest_snapshot_dir() -> Path:
    if not RAW_ROOT.exists():
        raise SystemExit(f"no Scandiatransplant bronze snapshots under {RAW_ROOT}")
    candidates = sorted(p for p in RAW_ROOT.iterdir() if p.is_dir())
    if not candidates:
        raise SystemExit(f"no dated subdirectories under {RAW_ROOT}")
    for candidate in reversed(candidates):
        if any(candidate.glob("sctp_figures_*_4Q.pdf")):
            return candidate
    raise SystemExit(
        f"no Scandiatransplant Q4 PDFs found under any snapshot in {RAW_ROOT}; "
        "run `python -m ingest.scandiatransplant_waitlist` to download"
    )


def _infer_year(filename: str) -> int | None:
    m = _FILENAME_RE.match(Path(filename).name)
    return int(m.group(1)) if m else None


def _parse_13_values(text: str, label: str) -> list[int] | None:
    """Extract exactly 13 integers following `label` in normalized text."""
    normalized = re.sub(r"\s+", " ", text)
    label_escaped = re.escape(label)
    pattern = rf"{label_escaped}\s+((?:[\d,]+\s+){{12}}[\d,]+)"
    m = re.search(pattern, normalized, re.IGNORECASE)
    if not m:
        return None
    tokens = m.group(1).strip().split()
    if len(tokens) != EXPECTED_COLS:
        return None
    try:
        return [int(t.replace(",", "")) for t in tokens]
    except ValueError:
        return None


def _extract_kidney_rows(pdf: pdfplumber.PDF) -> tuple[list[int] | None, list[int] | None]:
    """Return (active_values, on_hold_values) for the kidney section, or None if not found."""
    for page_idx in _CANDIDATE_PAGES:
        if page_idx >= len(pdf.pages):
            continue
        text = pdf.pages[page_idx].extract_text() or ""
        if "Kidney" not in text:
            continue
        active = _parse_13_values(text, _KIDNEY_ACTIVE_LABEL)
        on_hold = _parse_13_values(text, _KIDNEY_ON_HOLD_LABEL)
        if active is not None or on_hold is not None:
            return active, on_hold
    return None, None


def main() -> int:
    snapshot = _latest_snapshot_dir()
    pdfs = sorted(snapshot.glob("sctp_figures_*_4Q.pdf"))
    if not pdfs:
        print(f"ERROR: no Q4 PDFs found in {snapshot}", file=sys.stderr)
        return 1

    out_path = snapshot / "scandiatransplant_long.csv"
    total_rows = 0

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["metric_type", "country_iso3", "year", "organ", "patients"])

        for pdf_path in pdfs:
            year = _infer_year(pdf_path.name)
            if year is None:
                print(f"WARNING: cannot infer year from {pdf_path.name}", file=sys.stderr)
                continue

            try:
                with pdfplumber.open(pdf_path) as pdf:
                    active, on_hold = _extract_kidney_rows(pdf)
            except Exception as exc:  # noqa: BLE001
                print(f"WARNING: failed to parse {pdf_path.name}: {exc}", file=sys.stderr)
                continue

            if active is None and on_hold is None:
                print(
                    f"WARNING: kidney rows not found in {pdf_path.name} "
                    f"-- layout may have changed",
                    file=sys.stderr,
                )
                continue

            rows_this_file = 0
            for iso3, col_idx in COUNTRY_POSITIONS.items():
                total = 0
                if active is not None:
                    total += active[col_idx]
                if on_hold is not None:
                    total += on_hold[col_idx]
                if total == 0:
                    continue
                writer.writerow(["stock", iso3, year, "kidney", total])
                total_rows += 1
                rows_this_file += 1

            print(f"  {pdf_path.name} (year={year}): {rows_this_file} country row(s)")

    print(f"wrote {total_rows:,} rows -> {out_path}")
    return 0 if total_rows > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

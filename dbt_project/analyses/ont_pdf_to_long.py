"""Silver prebuild: ONT España organ-specific activity PDFs → long-format CSV.

Each PDF covers one organ for one reporting year (calendar year, 31 December
snapshot). The 2024 edition of each report contains a trend chart with
state-at-year-end breakdown for 2015-2024, giving 10 annual data points per PDF.

Two extraction strategies:
  • Heart / Liver / Lung: first "Activo N1 N2 … N10" row in the state-at-year-end
    chart — "Activo" = active patients (excludes temporally excluded).
  • Kidney: 10-integer series on the page that describes the year-end waiting list
    size ("lista de espera renal a final de año").  The renal report does not use
    the Activo/Excluido breakdown; the series is the total at year-end.

Output schema:
    metric_type,country_iso3,year,organ,patients
    stock,ESP,2024,kidney,4049
    stock,ESP,2024,heart,132
    ...

Year is the calendar year-end (31 December). All values are for Spain total (ESP).
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import pdfplumber

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = REPO_ROOT / "data" / "raw" / "ont_waitlist"

# Canonical organ names derived from the PDF filename suffix.
ORGAN_BY_KEYWORD: dict[str, str] = {
    "renal": "kidney",
    "cardiaco": "heart",
    "hepatico": "liver",
    "pulmonar": "lung",
    # English names used by our ingest script (ont-kidney-YYYY.pdf, etc.)
    "kidney": "kidney",
    "heart": "heart",
    "liver": "liver",
    "lung": "lung",
}

# Trend charts span exactly 10 years back from the reporting year.
# The 2024 PDF has series 2015-2024.
_SERIES_LENGTH = 10

# Regex for the "Activo" row in heart/liver/lung trend charts.
_ACTIVO_RE = re.compile(r"^Activo\s+((?:\d+\s+){%d}\d+)\s*$" % (_SERIES_LENGTH - 1), re.MULTILINE)

# Year-axis lines to discard when searching for renal series
# (all 10 values in the window [2000, 2040]).
_YEAR_AXIS_RE = re.compile(r"^(?:20[012]\d\s+){9}20[012]\d$")


def _latest_snapshot_dir() -> Path:
    if not RAW_ROOT.exists():
        raise SystemExit(f"no ONT bronze snapshots under {RAW_ROOT}")
    candidates = sorted(p for p in RAW_ROOT.iterdir() if p.is_dir())
    if not candidates:
        raise SystemExit(f"no dated subdirectories under {RAW_ROOT}")
    return candidates[-1]


def _detect_organ(filename: str) -> str | None:
    lower = filename.lower()
    for keyword, organ in ORGAN_BY_KEYWORD.items():
        if keyword in lower:
            return organ
    return None


def _reporting_year(filename: str) -> int | None:
    """Extract 4-digit year from filename (e.g. '…-2024.pdf' → 2024)."""
    m = re.search(r"(\d{4})", filename)
    return int(m.group(1)) if m else None


def _extract_activo_series(pdf: pdfplumber.PDF) -> list[int] | None:
    """Return the first 'Activo N1…N10' series found anywhere in the PDF."""
    for page in pdf.pages:
        text = page.extract_text() or ""
        m = _ACTIVO_RE.search(text)
        if m:
            return list(map(int, m.group(1).split()))
    return None


def _extract_renal_series(pdf: pdfplumber.PDF) -> list[int] | None:
    """Return the 10-value year-end renal waiting-list series.

    Targets the page with "lista de espera renal" and "final de año"; on that
    page, finds the first 10-integer line whose values are NOT a year axis
    (i.e. not all in 2000-2040) and are plausibly a kidney waitlist (>= 1000).
    """
    ten_int = re.compile(r"^(\d{3,5}(?:\s+\d{3,5}){%d})\s*$" % (_SERIES_LENGTH - 1))
    for page in pdf.pages:
        text = page.extract_text() or ""
        tl = text.lower()
        if "lista de espera renal" not in tl or "final de a" not in tl:
            continue
        for line in text.split("\n"):
            line = line.strip()
            m = ten_int.match(line)
            if not m:
                continue
            if _YEAR_AXIS_RE.match(line):
                continue
            nums = list(map(int, m.group(1).split()))
            if all(n >= 1000 for n in nums):
                return nums
    return None


def _series_to_rows(
    values: list[int],
    reporting_year: int,
    organ: str,
) -> list[tuple[str, str, int, str, int]]:
    """Map a 10-value series to (metric_type, country_iso3, year, organ, patients) tuples.

    The series runs from (reporting_year - 9) to reporting_year inclusive.
    """
    start_year = reporting_year - (_SERIES_LENGTH - 1)
    return [
        ("stock", "ESP", start_year + i, organ, v)
        for i, v in enumerate(values)
    ]


def main() -> int:
    snapshot = _latest_snapshot_dir()
    pdfs = sorted(snapshot.glob("*.pdf"))
    if not pdfs:
        print(f"ERROR: no PDFs found in {snapshot}", file=sys.stderr)
        return 1

    out_path = snapshot / "ont_long.csv"
    total_rows = 0

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["metric_type", "country_iso3", "year", "organ", "patients"])

        for pdf_path in pdfs:
            organ = _detect_organ(pdf_path.name)
            if organ is None:
                print(f"WARNING: cannot detect organ from {pdf_path.name}", file=sys.stderr)
                continue

            reporting_year = _reporting_year(pdf_path.name)
            if reporting_year is None:
                print(f"WARNING: cannot detect year from {pdf_path.name}", file=sys.stderr)
                continue

            try:
                with pdfplumber.open(pdf_path) as pdf:
                    if organ == "kidney":
                        values = _extract_renal_series(pdf)
                    else:
                        values = _extract_activo_series(pdf)
            except Exception as exc:  # noqa: BLE001
                print(f"WARNING: failed to parse {pdf_path.name}: {exc}", file=sys.stderr)
                continue

            if not values:
                print(f"WARNING: no waitlist series found in {pdf_path.name}", file=sys.stderr)
                continue

            rows = _series_to_rows(values, reporting_year, organ)
            for row in rows:
                writer.writerow(row)
                total_rows += 1

            print(f"  {pdf_path.name} (organ={organ}, year={reporting_year}): {len(rows)} year(s)")

    print(f"wrote {total_rows:,} rows -> {out_path}")
    return 0 if total_rows > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

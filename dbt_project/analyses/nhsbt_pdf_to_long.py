"""Silver prebuild: NHSBT activity report PDFs → long-format CSV.

Each annual PDF contains Table 2.1, the master summary of organ transplant
waitlist patients by UK nation. This parser extracts the TOTAL UK column for
each organ and emits nhsbt_long.csv.

Output schema matches the other *_long.csv files:

    metric_type,country_iso3,year,organ,patients
    stock,GBR,2025,kidney,6939
    stock,GBR,2025,liver,662
    ...

Year is the fiscal year END (31 March). For example, the 2024-2025 report
(snapshot at 31 March 2025) → year = 2025. Downstream silver adds a comment
so consumers understand the March vs. December reference date difference.

Table 2.1 layout (confirmed for 2024-2025; consistent since at least 2017):
    11 columns: [label, England N, Eng pmp, Wales N, Wal pmp,
                 Scotland N, Sco pmp, NI N, NI pmp, TOTAL N, TOTAL pmp]
    Organ groups: Kidney, Pancreas, Heart, Lung, Liver, Intestinal
    Within each group the "Transplant list" row holds the waitlist count.
    TOTAL N is column index 9.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import pdfplumber

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = REPO_ROOT / "data" / "raw" / "nhsbt_waitlist"

# Maps the organ label in Table 2.1 to the canonical organ code.
ORGAN_MAP: dict[str, str] = {
    "kidney": "kidney",
    "pancreas": "pancreas",
    "heart": "heart",
    "lung": "lung",
    "liver": "liver",
    "intestinal": "intestine",
}

# Column index (0-based) for UK TOTAL N in the 11-column Table 2.1.
TOTAL_COL = 9

# Matches "YYYY-YYYY" or "YYYY_YY" fiscal year patterns in filenames.
_FY_RE = re.compile(r"(\d{4})[-_](\d{2,4})")


def _latest_snapshot_dir() -> Path:
    if not RAW_ROOT.exists():
        raise SystemExit(f"no NHSBT bronze snapshots under {RAW_ROOT}")
    candidates = sorted(p for p in RAW_ROOT.iterdir() if p.is_dir())
    if not candidates:
        raise SystemExit(f"no dated subdirectories under {RAW_ROOT}")
    return candidates[-1]


def _fiscal_year_end(filename: str) -> int | None:
    """Extract the fiscal year-end integer from a PDF filename."""
    m = _FY_RE.search(filename)
    if not m:
        return None
    y2_raw = m.group(2)
    y2 = int(y2_raw) if len(y2_raw) == 4 else 2000 + int(y2_raw)
    return y2


def _find_table21_page(pdf: pdfplumber.PDF) -> pdfplumber.page.Page | None:
    """Find the page that contains Table 2.1 (all-organ waitlist summary).

    Strategy: look for a page whose text contains 'Table 2.1' or that has
    multiple 'Transplant list' occurrences alongside known organ names.
    Table extraction is only performed for text-matched candidate pages to
    avoid calling the expensive operation on every page; this also guards
    against intro/TOC pages that mention 'Table 2.1' in text but hold no
    actual extractable table.
    """
    for page in pdf.pages:
        text = page.extract_text() or ""
        has_table21 = "Table 2.1" in text
        if not has_table21:
            organ_hits = sum(1 for o in ORGAN_MAP if o.capitalize() in text)
            is_fallback = organ_hits >= 4 and "Transplant list" in text
        else:
            is_fallback = False
        if not (has_table21 or is_fallback):
            continue
        if page.extract_tables():
            return page
    return None


def _parse_table21(page: pdfplumber.page.Page) -> list[tuple[str, int]]:
    """Parse Table 2.1 from the page. Returns [(organ_code, patients), ...]."""
    results: list[tuple[str, int]] = []
    current_organ: str | None = None

    for table in page.extract_tables():
        ncols = len(table[0]) if table else 0
        # 2018+ format (11 cols): TOTAL N is the 10th column (index 9).
        # Pre-2018 format (9 cols): no TOTAL column — sum the four country N
        # columns (England=1, Wales=3, Scotland=5, N.Ireland=7).
        if ncols >= 11:
            total_col: int | None = TOTAL_COL
            sum_cols: list[int] | None = None
        elif ncols >= 9:
            total_col = None
            sum_cols = [1, 3, 5, 7]
        else:
            continue  # Too few columns; not Table 2.1

        for row in table:
            if not row or row[0] is None:
                continue
            label = str(row[0]).strip().rstrip("0123456789").strip()

            # Organ group header (e.g. "Kidney", "Pancreas")
            organ_code = ORGAN_MAP.get(label.lower())
            if organ_code is not None:
                current_organ = organ_code
                continue

            # "Total" row marks the end of organ-specific sections (skip it)
            if label.lower().startswith("total"):
                current_organ = None
                continue

            # "Transplant list" row inside the current organ group
            if current_organ and label.lower().startswith("transplant list"):
                try:
                    if total_col is not None:
                        patients = int(str(row[total_col]).replace(",", "").strip())
                    else:
                        patients = sum(
                            int(str(row[c]).replace(",", "").strip())
                            for c in sum_cols  # type: ignore[union-attr]
                            if row[c] is not None and str(row[c]).strip() not in ("", "-")
                        )
                    results.append((current_organ, patients))
                except (TypeError, ValueError, IndexError):
                    pass

    return results


def main() -> int:
    snapshot = _latest_snapshot_dir()
    pdfs = sorted(snapshot.glob("*.pdf"))
    if not pdfs:
        print(f"ERROR: no PDFs found in {snapshot}", file=sys.stderr)
        return 1

    out_path = snapshot / "nhsbt_long.csv"
    total_rows = 0

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["metric_type", "country_iso3", "year", "organ", "patients"])

        for pdf_path in pdfs:
            year_end = _fiscal_year_end(pdf_path.name)
            if year_end is None:
                print(f"WARNING: cannot infer fiscal year from {pdf_path.name}", file=sys.stderr)
                continue

            try:
                with pdfplumber.open(pdf_path) as pdf:
                    page = _find_table21_page(pdf)
                    if page is None:
                        print(f"WARNING: Table 2.1 not found in {pdf_path.name}", file=sys.stderr)
                        continue
                    rows = _parse_table21(page)
            except Exception as exc:  # noqa: BLE001
                print(f"WARNING: failed to parse {pdf_path.name}: {exc}", file=sys.stderr)
                continue

            if not rows:
                print(f"WARNING: no waitlist rows extracted from {pdf_path.name}", file=sys.stderr)
                continue

            for organ_code, patients in rows:
                writer.writerow(["stock", "GBR", year_end, organ_code, patients])
                total_rows += 1

            print(f"  {pdf_path.name} (year_end={year_end}): {len(rows)} organ(s)")

    print(f"wrote {total_rows:,} rows -> {out_path}")
    return 0 if total_rows > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

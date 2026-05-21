"""Silver prebuild: OPTN pivot CSVs → single long-format CSV.

OPTN's Build Advanced tool exports pivot tables with hierarchical row labels
(year, removal-reason, status) and one column per organ. dbt/DuckDB cannot
unstack that directly, so this prebuild flattens all three OPTN CSVs into one
tall `optn_long.csv` inside the latest snapshot dir.

Output schema:
    metric_type      stock | additions | removals
    year             integer (blank for snapshot rows)
    removal_reason   string (blank except for removals)
    status           string ("Liver: Status 1A", "Heart: Adult Status 4", ...)
    organ            canonical code: kidney, liver, heart, lung, pancreas,
                     kidney_pancreas, heart_lung, intestine, vca
    patients         integer

Aggregate rows ("All Organs", "All Types", "All Removal Reason", "To Date")
are dropped — they are derivable from the long output and including them would
double-count in dbt staging.

Failure modes (each exits non-zero — never fabricate):
    - The expected snapshot dir is missing.
    - Any of the three OPTN CSVs is absent or has the wrong column count.
    - An organ column name is not in ORGAN_MAP (signals OPTN added a new organ).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = REPO_ROOT / "data" / "raw" / "optn_waitlist"

SNAPSHOT_FILE = "Waitlist___Organ_by_Waiting_List_Status.csv"
ADDITIONS_FILE = (
    "Waitlist_Additions___Organ_by_List_Year,_Waiting_List_Status_at_Listing.csv"
)
REMOVALS_FILE = (
    "Waitlist_Removals___Organ_by_Removal_Year,_Removal_Reason,"
    "_Waiting_List_Status_at_Removal.csv"
)

# Maps OPTN column header text to the canonical organ taxonomy used across all
# three sources (CENATRA / Eurotransplant / OPTN). Multiple OPTN columns can
# collapse onto one code (six VCA columns → "vca").
ORGAN_MAP: dict[str, str] = {
    "Kidney": "kidney",
    "Liver": "liver",
    "Pancreas": "pancreas",
    "Kidney / Pancreas": "kidney_pancreas",
    "Heart": "heart",
    "Lung": "lung",
    "Heart / Lung": "heart_lung",
    "Intestine": "intestine",
    "VCA - abdominal wall": "vca",
    "VCA - external male genitalia": "vca",
    "VCA - head and neck": "vca",
    "VCA - other genitourinary organ": "vca",
    "VCA - upper limb": "vca",
    "VCA - uterus": "vca",
}

# Row-label prefixes (column 0 or 1) that mean "this is an aggregate, skip it".
# OPTN uses "All Types" for the per-year subtotal, "All Removal Reason" for the
# per-year subtotal across reasons, and "To Date" for the cumulative since 1988.
AGGREGATE_PREFIXES = ("All Types", "All Removal Reason", "All Statuses")
TOTAL_YEAR_LABEL = "To Date"


def _parse_count(value: str) -> int | None:
    """Lenient int parse: handles thousands separators and rejects '-' / empty."""

    cleaned = value.replace(",", "").replace(" ", "").strip()
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _latest_snapshot_dir() -> Path:
    if not RAW_ROOT.exists():
        raise SystemExit(f"no OPTN bronze snapshots under {RAW_ROOT}")
    candidates = sorted(p for p in RAW_ROOT.iterdir() if p.is_dir())
    if not candidates:
        raise SystemExit(f"no dated subdirectories under {RAW_ROOT}")
    # Walk newest-first; return first dir that has all three required CSVs.
    for candidate in reversed(candidates):
        if all((candidate / f).exists() for f in (SNAPSHOT_FILE, ADDITIONS_FILE, REMOVALS_FILE)):
            return candidate
    raise SystemExit(
        f"OPTN raw CSVs not found under any snapshot in {RAW_ROOT}; "
        "download from https://optn.transplant.hrsa.gov/data/view-data-reports/build-advanced/ "
        "and run `python -m ingest.optn_waitlist` to register them"
    )


def _validate_organ_columns(header_organs: list[str], context: str) -> list[str]:
    """Map each header organ to its canonical code; fail loud on unknown columns."""

    canonical: list[str] = []
    unknown: list[str] = []
    for organ_name in header_organs:
        if organ_name == "All Organs":
            canonical.append("__skip__")
            continue
        code = ORGAN_MAP.get(organ_name)
        if code is None:
            unknown.append(organ_name)
            canonical.append("__skip__")
        else:
            canonical.append(code)
    if unknown:
        raise SystemExit(
            f"unknown OPTN organ column(s) in {context}: {unknown!r} — extend ORGAN_MAP"
        )
    return canonical


def _emit_organ_cells(
    writer: csv.writer,
    *,
    metric_type: str,
    year: str,
    removal_reason: str,
    status: str,
    cells: list[str],
    organ_codes: list[str],
) -> int:
    """Emit one long-format row per non-zero organ cell. Returns rows emitted."""

    n = 0
    for code, raw in zip(organ_codes, cells, strict=False):
        if code == "__skip__":
            continue
        count = _parse_count(raw)
        if count is None or count == 0:
            continue
        writer.writerow([metric_type, year, removal_reason, status, code, count])
        n += 1
    return n


def _parse_snapshot(path: Path, writer: csv.writer) -> int:
    """File 1: 2 leading cols (blank, status), then 13 organ columns starting at col 2."""

    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise SystemExit(f"empty file: {path.name}")
    header = rows[0]
    if len(header) != 15:
        raise SystemExit(
            f"unexpected column count in {path.name}: got {len(header)}, expected 15"
        )
    organ_codes = _validate_organ_columns(header[2:], path.name)
    emitted = 0
    for row in rows[1:]:
        if len(row) < 15:
            continue
        status = row[0].strip() or row[1].strip()
        if not status or status.startswith(AGGREGATE_PREFIXES) or status == "All Types":
            continue
        emitted += _emit_organ_cells(
            writer,
            metric_type="stock",
            year="",
            removal_reason="",
            status=status,
            cells=row[2:15],
            organ_codes=organ_codes,
        )
    return emitted


def _parse_additions(path: Path, writer: csv.writer) -> int:
    """File 2: 3 leading cols (year, status, blank), then 15 organ columns starting at col 3."""

    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise SystemExit(f"empty file: {path.name}")
    header = rows[0]
    if len(header) != 18:
        raise SystemExit(
            f"unexpected column count in {path.name}: got {len(header)}, expected 18"
        )
    organ_codes = _validate_organ_columns(header[3:], path.name)
    current_year = ""
    emitted = 0
    for row in rows[1:]:
        if len(row) < 18:
            # Skip stray short rows. OPTN's CSV occasionally has a blank line
            # mid-file when crossing year boundaries.
            continue
        # Column 0: year (forward-filled when blank)
        year_cell = row[0].strip()
        if year_cell:
            current_year = year_cell
        if current_year == TOTAL_YEAR_LABEL:
            continue
        status = row[1].strip()
        if not status or status.startswith(AGGREGATE_PREFIXES):
            continue
        emitted += _emit_organ_cells(
            writer,
            metric_type="additions",
            year=current_year,
            removal_reason="",
            status=status,
            cells=row[3:18],
            organ_codes=organ_codes,
        )
    return emitted


def _parse_removals(path: Path, writer: csv.writer) -> int:
    """File 3: 4 leading cols (year, removal_reason, status, blank), then 15 organs at col 4."""

    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise SystemExit(f"empty file: {path.name}")
    header = rows[0]
    if len(header) != 19:
        raise SystemExit(
            f"unexpected column count in {path.name}: got {len(header)}, expected 19"
        )
    organ_codes = _validate_organ_columns(header[4:], path.name)
    current_year = ""
    current_reason = ""
    emitted = 0
    for row in rows[1:]:
        if len(row) < 19:
            continue
        year_cell = row[0].strip()
        if year_cell:
            current_year = year_cell
            current_reason = ""  # year change resets reason context
        if current_year == TOTAL_YEAR_LABEL:
            continue
        reason_cell = row[1].strip()
        if reason_cell:
            current_reason = reason_cell
        status = row[2].strip()
        if not status or status.startswith(AGGREGATE_PREFIXES):
            continue
        if not current_reason or current_reason.startswith(AGGREGATE_PREFIXES):
            continue
        emitted += _emit_organ_cells(
            writer,
            metric_type="removals",
            year=current_year,
            removal_reason=current_reason,
            status=status,
            cells=row[4:19],
            organ_codes=organ_codes,
        )
    return emitted


def main() -> int:
    snapshot = _latest_snapshot_dir()
    expected = [snapshot / SNAPSHOT_FILE, snapshot / ADDITIONS_FILE, snapshot / REMOVALS_FILE]
    missing = [p.name for p in expected if not p.exists()]
    if missing:
        print(f"ERROR: missing OPTN files in {snapshot}: {missing}", file=sys.stderr)
        return 1

    out_path = snapshot / "optn_long.csv"
    counts = {"stock": 0, "additions": 0, "removals": 0}
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["metric_type", "year", "removal_reason", "status", "organ", "patients"])
        counts["stock"] = _parse_snapshot(snapshot / SNAPSHOT_FILE, writer)
        counts["additions"] = _parse_additions(snapshot / ADDITIONS_FILE, writer)
        counts["removals"] = _parse_removals(snapshot / REMOVALS_FILE, writer)

    total = sum(counts.values())
    print(f"wrote {total:,} rows → {out_path}")
    print(f"  stock={counts['stock']:,}  additions={counts['additions']:,}  removals={counts['removals']:,}")
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

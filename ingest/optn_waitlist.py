"""Bronze ingest: OPTN/UNOS waiting-list reports (United States).

OPTN ("Organ Procurement and Transplantation Network", operated by HRSA / UNOS)
exposes a public report builder at:

    https://optn.transplant.hrsa.gov/data/view-data-reports/build-advanced/

That tool returns CSV downloads, but it is an interactive form rendered through
JavaScript — there is no stable HTTP endpoint we can hit from a script. So:

    *** OPTN bronze is a MANUAL-SNAPSHOT ingest. ***

The operator downloads the three CSVs from the Build Advanced UI and drops them
into data/raw/optn_waitlist/<YYYY-MM-DD>/. This module then:

    1. Verifies all three expected files are present.
    2. Computes SHA256, byte count, and row count for each.
    3. Records the original Build Advanced query that produced each CSV in
       meta.json (so a future operator can reproduce the snapshot).
    4. Records `data_as_of`, the date OPTN reported the data was current, which
       is *distinct* from `fetched_at` (when the operator downloaded it).

The three expected CSVs:

  - **Waitlist___Organ_by_Waiting_List_Status.csv**
        Current waitlist snapshot (today). Pivot of patient counts by
        waiting-list status × organ. No year axis — OPTN does not publish a
        year-axis version of this report.

  - **Waitlist_Additions___Organ_by_List_Year,_Waiting_List_Status_at_Listing.csv**
        Historical additions (people entering the US waitlist) by list-year ×
        status-at-listing × organ. Goes back to ~1988.

  - **Waitlist_Removals___Organ_by_Removal_Year,_Removal_Reason,_Waiting_List_Status_at_Removal.csv**
        Historical removals by removal-year × removal-reason × status × organ.
        Removal reasons include Deceased Donor Transplant, Living Donor Transplant,
        Died, Too Sick to Transplant, Condition Improved, Refused, Transferred,
        etc. This is the dataset that lets us compute the *fate distribution*
        for any cohort.

License: OPTN data is public. Attribution required: "Based on OPTN data as of
<data_as_of>". Recorded in meta.json.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from ingest._common import (
    FileRecord,
    SnapshotComplete,
    SnapshotMeta,
    build_argparser,
    configure_logging,
    count_csv_rows,
    ensure_snapshot_dir,
    fail,
    pipeline_version,
    sha256_of,
    snapshot_dir,
    utcnow_iso,
    write_meta,
)

SOURCE = "optn_waitlist"
BUILD_ADVANCED_URL = "https://optn.transplant.hrsa.gov/data/view-data-reports/build-advanced/"


@dataclass(frozen=True)
class ExpectedFile:
    """One CSV the operator is expected to have downloaded from Build Advanced."""

    filename: str
    description: str
    build_query: str  # human-readable summary of the Build Advanced configuration


EXPECTED: list[ExpectedFile] = [
    ExpectedFile(
        filename="Waitlist___Organ_by_Waiting_List_Status.csv",
        description="Current waitlist snapshot: counts by status × organ. No year axis.",
        build_query=(
            "Category=Waiting List; Output=Candidates on the OPTN Waiting List; "
            "Columns=Organ (19 items); Rows=Waiting List Status (70 items); "
            "Measure=Candidates"
        ),
    ),
    ExpectedFile(
        filename="Waitlist_Additions___Organ_by_List_Year,_Waiting_List_Status_at_Listing.csv",
        description=(
            "Historical waitlist additions by list-year × status-at-listing × organ. "
            "Multi-year ~1988-present."
        ),
        build_query=(
            "Category=Waiting List Additions; Columns=Organ; "
            "Rows=List Year, Waiting List Status at Listing; Measure=Candidates"
        ),
    ),
    ExpectedFile(
        filename="Waitlist_Removals___Organ_by_Removal_Year,_Removal_Reason,_Waiting_List_Status_at_Removal.csv",
        description=(
            "Historical waitlist removals by removal-year × removal-reason × "
            "status-at-removal × organ. Source for *fate distribution* (transplanted "
            "vs died vs other) — the most valuable OPTN dataset for cross-source "
            "comparison against Eurotransplant removal-reason reports."
        ),
        build_query=(
            "Category=Waiting List Removals; Columns=Organ; "
            "Rows=Removal Year, Removal Reason, Waiting List Status at Removal; "
            "Measure=Candidates"
        ),
    ),
]


def main(argv: list[str] | None = None) -> int:
    parser = build_argparser(SOURCE)
    parser.add_argument(
        "--data-as-of",
        required=False,
        help=(
            "ISO date the OPTN report itself states the data is current to "
            "(distinct from --snapshot-date, which is when the operator ran ingest). "
            "If omitted, defaults to --snapshot-date."
        ),
    )
    args = parser.parse_args(argv)
    log = configure_logging(SOURCE)

    data_as_of = args.data_as_of or args.snapshot_date
    target = snapshot_dir(SOURCE, args.snapshot_date)

    missing = [exp for exp in EXPECTED if not (target / exp.filename).exists()]
    if missing:
        log.error("manual-snapshot ingest: missing %d expected file(s) in %s", len(missing), target)
        log.error("Place these CSVs (download from %s) and rerun:", BUILD_ADVANCED_URL)
        for exp in missing:
            log.error("  - %s", exp.filename)
            log.error("    %s", exp.build_query)
        fail("OPTN ingest aborted: required files not present", log=log)

    if args.dry_run:
        log.info("--dry-run: would register %d file(s) in %s", len(EXPECTED), target)
        for exp in EXPECTED:
            log.info("  ✓ %s", exp.filename)
        return 0

    # Idempotency: if meta.json already exists, this snapshot is complete.
    # ensure_snapshot_dir will short-circuit unless --force is passed.
    try:
        ensure_snapshot_dir(SOURCE, args.snapshot_date, args.force)
    except SnapshotComplete as exc:
        log.info("snapshot already complete: %s — skipping", exc.path)
        return 0

    meta = SnapshotMeta(
        source=SOURCE,
        snapshot_date=args.snapshot_date,
        fetched_at=utcnow_iso(),
        pipeline_version=pipeline_version(),
    )

    for exp in EXPECTED:
        path = target / exp.filename
        log.info("registering %s", exp.filename)
        try:
            rows = count_csv_rows(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("row count failed for %s: %r", exp.filename, exc)
            rows = None
        meta.files.append(
            FileRecord(
                name=exp.filename,
                # No public stable URL — the Build Advanced UI generates ad-hoc CSV
                # downloads. We record the canonical entry-point + the query that
                # produces the file so a future operator can reproduce it.
                url=BUILD_ADVANCED_URL,
                sha256=sha256_of(path),
                bytes=path.stat().st_size,
                row_count=rows,
            )
        )

    write_meta(target, meta)

    # Append `data_as_of` and acquisition note to meta.json post-hoc — these are
    # OPTN-specific fields the generic SnapshotMeta dataclass doesn't carry.
    import json

    meta_path = target / "meta.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["data_as_of"] = data_as_of
    payload["acquisition"] = "manual (Build Advanced UI download by operator)"
    payload["build_queries"] = {exp.filename: exp.build_query for exp in EXPECTED}
    meta_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    log.info(
        "snapshot complete: %s (%d files, data_as_of=%s)",
        target,
        len(EXPECTED),
        data_as_of,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

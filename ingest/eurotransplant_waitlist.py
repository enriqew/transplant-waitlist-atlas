"""Bronze ingest: Eurotransplant waiting-list statistics.

Eurotransplant publishes a public statistics portal that drives every report off
a single endpoint:

    https://statistics.eurotransplant.org/reportloader.php?report=<id>&format=xlsx&download=1

Report IDs are stable across years. We curate a focused set that gives us:
  - the multi-year × country × organ active-waitlist matrix (THE primary artifact),
  - per-organ registrations (inflow into the waitlist) by year × country,
  - per-organ removals (outflow, broken down by reason) by year × country,
  - the official yearly overview as a sanity-check companion.

We pull XLSX rather than PDF because xlsx is round-trippable without OCR/table
extraction heuristics — the silver layer can read it with pandas/openpyxl.

License/attribution: © Eurotransplant International Foundation. Required
attribution recorded in meta.json (`url` of each file).
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from ingest._common import (
    FileRecord,
    SnapshotComplete,
    SnapshotMeta,
    build_argparser,
    configure_logging,
    ensure_snapshot_dir,
    fail,
    pipeline_version,
    sha256_of,
    stream_to_file,
    utcnow_iso,
    write_meta,
)

SOURCE = "eurotransplant_waitlist"
BASE_URL = "https://statistics.eurotransplant.org/reportloader.php"
POLITE_DELAY_S = 1.0


@dataclass(frozen=True)
class Report:
    """One Eurotransplant report we want to download."""

    report_id: str
    slug: str
    description: str


# Curated set of waiting-list reports. Each is identified by a stable `report=`
# parameter on the Eurotransplant statistics endpoint. To find new IDs, browse
# https://statistics.eurotransplant.org/ and inspect the `reportloader.php?report=`
# links on a search result page (see README for the discovery procedure).
REPORTS: list[Report] = [
    # ─── Multi-year active waitlist (primary artifact) ─────────────────────
    Report(
        report_id="10728-33135",
        slug="active_waitlist_by_year_by_country_by_organ",
        description="Active waiting list (at year-end) in All ET, by year, by country, by organ",
    ),
    # ─── Yearly overview (sanity-check companion) ──────────────────────────
    Report(
        report_id="10789-32916",
        slug="yearly_statistics_overview_2025",
        description="ET Yearly Statistics Overview — 2025",
    ),
    # ─── Per-organ registrations (inflow), by year, by country ─────────────
    Report(
        report_id="10744-33195",
        slug="kidney_registrations_by_year_by_country",
        description="Kidney waiting list registrations, by year, by country",
    ),
    Report(
        report_id="10744-33178",
        slug="liver_registrations_by_year_by_country",
        description="Liver waiting list registrations, by year, by country",
    ),
    Report(
        report_id="10744-33196",
        slug="heart_registrations_by_year_by_country",
        description="Heart waiting list registrations, by year, by country",
    ),
    Report(
        report_id="10744-33197",
        slug="lung_registrations_by_year_by_country",
        description="Lung waiting list registrations, by year, by country",
    ),
    Report(
        report_id="10744-33176",
        slug="pancreas_registrations_by_year_by_country",
        description="Pancreas waiting list registrations, by year, by country",
    ),
    # ─── Per-organ removals (outflow), by year, by country, by reason ──────
    Report(
        report_id="11146-33195",
        slug="kidney_removals_by_year_by_country_by_reason",
        description="Kidney waiting list removals, by year, by country, by reason",
    ),
    Report(
        report_id="11146-33178",
        slug="liver_removals_by_year_by_country_by_reason",
        description="Liver waiting list removals, by year, by country, by reason",
    ),
    Report(
        report_id="11146-33196",
        slug="heart_removals_by_year_by_country_by_reason",
        description="Heart waiting list removals, by year, by country, by reason",
    ),
    Report(
        report_id="11146-33197",
        slug="lung_removals_by_year_by_country_by_reason",
        description="Lung waiting list removals, by year, by country, by reason",
    ),
    Report(
        report_id="11146-33176",
        slug="pancreas_removals_by_year_by_country_by_reason",
        description="Pancreas waiting list removals, by year, by country, by reason",
    ),
]


def report_url(report_id: str, fmt: str = "xlsx") -> str:
    """Build the download URL for a given report ID."""

    query = urlencode({"report": report_id, "format": fmt, "download": "1"})
    return f"{BASE_URL}?{query}"


def main(argv: list[str] | None = None) -> int:
    args = build_argparser(SOURCE).parse_args(argv)
    log = configure_logging(SOURCE)

    log.info("planned %d Eurotransplant report(s) to fetch", len(REPORTS))

    if args.dry_run:
        for r in REPORTS:
            log.info("  %s → %s", r.report_id, report_url(r.report_id))
        log.info("--dry-run: not writing snapshot")
        return 0

    try:
        target = ensure_snapshot_dir(SOURCE, args.snapshot_date, args.force)
    except SnapshotComplete as exc:
        log.info("snapshot already complete: %s — skipping", exc.path)
        return 0

    meta = SnapshotMeta(
        source=SOURCE,
        snapshot_date=args.snapshot_date,
        fetched_at=utcnow_iso(),
        pipeline_version=pipeline_version(),
    )

    failures: list[tuple[Report, str]] = []
    for report in REPORTS:
        url = report_url(report.report_id)
        dest = target / f"{report.slug}__{report.report_id}.xlsx"

        if dest.exists() and dest.stat().st_size > 0:
            log.info("already present, reusing: %s", dest.name)
        else:
            log.info("downloading %s → %s", report.report_id, dest.name)
            try:
                stream_to_file(url, dest)
            except Exception as exc:  # noqa: BLE001
                # Eurotransplant's server is sometimes slow — keep going and let
                # the operator rerun (file-level reuse picks up where we stopped).
                log.warning("transient failure on %s: %r — skipping", report.report_id, exc)
                if dest.exists() and dest.stat().st_size == 0:
                    dest.unlink()  # don't leave a zero-byte stub that future runs will trust
                failures.append((report, repr(exc)))
                continue
            time.sleep(POLITE_DELAY_S)

        # Sanity check: XLSX always begins with the PK ZIP signature.
        with dest.open("rb") as fh:
            head = fh.read(4)
        if head[:2] != b"PK":
            log.warning(
                "%s does not look like XLSX (header=%r); Eurotransplant likely "
                "returned an HTML error page — dropping",
                dest.name,
                head,
            )
            dest.unlink()
            failures.append((report, "non-XLSX response"))
            continue

        meta.files.append(
            FileRecord(
                name=dest.name,
                url=url,
                sha256=sha256_of(dest),
                bytes=dest.stat().st_size,
                row_count=None,  # populated by silver after parsing XLSX sheets
            )
        )

    write_meta(target, meta)
    log.info(
        "snapshot complete: %s (%d/%d reports captured)",
        target,
        len(meta.files),
        len(REPORTS),
    )
    if failures:
        log.warning("%d report(s) failed — rerun to retry:", len(failures))
        for r, msg in failures:
            log.warning("  %s (%s): %s", r.report_id, r.slug, msg)
        return 2  # non-zero so callers can detect, but snapshot dir is finalized
    return 0


if __name__ == "__main__":
    sys.exit(main())

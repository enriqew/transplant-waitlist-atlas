"""Bronze ingest: Scandiatransplant Q4 quarterly waitlist PDFs.

Scandiatransplant publishes quarterly kidney waitlist statistics for 5 member
countries: Denmark (DNK), Sweden (SWE), Norway (NOR), Finland (FIN), and
Estonia (EST).

The Q4 report for year Y covers "Waiting list statistics January 1st, Y+1"
— effectively the year-end (31 December Y) snapshot.

URL: https://www.scandiatransplant.org/data/sctp_figures_{year}_4Q.pdf
Available years confirmed: 2017-2023 (2024 returns 404 as of 2026-05-20).

Only kidney data contains per-country subtotals. Other organs list
center-level data only; multi-center patient listings create double-counting
that prevents reliable per-country aggregation.
"""

from __future__ import annotations

import sys
import time

from ingest._common import (
    FileRecord,
    SnapshotComplete,
    SnapshotMeta,
    build_argparser,
    configure_logging,
    ensure_snapshot_dir,
    http_get,
    pipeline_version,
    sha256_of,
    stream_to_file,
    utcnow_iso,
    write_meta,
)

SOURCE = "scandiatransplant_waitlist"
BASE_URL = "https://www.scandiatransplant.org/data"
FIRST_YEAR = 2017
POLITE_DELAY_S = 1.5


def _q4_url(year: int) -> str:
    return f"{BASE_URL}/sctp_figures_{year}_4Q.pdf"


def _q4_filename(year: int) -> str:
    return f"sctp_figures_{year}_4Q.pdf"


def main(argv: list[str] | None = None) -> int:
    args = build_argparser(SOURCE).parse_args(argv)
    log = configure_logging(SOURCE)

    snapshot_year = int(args.snapshot_date[:4])
    candidate_years = list(range(FIRST_YEAR, snapshot_year + 1))

    if args.dry_run:
        for year in candidate_years:
            log.info("  %d -> %s", year, _q4_url(year))
        log.info("--dry-run: not writing snapshot")
        return 0

    try:
        target = ensure_snapshot_dir(SOURCE, args.snapshot_date, args.force)
    except SnapshotComplete as exc:
        log.info("snapshot already complete: %s -- skipping", exc.path)
        return 0

    meta = SnapshotMeta(
        source=SOURCE,
        snapshot_date=args.snapshot_date,
        fetched_at=utcnow_iso(),
        pipeline_version=pipeline_version(),
    )

    for year in candidate_years:
        url = _q4_url(year)
        dest = target / _q4_filename(year)

        if dest.exists() and dest.stat().st_size > 0:
            log.info("already present, reusing: %s", dest.name)
        else:
            log.info("trying %d -> %s", year, dest.name)
            try:
                resp = http_get(url)
            except Exception as exc:  # noqa: BLE001
                msg = repr(exc)
                if "404" in msg or "Not Found" in msg:
                    log.info("year %d not yet available (404) -- skipping", year)
                else:
                    log.warning("failed to download %d: %s -- skipping", year, msg)
                continue

            dest.write_bytes(resp.content)
            time.sleep(POLITE_DELAY_S)

        with dest.open("rb") as fh:
            header = fh.read(4)
        if not header.startswith(b"%PDF"):
            log.warning("%s is not a valid PDF (header=%r) -- dropping", dest.name, header)
            dest.unlink()
            continue

        meta.files.append(
            FileRecord(
                name=dest.name,
                url=url,
                sha256=sha256_of(dest),
                bytes=dest.stat().st_size,
                row_count=None,
            )
        )

    if not meta.files:
        raise SystemExit(
            f"no Scandiatransplant Q4 PDFs could be downloaded; "
            f"check network access to {BASE_URL}"
        )

    write_meta(target, meta)
    log.info(
        "snapshot complete: %s (%d Q4 report(s) captured)",
        target,
        len(meta.files),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

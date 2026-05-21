"""Bronze ingest: NHS Blood and Transplant annual activity reports (PDFs).

NHSBT publishes annual UK organ donation and transplant activity reports for
each UK fiscal year (April–March). Each report contains Table 2.1 — the
master waitlist summary by organ and UK nation.

PDFs are hosted on Azure Blob Storage with stable direct URLs. The numeric
blob ID changes every year and is not guessable; we maintain a curated list
of known URLs for past years and scrape the public index page to discover
the current year's URL automatically.

A single snapshot directory holds all fiscal-year PDFs. The prebuild parser
(analyses/nhsbt_pdf_to_long.py) extracts Table 2.1 from each.

Fiscal year labelling: "2024-2025" → year = 2025 (snapshot at 31 March 2025).
"""

from __future__ import annotations

import re
import sys
import time

from ingest._common import (
    FileRecord,
    SnapshotComplete,
    SnapshotMeta,
    build_argparser,
    configure_logging,
    ensure_snapshot_dir,
    fail,
    http_get,
    pipeline_version,
    sha256_of,
    stream_to_file,
    utcnow_iso,
    write_meta,
)

SOURCE = "nhsbt_waitlist"
INDEX_URL = "https://www.odt.nhs.uk/statistics-and-reports/annual-activity-report/"
POLITE_DELAY_S = 1.0

# Curated list of confirmed PDF URLs keyed by "YYYY-YYYY" fiscal year string.
# The numeric blob ID is not predictable; these were resolved from the index page.
# When the index scraper finds a URL not listed here, it adds it automatically.
KNOWN_REPORT_URLS: dict[str, str] = {
    "2024-2025": "https://nhsbtdbe.blob.core.windows.net/umbraco-assets-corp/36795/activity-report-2024-2025-final.pdf",
    "2023-2024": "https://nhsbtdbe.blob.core.windows.net/umbraco-assets-corp/33778/activity-report-2023-2024.pdf",
    "2022-2023": "https://nhsbtdbe.blob.core.windows.net/umbraco-assets-corp/30198/activity-report-2022-2023-final.pdf",
    "2020-2021": "https://nhsbtdbe.blob.core.windows.net/umbraco-assets-corp/24053/activity-report-2020-2021.pdf",
    "2018-2019": "https://nhsbtdbe.blob.core.windows.net/umbraco-assets-corp/16469/organ-donation-and-transplantation-activity-report-2018-2019.pdf",
    "2017-2018": "https://nhsbtdbe.blob.core.windows.net/umbraco-assets-corp/1848/transplant-activity-report-2017-2018.pdf",
    "2016-2017": "https://nhsbtdbe.blob.core.windows.net/umbraco-assets-corp/4657/activity_report_2016_17.pdf",
    "2015-2016": "https://nhsbtdbe.blob.core.windows.net/umbraco-assets-corp/1452/activity_report_2015_16.pdf",
}

# Matches fiscal year in URL or filename: "2024-2025", "2016_17", "2018-2019", etc.
_FISCAL_YEAR_RE = re.compile(r"(\d{4})[-_](\d{2,4})")

# Matches NHSBT blob storage URLs for the activity report PDFs.
_BLOB_URL_RE = re.compile(
    r"https://nhsbtdbe\.blob\.core\.windows\.net/[^\s\"'>]+\.pdf",
    re.IGNORECASE,
)


def _infer_fiscal_year(url: str) -> str | None:
    """Extract 'YYYY-YYYY' from a blob URL. Returns None if not parseable."""
    m = _FISCAL_YEAR_RE.search(url)
    if not m:
        return None
    y1 = int(m.group(1))
    y2_raw = m.group(2)
    y2 = int(y2_raw) if len(y2_raw) == 4 else 2000 + int(y2_raw)
    if y2 <= y1:
        return None
    return f"{y1}-{y2}"


def _scrape_index_for_urls(log: object) -> dict[str, str]:
    """GET the NHSBT annual report index page and extract activity report PDF URLs."""
    try:
        response = http_get(INDEX_URL)
    except Exception as exc:
        log.warning("could not fetch index page %s: %r", INDEX_URL, exc)
        return {}

    found: dict[str, str] = {}
    for url in _BLOB_URL_RE.findall(response.text):
        fy = _infer_fiscal_year(url)
        if fy and fy not in found:
            found[fy] = url
    log.info("index page yielded %d activity report URL(s)", len(found))
    return found


def _pdf_filename(fiscal_year: str) -> str:
    return f"activity-report-{fiscal_year}.pdf"


def main(argv: list[str] | None = None) -> int:
    args = build_argparser(SOURCE).parse_args(argv)
    log = configure_logging(SOURCE)

    if args.dry_run:
        for fy, url in sorted(KNOWN_REPORT_URLS.items()):
            log.info("  %s → %s", fy, url)
        log.info("--dry-run: not writing snapshot")
        return 0

    # Scraped URLs are authoritative (they reflect the live index page).
    # KNOWN_REPORT_URLS fills in any years the scraper missed.
    scraped = _scrape_index_for_urls(log)
    all_report_urls: dict[str, str] = dict(scraped)
    for fy, url in KNOWN_REPORT_URLS.items():
        if fy not in all_report_urls:
            log.info("using known URL for %s (not found by scraper)", fy)
            all_report_urls[fy] = url

    log.info("planned %d NHSBT report(s) to fetch", len(all_report_urls))

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

    failures: list[tuple[str, str]] = []
    for fiscal_year, url in sorted(all_report_urls.items()):
        dest = target / _pdf_filename(fiscal_year)

        if dest.exists() and dest.stat().st_size > 0:
            log.info("already present, reusing: %s", dest.name)
        else:
            log.info("downloading %s → %s", fiscal_year, dest.name)
            try:
                stream_to_file(url, dest)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to download %s: %r — skipping", fiscal_year, exc)
                if dest.exists() and dest.stat().st_size == 0:
                    dest.unlink()
                failures.append((fiscal_year, repr(exc)))
                continue
            time.sleep(POLITE_DELAY_S)

        # Validate PDF magic bytes.
        with dest.open("rb") as fh:
            header = fh.read(4)
        if not header.startswith(b"%PDF"):
            log.warning(
                "%s does not look like a PDF (header=%r) — dropping",
                dest.name,
                header,
            )
            dest.unlink()
            failures.append((fiscal_year, "non-PDF response"))
            continue

        meta.files.append(
            FileRecord(
                name=dest.name,
                url=url,
                sha256=sha256_of(dest),
                bytes=dest.stat().st_size,
                row_count=None,  # populated by prebuild after PDF parsing
            )
        )

    write_meta(target, meta)
    log.info(
        "snapshot complete: %s (%d/%d reports captured)",
        target,
        len(meta.files),
        len(all_report_urls),
    )
    if failures:
        log.warning("%d report(s) failed:", len(failures))
        for fy, msg in failures:
            log.warning("  %s: %s", fy, msg)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

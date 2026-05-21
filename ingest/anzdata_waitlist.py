"""Bronze ingest: ANZDATA annual report Chapter 6 Excel files.

ANZDATA (Australia and New Zealand Dialysis and Transplant Registry) publishes
annual reports with Chapter 6 covering the Australian Kidney Transplant Waiting
List. Each report covers a 6-year rolling window. Downloading multiple report
years extends the historical series.

Chapter 6 Excel URL is CMS-generated (not predictable); the ingest script
scrapes each report's landing page to find the current URL, falling back to a
curated list of known URLs for years the scraper cannot resolve.

Source: https://anzorrg.org.au/reports?type=Annual+Report
Scope:  Australia (AUS) kidney waitlist only. ANZDATA does not publish
        waitlist data for other organs or for New Zealand in Chapter 6.
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
    http_get,
    pipeline_version,
    sha256_of,
    stream_to_file,
    utcnow_iso,
    write_meta,
)

SOURCE = "anzdata_waitlist"
REPORTS_URL = "https://anzorrg.org.au/reports?type=Annual+Report"
POLITE_DELAY_S = 1.5

# Known Chapter 6 Excel URLs keyed by data year (year data was collected to).
# URL suffix is CMS-generated and changes; update here when new reports appear.
KNOWN_CHAPTER6_URLS: dict[int, str] = {
    2024: "https://anzdata.syd1.digitaloceanspaces.com/assets/publications/c06_waiting-list_2024_ar_2025_tables_F_20251205.xlsx",
    2023: "https://anzdata.syd1.digitaloceanspaces.com/assets/publications/06_waiting-list_2023_ar_2024_F_20241224_2025-09-23-013855_qgdf.xlsx",
    2022: "https://anzdata.syd1.digitaloceanspaces.com/assets/publications/c06_waiting-list_2022_ar_2023_v0.2_20231109.xlsx",
    2021: "https://anzdata.syd1.digitaloceanspaces.com/assets/publications/c06_waiting-list_2021_ar_2022_v1.1_Final_2025-08-12-021806_vbzl.xlsx",
}

# Matches ANZDATA annual report landing page slugs in HTML.
_REPORT_SLUG_RE = re.compile(
    r'href="(/reports/anzdata-\d+\w*-annual-report-[^"]+)"',
    re.IGNORECASE,
)
# Matches Chapter 6 xlsx on an individual report landing page.
_CHAPTER6_XLSX_RE = re.compile(
    r'https://anzdata\.syd1\.digitaloceanspaces\.com[^\s"\']*waiting[_-]list[^\s"\']*\.xlsx',
    re.IGNORECASE,
)
# Extracts data year from Chapter 6 xlsx filename (e.g. "waiting-list_2024_").
_DATA_YEAR_RE = re.compile(r'waiting[_-]list[_-](\d{4})[_-]', re.IGNORECASE)


def _ch6_xlsx_filename(data_year: int) -> str:
    return f"anzdata_ch6_{data_year}.xlsx"


def _scrape_new_report_urls(log: object) -> dict[int, str]:
    """Scrape the reports listing page for ANZDATA report landing page URLs,
    then fetch each landing page to find its Chapter 6 xlsx URL.

    Returns {data_year: xlsx_url} for years not already in KNOWN_CHAPTER6_URLS.
    """
    try:
        resp = http_get(REPORTS_URL)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not fetch reports listing %s: %r", REPORTS_URL, exc)
        return {}

    base = "https://anzorrg.org.au"
    slugs = list(dict.fromkeys(_REPORT_SLUG_RE.findall(resp.text)))  # dedup, preserve order
    log.info("listing page: found %d ANZDATA report slug(s)", len(slugs))

    discovered: dict[int, str] = {}
    for slug in slugs:
        landing_url = base + slug
        try:
            landing = http_get(landing_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not fetch %s: %r", landing_url, exc)
            continue
        xlsx_urls = _CHAPTER6_XLSX_RE.findall(landing.text)
        if not xlsx_urls:
            continue
        xlsx_url = xlsx_urls[0]
        m = _DATA_YEAR_RE.search(xlsx_url)
        if not m:
            continue
        data_year = int(m.group(1))
        if data_year not in KNOWN_CHAPTER6_URLS and data_year not in discovered:
            log.info("discovered new Chapter 6 xlsx for data_year=%d: %s", data_year, xlsx_url)
            discovered[data_year] = xlsx_url
        time.sleep(POLITE_DELAY_S)

    return discovered


def main(argv: list[str] | None = None) -> int:
    args = build_argparser(SOURCE).parse_args(argv)
    log = configure_logging(SOURCE)

    if args.dry_run:
        for dy, url in sorted(KNOWN_CHAPTER6_URLS.items()):
            log.info("  data_year=%d -> %s", dy, url)
        log.info("--dry-run: not writing snapshot")
        return 0

    try:
        target = ensure_snapshot_dir(SOURCE, args.snapshot_date, args.force)
    except SnapshotComplete as exc:
        log.info("snapshot already complete: %s -- skipping", exc.path)
        return 0

    discovered = _scrape_new_report_urls(log)
    all_urls: dict[int, str] = {**KNOWN_CHAPTER6_URLS, **discovered}
    log.info("planned %d Chapter 6 Excel file(s)", len(all_urls))

    meta = SnapshotMeta(
        source=SOURCE,
        snapshot_date=args.snapshot_date,
        fetched_at=utcnow_iso(),
        pipeline_version=pipeline_version(),
    )

    for data_year, url in sorted(all_urls.items()):
        dest = target / _ch6_xlsx_filename(data_year)

        if dest.exists() and dest.stat().st_size > 0:
            log.info("already present, reusing: %s", dest.name)
        else:
            log.info("downloading data_year=%d -> %s", data_year, dest.name)
            try:
                stream_to_file(url, dest)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to download data_year=%d: %r -- skipping", data_year, exc)
                if dest.exists() and dest.stat().st_size == 0:
                    dest.unlink()
                continue
            time.sleep(POLITE_DELAY_S)

        # Validate xlsx magic bytes (PK zip header).
        with dest.open("rb") as fh:
            magic = fh.read(4)
        if magic != b"PK\x03\x04":
            log.warning("%s does not look like an xlsx file (magic=%r) -- dropping", dest.name, magic)
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
        raise SystemExit("no ANZDATA Chapter 6 Excel files could be downloaded")

    write_meta(target, meta)
    log.info(
        "snapshot complete: %s (%d Excel file(s) captured, data years: %s)",
        target,
        len(meta.files),
        sorted(
            int(f.name.removeprefix("anzdata_ch6_").removesuffix(".xlsx"))
            for f in meta.files
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Bronze ingest: ONT España organ activity PDFs.

ONT (Organización Nacional de Trasplantes) publishes one annual activity report
per organ (kidney, heart, liver, lung) in PDF format.  Reports for year Y are
typically uploaded to the ONT WordPress site in the first quarter of year Y+1.

URL pattern: https://www.ont.es/wp-content/uploads/{Y+1}/{MM}/{FILENAME}.pdf

This module tries each month 01-06 of the upload year for several filename
variants until a valid PDF is found.

Usage:
    python -m ingest.ont_waitlist --snapshot-date 2026-05-20 [--force] [--dry-run]
"""

from __future__ import annotations

import sys
from pathlib import Path

from ingest._common import (
    FileRecord,
    SnapshotComplete,
    SnapshotMeta,
    build_argparser,
    configure_logging,
    ensure_snapshot_dir,
    pipeline_version,
    sha256_of,
    stream_to_file,
    utcnow_iso,
    write_meta,
)

_SOURCE = "ont_waitlist"
_ONT_BASE = "https://www.ont.es/wp-content/uploads"

# Per-organ filename candidates (tried in order). {year} is the data year.
_FILENAME_PATTERNS: dict[str, list[str]] = {
    "kidney": [
        "ACTIVIDAD-DE-DONACION-Y-TRASPLANTE-RENAL-ESPANA-{year}.pdf",
        "ACTIVIDAD-DE-DONACION-Y-TRASPLANTE-RENAL-ESPANA-{year}-web.pdf",
    ],
    "heart": [
        "ACTIVIDAD-DE-DONACION-Y-TRASPLANTE-CARDIACO-{year}.pdf",
        "ACTIVIDAD-DE-DONACION-Y-TRASPLANTE-CARDIACO-ESPANA-{year}.pdf",
        "ACTIVIDAD-DE-DONACION-Y-TRASPLANTE-CARDIACO-{year}-web.pdf",
    ],
    "liver": [
        "ACTIVIDAD-DE-DONACION-Y-TRASPLANTE-HEPATICO-ESPANA-{year}.pdf",
        "ACTIVIDAD-DE-DONACION-Y-TRASPLANTE-HEPATICO-ESPANA-{year}-web.pdf",
        "ACTIVIDAD-DE-DONACION-Y-TRASPLANTE-HEPATICO-{year}.pdf",
    ],
    "lung": [
        "ACTIVIDAD-DE-DONACION-Y-TRASPLANTE-PULMONAR-ESPANA-{year}.pdf",
        "ACTIVIDAD-DE-DONACION-Y-TRASPLANTE-PULMONAR-ESPANA-{year}-web.pdf",
        "ACTIVIDAD-DE-DONACION-Y-TRASPLANTE-PULMONAR-{year}.pdf",
    ],
}

# Upload year = data year + 1; try months 01-06.
_UPLOAD_MONTHS = ["01", "02", "03", "04", "05", "06"]


def _try_download(data_year: int, organ: str, dest: Path, log) -> str | None:
    """Try all URL candidates for one organ/year. Returns downloaded URL or None."""
    upload_year = data_year + 1
    patterns = _FILENAME_PATTERNS[organ]

    for month in _UPLOAD_MONTHS:
        for pattern in patterns:
            filename = pattern.format(year=data_year)
            url = f"{_ONT_BASE}/{upload_year}/{month}/{filename}"
            try:
                stream_to_file(url, dest)
                return url
            except Exception:
                if dest.exists():
                    dest.unlink(missing_ok=True)
                continue
    return None


def ingest(snapshot_date: str, force: bool = False, dry_run: bool = False) -> None:
    log = configure_logging(_SOURCE)

    if dry_run:
        snap_year = int(snapshot_date[:4])
        data_year = snap_year - 2
        for organ in _FILENAME_PATTERNS:
            upload_year = data_year + 1
            for month in _UPLOAD_MONTHS:
                for pattern in _FILENAME_PATTERNS[organ]:
                    url = f"{_ONT_BASE}/{upload_year}/{month}/{pattern.format(year=data_year)}"
                    log.info("DRY-RUN would try: %s", url)
        return

    try:
        snap_dir = ensure_snapshot_dir(_SOURCE, snapshot_date, force)
    except SnapshotComplete:
        log.info("snapshot already complete for %s (use --force to re-download)", snapshot_date)
        return

    snap_year = int(snapshot_date[:4])
    data_year = snap_year - 2  # ONT publishes Y data in Q1 of Y+1; by May Y+2 use Y

    fetched_at = utcnow_iso()
    file_records: list[FileRecord] = []
    failed: list[str] = []

    for organ in _FILENAME_PATTERNS:
        dest = snap_dir / f"ont-{organ}-{data_year}.pdf"
        if dest.exists() and not force:
            log.info("%s: already present, skipping", dest.name)
            file_records.append(FileRecord(
                name=dest.name,
                url="(pre-existing)",
                sha256=sha256_of(dest),
                bytes=dest.stat().st_size,
            ))
            continue

        log.info("%s: searching for %d report...", organ, data_year)
        url = _try_download(data_year, organ, dest, log)
        if url:
            log.info("%s: downloaded from %s", organ, url)
            file_records.append(FileRecord(
                name=dest.name,
                url=url,
                sha256=sha256_of(dest),
                bytes=dest.stat().st_size,
            ))
        else:
            log.error(
                "%s: FAILED — could not find PDF at any candidate URL. "
                "Tried upload months %s of %d. "
                "Manually download from https://www.ont.es/ and place as: %s",
                organ, _UPLOAD_MONTHS, data_year + 1, dest,
            )
            failed.append(organ)

    if not file_records and failed:
        raise SystemExit("all ONT organs failed — cannot create snapshot")

    write_meta(snap_dir, SnapshotMeta(
        source=_SOURCE,
        snapshot_date=snapshot_date,
        fetched_at=fetched_at,
        pipeline_version=pipeline_version(),
        files=file_records,
    ))
    log.info("snapshot written: %s (%d organ(s), %d failed)", snap_dir, len(file_records), len(failed))

    if failed:
        raise SystemExit(f"partial ONT snapshot: {len(failed)} organ(s) missing: {failed}")


def main() -> None:
    parser = build_argparser(_SOURCE)
    args = parser.parse_args()
    ingest(args.snapshot_date, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

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

import argparse
import json
import sys
from pathlib import Path

# Repository-level imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest._common import (  # noqa: E402
    make_snapshot_dir,
    stream_download,
    sha256_file,
    write_meta,
)

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


def _try_download(data_year: int, organ: str, dest: Path, dry_run: bool) -> str | None:
    """Try all URL candidates for one organ/year. Returns downloaded URL or None."""
    upload_year = data_year + 1
    patterns = _FILENAME_PATTERNS[organ]
    import urllib.request

    for month in _UPLOAD_MONTHS:
        for pattern in patterns:
            filename = pattern.format(year=data_year)
            url = f"{_ONT_BASE}/{upload_year}/{month}/{filename}"
            if dry_run:
                print(f"  DRY-RUN would try: {url}")
                continue
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    if resp.status == 200:
                        stream_download(url, dest)
                        return url
            except Exception:
                continue
    return None


def ingest(
    snapshot_date: str,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    raw_root = Path(__file__).resolve().parents[1] / "data" / "raw" / "ont_waitlist"
    snap_dir = make_snapshot_dir(raw_root, snapshot_date, force=force)
    meta_path = snap_dir / "meta.json"

    if meta_path.exists() and not force:
        print(f"snapshot already complete: {snap_dir} (use --force to re-download)")
        return

    # Infer data year from snapshot date (e.g. 2026-05-20 → latest available = 2024)
    snap_year = int(snapshot_date[:4])
    data_year = snap_year - 2  # ONT publishes Y data in Q1 of Y+1; by May Y+2 use Y

    downloaded: dict[str, str] = {}  # organ → url
    failed: list[str] = []

    for organ in _FILENAME_PATTERNS:
        dest = snap_dir / f"ont-{organ}-{data_year}.pdf"
        if dest.exists() and not force:
            print(f"  {dest.name}: already present, skip")
            downloaded[organ] = "(pre-existing)"
            continue
        print(f"  {organ}: searching for {data_year} report...")
        url = _try_download(data_year, organ, dest, dry_run)
        if url:
            print(f"  {organ}: downloaded from {url}")
            downloaded[organ] = url
        else:
            msg = (
                f"  {organ}: FAILED — could not find PDF at any candidate URL.\n"
                f"    Tried upload months {_UPLOAD_MONTHS} of {data_year + 1}.\n"
                f"    Manually download from https://www.ont.es/ and place as:\n"
                f"    {dest}"
            )
            print(msg, file=sys.stderr)
            failed.append(organ)

    if dry_run:
        print("DRY-RUN complete, no files written")
        return

    if not downloaded and failed:
        raise SystemExit("all ONT organs failed — cannot create snapshot")

    shas = {organ: sha256_file(snap_dir / f"ont-{organ}-{data_year}.pdf")
            for organ in downloaded if (snap_dir / f"ont-{organ}-{data_year}.pdf").exists()}

    meta = {
        "source": "ONT",
        "data_year": data_year,
        "snapshot_date": snapshot_date,
        "organs_downloaded": list(downloaded.keys()),
        "organs_failed": failed,
        "urls": downloaded,
        "sha256": shas,
    }
    write_meta(snap_dir, meta)
    print(f"snapshot written: {snap_dir} ({len(downloaded)} organ(s), {len(failed)} failed)")

    if failed:
        raise SystemExit(f"partial ONT snapshot: {len(failed)} organ(s) missing: {failed}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest ONT España organ activity PDFs")
    parser.add_argument("--snapshot-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="Overwrite existing snapshot")
    parser.add_argument("--dry-run", action="store_true", help="Print URLs without downloading")
    args = parser.parse_args()
    ingest(args.snapshot_date, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

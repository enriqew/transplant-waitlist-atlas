"""Datos.gob.mx (CKAN) discovery + generic CSV-snapshot helper.

The Mexican government open-data portal exposes a CKAN-compatible API at
https://www.datos.gob.mx/api/3/action/. We use it to discover the current CSV resources
for each CENATRA dataset without hard-coding URLs that rotate when CENATRA reissues a file.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from ingest._common import (
    FileRecord,
    SnapshotComplete,
    SnapshotMeta,
    configure_logging,
    count_csv_rows,
    ensure_snapshot_dir,
    fail,
    http_get,
    pipeline_version,
    sha256_of,
    stream_to_file,
    utcnow_iso,
    write_meta,
)

CKAN_BASE = "https://www.datos.gob.mx/api/3/action"


@dataclass
class CkanResource:
    """One file (resource) inside a CKAN dataset."""

    id: str
    name: str
    url: str
    format: str
    last_modified: str | None


def list_dataset_resources(dataset_slug: str) -> list[CkanResource]:
    """Call CKAN's package_show and return its resources in their declared order.

    Raises requests.HTTPError on any non-2xx response.
    """

    response = http_get(f"{CKAN_BASE}/package_show?id={dataset_slug}")
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"CKAN reported failure for dataset {dataset_slug}: {payload}")
    return [
        CkanResource(
            id=raw["id"],
            name=raw.get("name") or raw.get("description") or raw["id"],
            url=raw["url"],
            format=(raw.get("format") or "").lower(),
            last_modified=raw.get("last_modified") or raw.get("created"),
        )
        for raw in payload["result"]["resources"]
    ]


def filter_csv(resources: list[CkanResource]) -> list[CkanResource]:
    """Keep only resources that look like CSVs."""

    return [r for r in resources if r.format == "csv" or r.url.lower().endswith(".csv")]


def ingest_ckan_csv_dataset(
    *,
    source: str,
    dataset_slug: str,
    args: argparse.Namespace,
    name_substrings: list[str] | None = None,
    name_excludes: list[str] | None = None,
) -> int:
    """Generic flow for any CENATRA / datos.gob.mx CSV dataset.

    Returns process exit code. Any unrecoverable error calls fail() (raises SystemExit).

    If `name_substrings` is provided, only resources whose `name` (case-insensitive)
    contains at least one of those substrings are kept. Use this for datasets that
    bundle several unrelated files.
    """

    log = configure_logging(source)
    log.info("discovering resources via CKAN for %s", dataset_slug)
    try:
        resources = filter_csv(list_dataset_resources(dataset_slug))
    except Exception as exc:  # noqa: BLE001
        fail(f"CKAN discovery failed: {exc!r}", log=log)

    if name_substrings:
        needles = [n.lower() for n in name_substrings]
        resources = [r for r in resources if any(n in r.name.lower() for n in needles)]
        log.info("filtered to %d resource(s) matching: %s", len(resources), name_substrings)

    if name_excludes:
        excludes = [n.lower() for n in name_excludes]
        resources = [r for r in resources if not any(n in r.name.lower() for n in excludes)]
        log.info(
            "after excluding %s: %d resource(s) remain", name_excludes, len(resources)
        )

    if not resources:
        fail(f"no CSV resources found in dataset {dataset_slug}", log=log)

    log.info("found %d CSV resource(s)", len(resources))
    for r in resources:
        log.info("  - %s  (%s)", r.name, r.url)

    if args.dry_run:
        log.info("--dry-run: not writing snapshot")
        return 0

    try:
        target = ensure_snapshot_dir(source, args.snapshot_date, args.force)
    except SnapshotComplete as exc:
        log.info("snapshot already complete: %s — skipping", exc.path)
        return 0
    meta = SnapshotMeta(
        source=source,
        snapshot_date=args.snapshot_date,
        fetched_at=utcnow_iso(),
        pipeline_version=pipeline_version(),
    )

    for resource in resources:
        safe_name = resource.name.replace(" ", "_").replace("/", "_")
        if not safe_name.lower().endswith(".csv"):
            safe_name += ".csv"
        dest = target / safe_name
        log.info("downloading %s → %s", resource.url, dest.name)
        try:
            bytes_written = stream_to_file(resource.url, dest)
        except Exception as exc:  # noqa: BLE001
            fail(f"download failed for {resource.url}: {exc!r}", log=log)

        try:
            rows = count_csv_rows(dest)
        except Exception as exc:  # noqa: BLE001
            log.warning("row count failed for %s: %r", dest.name, exc)
            rows = None

        meta.files.append(
            FileRecord(
                name=safe_name,
                url=resource.url,
                sha256=sha256_of(dest),
                bytes=bytes_written,
                row_count=rows,
            )
        )

    write_meta(target, meta)
    log.info("snapshot complete: %s", target)
    return 0

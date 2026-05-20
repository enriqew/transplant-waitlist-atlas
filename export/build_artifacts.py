"""Build the downstream JSON artifacts from the gold DuckDB tables.

Outputs (data/exports/):
  - world-waitlist.json          one record per (country, year, organ)
  - mexico-waitlist.json         one record per (state, year, quarter, organ)
  - us-fate-distribution.json    one record per (year, organ, reason_bucket)
                                 — the differentiating viz: what happens to
                                 people on the US waitlist
  - meta.json                    pipeline provenance: per-source URLs,
                                 snapshot dates, SHA256, row counts, coverage

Run:
    python -m export.build_artifacts
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
DUCKDB_PATH = Path(
    os.environ.get("TRANSPLANT_WAITLIST_DUCKDB", REPO_ROOT / "data/duckdb/waitlist.duckdb")
)
EXPORTS_DIR = REPO_ROOT / "data/exports"
RAW_ROOT = REPO_ROOT / "data/raw"

# Hard caps — keep the portfolio bundle small. Each is gzipped before check
# (CloudFront serves gzipped responses, so that's the real-world wire size).
JSON_MAX_GZIPPED_BYTES = 500_000


def _configure_logging() -> logging.Logger:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )
    return logging.getLogger("export.build_artifacts")


def _write_json(path: Path, payload: object, *, log: logging.Logger) -> None:
    """Write JSON with a trailing newline + check the gzipped size."""

    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    gz = gzip.compress(raw, compresslevel=9)
    if len(gz) > JSON_MAX_GZIPPED_BYTES:
        raise SystemExit(
            f"{path.name} would be {len(gz):,} bytes gzipped, exceeds {JSON_MAX_GZIPPED_BYTES:,} limit"
        )
    path.write_bytes(raw + b"\n")
    log.info("%s: %s bytes raw, %s gzipped", path.name, f"{len(raw):,}", f"{len(gz):,}")


def _build_world_waitlist(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """One record per (country, year, organ). Drives the world choropleth."""

    rows = con.execute(
        """
        SELECT country_iso3, year, organ, patients, source
        FROM main_gold.fct_waitlist_country_year_organ
        ORDER BY country_iso3, year, organ
        """
    ).fetchall()
    return [
        {
            "country_iso3": r[0],
            "year": int(r[1]),
            "organ": r[2],
            "patients": int(r[3]),
            "source": r[4],
        }
        for r in rows
    ]


def _build_mexico_waitlist(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """One record per (state, year, quarter, organ). Drives the México inset."""

    rows = con.execute(
        """
        SELECT region_code, year, quarter, organ, patients
        FROM main_gold.fct_waitlist_mx_state_quarter_organ
        ORDER BY region_code, year, quarter, organ
        """
    ).fetchall()
    return [
        {
            "state_code": r[0],
            "year": int(r[1]),
            "quarter": int(r[2]),
            "organ": r[3],
            "patients": int(r[4]),
        }
        for r in rows
    ]


def _build_us_fate_distribution(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """One record per (year, organ, reason_bucket). The differentiating viz.

    Aggregates the 21 raw OPTN reasons into 6 analytic buckets:
    transplanted_deceased | transplanted_living | died_waiting |
    removed_too_sick      | improved            | other
    """

    rows = con.execute(
        """
        SELECT year, organ, reason_bucket, SUM(patients) AS patients
        FROM main_gold.fct_waitlist_removals
        WHERE country_iso3 = 'USA'
        GROUP BY 1, 2, 3
        ORDER BY year, organ, reason_bucket
        """
    ).fetchall()
    return [
        {
            "year": int(r[0]),
            "organ": r[1],
            "reason_bucket": r[2],
            "patients": int(r[3]),
        }
        for r in rows
    ]


def _build_meta(con: duckdb.DuckDBPyConnection) -> dict:
    """Pipeline provenance: collect every bronze source's meta.json + summary
    statistics from the gold marts."""

    sources: list[dict] = []
    for source_dir in sorted(RAW_ROOT.iterdir()):
        if not source_dir.is_dir():
            continue
        # Pick the latest snapshot under each source.
        snapshots = sorted(p for p in source_dir.iterdir() if p.is_dir())
        if not snapshots:
            continue
        latest = snapshots[-1]
        meta_path = latest / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        total_bytes = sum(f.get("bytes", 0) for f in meta.get("files", []))
        total_rows = sum(f.get("row_count") or 0 for f in meta.get("files", []))
        sources.append(
            {
                "name": meta.get("source"),
                "snapshot_date": meta.get("snapshot_date"),
                "data_as_of": meta.get("data_as_of"),
                "files": len(meta.get("files", [])),
                "bytes": total_bytes,
                "rows_ingested": total_rows or None,
            }
        )

    summary = con.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM main_gold.fct_waitlist_country_year_organ)        AS world_rows,
            (SELECT COUNT(*) FROM main_gold.fct_waitlist_mx_state_quarter_organ)    AS mexico_rows,
            (SELECT COUNT(*) FROM main_gold.fct_waitlist_removals)                  AS removals_rows,
            (SELECT COUNT(DISTINCT country_iso3)
                FROM main_gold.fct_waitlist_country_year_organ)                     AS countries_world,
            (SELECT MIN(year) FROM main_gold.fct_waitlist_country_year_organ)       AS world_min_year,
            (SELECT MAX(year) FROM main_gold.fct_waitlist_country_year_organ)       AS world_max_year,
            (SELECT MIN(year) FROM main_gold.fct_waitlist_removals)                 AS removals_min_year,
            (SELECT MAX(year) FROM main_gold.fct_waitlist_removals)                 AS removals_max_year
        """
    ).fetchone()

    return {
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pipeline_version": "0.1.0",
        "sources": sources,
        "artifact_row_counts": {
            "world_waitlist": int(summary[0]),
            "mexico_waitlist": int(summary[1]),
            "us_fate_distribution": int(summary[2]),
        },
        "coverage": {
            "countries_world": int(summary[3]),
            "world_years": [int(summary[4]), int(summary[5])],
            "removals_years": [int(summary[6]), int(summary[7])],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out-dir",
        default=str(EXPORTS_DIR),
        help="Destination directory (default: data/exports/)",
    )
    args = parser.parse_args(argv)
    log = _configure_logging()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("connecting to %s", DUCKDB_PATH)
    if not DUCKDB_PATH.exists():
        raise SystemExit(f"DuckDB not found at {DUCKDB_PATH}; run `make build` first")
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)

    log.info("building world-waitlist.json")
    world = _build_world_waitlist(con)
    _write_json(out_dir / "world-waitlist.json", world, log=log)

    log.info("building mexico-waitlist.json")
    mexico = _build_mexico_waitlist(con)
    _write_json(out_dir / "mexico-waitlist.json", mexico, log=log)

    log.info("building us-fate-distribution.json")
    fate = _build_us_fate_distribution(con)
    _write_json(out_dir / "us-fate-distribution.json", fate, log=log)

    log.info("building meta.json")
    meta = _build_meta(con)
    _write_json(out_dir / "meta.json", meta, log=log)

    log.info("all artifacts written to %s", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Shared helpers for bronze-layer ingest scripts.

Every ingest script:
  - resolves its snapshot directory under data/raw/{source}/{snapshot_date}/
  - downloads source files there
  - computes SHA256 of each downloaded file
  - writes a sibling meta.json with provenance (url, fetched_at, sha256, bytes, row_count?)
  - exits non-zero on any network or parsing failure

This module owns those concerns so the per-source scripts only describe what to fetch.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_ROOT = REPO_ROOT / "data" / "raw"

CHUNK_SIZE = 1 << 15  # 32 KiB
HTTP_TIMEOUT_SECONDS = 60
USER_AGENT = "transplant-waitlist-atlas/0.1 (+https://github.com/enriqew/transplant-waitlist-atlas)"


def configure_logging(source: str) -> logging.Logger:
    """One logger per ingest module, stderr only, ISO timestamps."""

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
        force=True,
    )
    return logging.getLogger(f"ingest.{source}")


def build_argparser(source: str) -> argparse.ArgumentParser:
    """Standard CLI surface shared by every ingest script."""

    parser = argparse.ArgumentParser(prog=f"ingest.{source}", description=f"Bronze ingest: {source}")
    parser.add_argument(
        "--snapshot-date",
        default=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        help="ISO date (UTC) for the snapshot directory. Default: today.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the snapshot directory already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be fetched without downloading.",
    )
    return parser


def snapshot_dir(source: str, snapshot_date: str) -> Path:
    """Return data/raw/{source}/{snapshot_date}/ (does not create it)."""

    return RAW_ROOT / source / snapshot_date


def ensure_snapshot_dir(source: str, snapshot_date: str, force: bool) -> Path:
    """Create the snapshot dir.

    Behaviour by existing state:
      - dir missing or empty → create and proceed
      - dir has meta.json    → snapshot is considered complete; raise SnapshotComplete
                               so the caller can short-circuit (Make resumability)
      - dir has files but no meta.json → partial/aborted run; refuse unless --force
    """

    target = snapshot_dir(source, snapshot_date)
    if target.exists() and any(target.iterdir()):
        meta_present = (target / "meta.json").exists()
        if meta_present and not force:
            raise SnapshotComplete(target)
        if not meta_present and not force:
            raise SystemExit(
                f"partial snapshot at {target} (no meta.json); rerun with --force to overwrite"
            )
    target.mkdir(parents=True, exist_ok=True)
    return target


class SnapshotComplete(Exception):
    """Raised by ensure_snapshot_dir when the snapshot is already finalized."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"snapshot already complete at {path}")
        self.path = path


def sha256_of(path: Path) -> str:
    """Streaming SHA256 of a file."""

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def http_get(url: str, *, timeout: int = HTTP_TIMEOUT_SECONDS) -> requests.Response:
    """GET with our UA, no retries (failures must surface)."""

    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response


def stream_to_file(
    url: str,
    dest: Path,
    *,
    timeout: int = HTTP_TIMEOUT_SECONDS,
    verify: str | bool | None = None,
) -> int:
    """Stream a GET response into `dest`. Returns bytes written.

    `verify` is forwarded to requests. Pass a path to a custom CA bundle for sources
    whose servers don't include their intermediate certs.
    """

    bytes_written = 0
    with requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        stream=True,
        verify=True if verify is None else verify,
    ) as response:
        response.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    fh.write(chunk)
                    bytes_written += len(chunk)
    return bytes_written


@dataclass
class FileRecord:
    """Single file inside a snapshot, recorded in meta.json."""

    name: str
    url: str
    sha256: str
    bytes: int
    row_count: int | None = None


@dataclass
class SnapshotMeta:
    """Provenance for one ingest run; serialised to meta.json."""

    source: str
    snapshot_date: str
    fetched_at: str
    pipeline_version: str
    files: list[FileRecord] = field(default_factory=list)


def write_meta(snapshot_dir_path: Path, meta: SnapshotMeta) -> None:
    """Write the per-snapshot meta.json. Pretty-printed for git diffability."""

    payload = asdict(meta)
    (snapshot_dir_path / "meta.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def pipeline_version() -> str:
    """Best-effort read of pyproject.toml version, falls back to '0.0.0+unknown'."""

    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return "0.0.0+unknown"
    for raw in pyproject.read_text(encoding="utf-8").splitlines():
        cleaned = raw.strip()
        if cleaned.startswith("version"):
            return cleaned.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0+unknown"


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def count_csv_rows(path: Path) -> int:
    """Line count minus 1 (header). Safe for moderately sized files."""

    with path.open("rb") as fh:
        total = sum(1 for _ in fh)
    return max(total - 1, 0)


def fail(message: str, *, log: logging.Logger | None = None, code: int = 1) -> None:
    """Log and exit non-zero. Use for any unrecoverable ingest failure."""

    if log is not None:
        log.error(message)
    else:
        print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


__all__ = [
    "FileRecord",
    "SnapshotComplete",
    "SnapshotMeta",
    "build_argparser",
    "configure_logging",
    "count_csv_rows",
    "ensure_snapshot_dir",
    "fail",
    "http_get",
    "pipeline_version",
    "sha256_of",
    "snapshot_dir",
    "stream_to_file",
    "utcnow_iso",
    "write_meta",
]

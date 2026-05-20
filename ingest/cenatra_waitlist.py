"""Bronze ingest: CENATRA organ-transplant waiting list (México).

Two CENATRA datasets, both on datos.gob.mx CKAN:
  - pacientes_espera_organo_tejido      (waiting list, quarterly patient-level snapshots)
  - establecimientos_activos_programas  (authorized transplant establishments,
                                         provides the program-level context for the waitlist)

This is the *core* dataset for this project. CENATRA publishes patient-level rows
each quarter: one row per registered patient currently waiting for an organ. The
silver layer aggregates `COUNT(*)` per (state, organ, snapshot_period).

We avoid the `organization_show` CKAN endpoint (which returns 404 on
www.datos.gob.mx) and pull each dataset directly via `package_show`. Override the
slug list at runtime via TRANSPLANT_WAITLIST_CENATRA_SLUGS (comma-separated) if
CENATRA reorganizes the publishing slugs.

License: Libre Uso MX.
"""

from __future__ import annotations

import os
import sys

from ingest._ckan import ingest_ckan_csv_dataset
from ingest._common import build_argparser, configure_logging

SOURCE = "cenatra_waitlist"
DEFAULT_SLUGS = [
    "pacientes_espera_organo_tejido",
    "establecimientos_activos_programas",
]


def main(argv: list[str] | None = None) -> int:
    args = build_argparser(SOURCE).parse_args(argv)
    log = configure_logging(SOURCE)

    raw = os.environ.get("TRANSPLANT_WAITLIST_CENATRA_SLUGS")
    slugs = [s.strip() for s in raw.split(",") if s.strip()] if raw else DEFAULT_SLUGS
    log.info("waitlist-related dataset slugs: %s", slugs)

    # Each slug becomes its own snapshot directory: data/raw/cenatra_waitlist__<slug>/<date>/
    # so the silver model can union them with a single glob.
    exit_codes = [
        ingest_ckan_csv_dataset(source=f"{SOURCE}__{slug}", dataset_slug=slug, args=args)
        for slug in slugs
    ]
    return max(exit_codes) if exit_codes else 0


if __name__ == "__main__":
    sys.exit(main())

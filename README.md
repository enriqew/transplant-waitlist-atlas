# transplant-waitlist-atlas

Offline data pipeline that ingests organ-transplant **waiting list** statistics from multiple national registries, normalizes their heterogeneous schemas, and emits static JSON artifacts ready for visualization.

Companion project to [`transplant-atlas`](https://github.com/enriqew/transplant-atlas), which covers donation and transplantation rates. Where the atlas answers *"who is doing transplants?"*, this project answers *"who is waiting for one?"*.

The output is consumed by [enriqueredonda.dev](https://github.com/enriqew/data-dive-design-hub) at the `/projects/transplant-waitlist-atlas` route, but every artifact is plain JSON and can be reused by any client.

## Why this is hard

Unlike donation and transplant counts (which IRODaT and GODT publish in a uniform-ish shape across ~110 countries), **waiting-list statistics are not standardized internationally**. Each major national or regional registry publishes its own format, on its own cadence, with its own definition of *"on the waitlist"*:

| Registry | Coverage | Format | Cadence | Granularity |
|---|---|---|---|---|
| **OPTN/UNOS** | United States | CSV + interactive report builder | Weekly | Per organ, demographics |
| **CENATRA** | México | CSV via CKAN, patient-level | Quarterly snapshots | Per organ, per state |
| **NHS Blood and Transplant** | United Kingdom | Annual PDF reports + some CSV | Annual | Per organ, region |
| **Eurotransplant** | DE, NL, BE, AT, HU, SI, HR, LU | Annual PDF | Annual | Per organ, country |
| **ONT** | España | Memoria anual PDF | Annual | Per organ, autonomous community |
| **Scandiatransplant** | DK, FI, IS, NO, SE | Annual PDF | Annual | Per organ |
| **ANZDATA** | Australia, New Zealand | Academic registry | Annual | Renal focus |
| **GODT** | Various (WHO/ONT) | Global report PDF | Annual | Country-level when present |

Realistic country coverage: **15–25 countries**, biased toward Europe, North America, Oceania.

### Definitional pitfalls (will be documented in the project's blog post)

- **"Active waitlist"** (US OPTN) ≠ **"Registered patients"** (UK NHS BT) ≠ **"Lista activa"** (España ONT).
- Some registries exclude temporarily inactive candidates; others include them.
- Some report year-end snapshots; others report monthly averages or peak.
- Comparing 1:1 is **fundamentally limited** — the project's job is to make those caveats first-class, not paper over them.

## Architecture

Same medallion (bronze / silver / gold) shape as [`transplant-atlas`](https://github.com/enriqew/transplant-atlas), with DuckDB as the analytical engine and `dbt-duckdb` for SQL-first transformations.

```
ingest/                            # bronze — Python fetchers per source
  cenatra_waitlist.py
  optn_waitlist.py
  nhsbt_waitlist.py                # planned
  eurotransplant_waitlist.py       # planned
  ont_waitlist.py                  # planned
  ...
  _common.py                       # shared helpers (snapshot dirs, SHA256, meta.json)

dbt_project/                       # silver + gold — normalization and marts
  models/staging/                  # one stg_<source>.sql per ingest
  models/marts/                    # fct_waitlist_country_year_organ.sql, etc.
  seeds/                           # country/organ/state code lookups
  tests/                           # not_null, unique, relationships

export/                            # final JSON artifacts
  build_artifacts.py

data/
  raw/{source}/{YYYY-MM-DD}/       # bronze snapshots (gitignored, meta.json committed)
  duckdb/                          # DuckDB working file (gitignored)
  exports/                         # final JSON for the portfolio (committed)

schemas/                           # JSON Schema for each export artifact
```

### Bronze

Each `ingest/<source>.py` is a self-contained Python module that:

1. Downloads raw bytes to `data/raw/<source>/<YYYY-MM-DD>/`.
2. Computes SHA256 + byte count + row count of each file.
3. Writes a `meta.json` sidecar with full provenance (`url`, `fetched_at`, `pipeline_version`, etc.).
4. Exits non-zero on any failure — silent partial runs are unacceptable.

Snapshots are immutable. To refresh, re-run with a new `--snapshot-date`.

### Silver

dbt staging models normalize each source's shape into a common contract:

```
(source, country_iso3, region_code?, organ, report_period_start, report_period_end,
 patients_waiting, demographic_breakdown_json?)
```

Country and organ names are mapped to canonical codes (`ISO 3166-1 alpha-3`, internal organ taxonomy) via `seeds/`. Every column with downstream consumers has `not_null` / `unique` / `accepted_values` / `relationships` tests asserted before any artifact is written.

### Gold

Two marts feed the portfolio:

- **`fct_waitlist_country_year_organ`** — one row per `(country, year, organ)`. The world view.
- **`fct_waitlist_mx_state_year_organ`** — Mexico drill-down (CENATRA-only). Per state × organ.

PMP rates (`patients_waiting_pmp`) require the population data already in `transplant-atlas`. To keep this repo self-contained, the relevant slices of World Bank / CONAPO are re-ingested here.

### Export

A single Python script queries gold, validates each output against a JSON Schema (draft 2020-12), asserts size limits, and writes:

- `world-waitlist.json` — country × year × organ.
- `mexico-waitlist.json` — state × year × organ.
- `definitions.json` — per-source definition string for the *"what does waitlist mean here?"* tooltip.
- `meta.json` — provenance.

These four files are the only output the portfolio consumes.

## Commands

```bash
make install   # editable install with dev extras
make ingest    # run all bronze ingest scripts for today
make build     # dbt deps + seed + run
make test      # dbt test + pytest
make export    # build final JSON artifacts
make all       # the full pipeline
make clean     # remove DuckDB + exports
```

Each ingest is also runnable standalone:

```bash
python -m ingest.cenatra_waitlist --snapshot-date 2026-05-19
python -m ingest.optn_waitlist    --snapshot-date 2026-05-19 --dry-run
```

## Roadmap

- [x] Repo scaffolding, shared bronze helpers, common Makefile
- [ ] CENATRA waitlist (México, CSV via CKAN — quarterly snapshots) — **in progress**
- [ ] OPTN/UNOS waitlist (US, public summary CSV)
- [ ] Silver staging for the two sources above
- [ ] Gold mart `fct_waitlist_country_year_organ`
- [ ] First export artifact + portfolio integration
- [ ] NHS BT (UK, PDF parsing)
- [ ] ONT (España, PDF parsing)
- [ ] Eurotransplant (PDF parsing, 8 countries)
- [ ] Scandiatransplant
- [ ] ANZDATA
- [ ] Optional: PMP rates against World Bank population

## License

MIT. See [LICENSE](LICENSE).

## Data attribution

Each source has its own attribution requirements; see `meta.json` for the URL of every snapshot. Use of the redistributed data must respect the original registries' terms.

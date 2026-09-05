# transplant-waitlist-atlas

Offline data pipeline that ingests organ-transplant **waiting list** statistics from multiple national registries, normalizes their heterogeneous schemas, and emits static JSON artifacts ready for visualization.

Companion project to [`transplant-atlas`](https://github.com/enriqew/transplant-atlas), which covers donation and transplantation rates. Where the atlas answers *"who is doing transplants?"*, this project answers *"who is waiting for one?"*.

The output is consumed by the [live dashboard](https://eredonda.com/projects/transplant-waitlist-atlas?utm_source=github&utm_medium=referral) on eredonda.com, but every artifact is plain JSON and can be reused by any client.

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
- Comparing 1:1 is **fundamentally limited**: the project's job is to make those caveats first-class, not paper over them.

## Architecture

Same medallion (bronze / silver / gold) shape as [`transplant-atlas`](https://github.com/enriqew/transplant-atlas), with DuckDB as the analytical engine and `dbt-duckdb` for SQL-first transformations.

```
ingest/                            # bronze, Python fetchers per source
  cenatra_waitlist.py              # México (CKAN, quarterly)
  optn_waitlist.py                 # US (OPTN/UNOS)
  nhsbt_waitlist.py                # UK
  ont_waitlist.py                  # España
  eurotransplant_waitlist.py       # Eurotransplant member states
  scandiatransplant_waitlist.py    # Nordic and Baltic countries
  anzdata_waitlist.py              # Australia and New Zealand
  _common.py                       # shared helpers (snapshot dirs, SHA256, meta.json)

dbt_project/                       # silver + gold, normalization and marts
  models/staging/                  # one stg_<source>.sql per ingest
  models/marts/                    # three facts + schema.yml with dbt tests

export/                            # final JSON artifacts
  build_artifacts.py

data/
  raw/{source}/{YYYY-MM-DD}/       # bronze snapshots (gitignored, meta.json committed)
  duckdb/                          # DuckDB working file (gitignored)
  exports/                         # final JSON for the portfolio (committed)
```

### Bronze

Each `ingest/<source>.py` is a self-contained Python module that:

1. Downloads raw bytes to `data/raw/<source>/<YYYY-MM-DD>/`.
2. Computes SHA256 + byte count + row count of each file.
3. Writes a `meta.json` sidecar with full provenance (`url`, `fetched_at`, `pipeline_version`, etc.).
4. Exits non-zero on any failure, silent partial runs are unacceptable.

Snapshots are immutable. To refresh, re-run with a new `--snapshot-date`.

### Silver

dbt staging models normalize each source's shape into a common contract:

```
(source, country_iso3, region_code?, organ, report_period_start, report_period_end,
 patients_waiting, demographic_breakdown_json?)
```

Country and organ names are mapped to canonical codes (`ISO 3166-1 alpha-3`, internal organ taxonomy) via `seeds/`. Every column with downstream consumers has `not_null` / `unique` / `accepted_values` / `relationships` tests asserted before any artifact is written.

### Gold

Three marts feed the portfolio:

- **`fct_waitlist_country_year_organ`**: one row per `(country, year, organ)`. The world view.
- **`fct_waitlist_mx_state_quarter_organ`**: Mexico drill-down (CENATRA only), per state, quarter and organ.
- **`fct_waitlist_removals`**: US removals by year, organ and reason. The fate distribution.

PMP rates (`patients_waiting_pmp`) require the population data already in `transplant-atlas`. To keep this repo self-contained, the relevant slices of World Bank / CONAPO are re-ingested here.

### Export

A single Python script queries gold, asserts a hard gzipped size cap per artifact (CloudFront serves gzipped, so that is the real wire size), and writes:

- `world-waitlist.json`: country × year × organ.
- `mexico-waitlist.json`: state × year × organ.
- `definitions.json`: per-source definition string for the *"what does waitlist mean here?"* tooltip.
- `us-fate-distribution.json`: US removals by year, organ and reason.
- `meta.json`: provenance.

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

## The OPTN snapshot is manual, and that has bitten once

OPTN bronze cannot be fetched unattended. The three CSVs come from the
[Build Advanced](https://optn.transplant.hrsa.gov/data/view-data-reports/build-advanced/)
UI, an interactive form, and an operator drops them into the snapshot directory.
On 2026-05-21 the long-form CSV was re-derived after those source files were
gone, so the run produced nothing for the United States: `us-fate-distribution.json`
was written as `[]` and `world-waitlist.json` lost its US rows. Both were
committed and published in that state.

Two things changed as a result.

The export now refuses to write when a bronze snapshot on disk declares rows
that do not reach the artifacts. It names the snapshot and the row count it
expected, and it checks before writing anything, so a partial run cannot leave
half the artifacts refreshed and the other half emptied. See
`tests/test_export_guard.py`.

The artifacts themselves were restored from this pipeline's own earlier output
rather than re-derived, because the bronze that produced them no longer exists.
`data/exports/meta.json` carries a `provenance_note` saying so: every source
except OPTN is from the 2026-05-21 run, the OPTN-derived rows are older. To
return the export to a single coherent run, re-download the three CSVs named in
`data/raw/optn_waitlist/2026-05-19/meta.json` and run `make all`.

### Runbook: re-taking the OPTN snapshot

Open <https://optn.transplant.hrsa.gov/data/view-data-reports/build-advanced/>. It is an
interactive report builder, so there is no URL to script. Build these three reports, one at a
time, and use its CSV download on each. The exact queries are also recorded in every snapshot's
`meta.json` under `build_queries`.

| # | Category | Columns | Rows | Measure |
|---|---|---|---|---|
| 1 | Waiting List | Organ (all 19) | Waiting List Status (all 70) | Candidates |
| 2 | Waiting List Additions | Organ | List Year, Waiting List Status at Listing | Candidates |
| 3 | Waiting List Removals | Organ | Removal Year, Removal Reason, Waiting List Status at Removal | Candidates |

Report 1 has no year axis; OPTN does not publish one for the current snapshot. Reports 2 and 3 go
back to about 1988.

Save them under `data/raw/optn_waitlist/<YYYY-MM-DD>/` using today's date, keeping the filenames
OPTN gives them. The ingest checks for these three names exactly:

```
Waitlist___Organ_by_Waiting_List_Status.csv
Waitlist_Additions___Organ_by_List_Year,_Waiting_List_Status_at_Listing.csv
Waitlist_Removals___Organ_by_Removal_Year,_Removal_Reason,_Waiting_List_Status_at_Removal.csv
```

Then register and rebuild. Note the `data-as-of` date OPTN shows on the page, which is not the
day you downloaded:

```bash
python -m ingest.optn_waitlist --snapshot-date <YYYY-MM-DD>   # hashes the files, writes meta.json
make build export                                             # flatten, dbt, artifacts
```

The export refuses to write if the new snapshot declares rows that do not reach the artifacts, so
a bad download fails loudly rather than emptying the map. Copy the resulting
`data/exports/*.json` into the portfolio's `src/data/transplant-waitlist-atlas/` and drop the
`provenance_note` from `meta.json`, since the run is coherent again.

A third thing was wrong on the path that instruction takes. `make build` ran dbt
without `TRANSPLANT_WAITLIST_RAW_ROOT`, so the staging views were compiled with the
relative default `../data/raw`, which resolves only while the query runs from
`dbt_project/`. The export queries those same views from the repo root, so it died
with a DuckDB IO error naming a directory nobody had written to, which reads like a
missing file rather than a compiled-in path. Both targets now share one absolute
`DBT_ENV`.

## Roadmap

- [x] Repo scaffolding, shared bronze helpers, common Makefile
- [x] CENATRA waitlist (México, CSV via CKAN, quarterly snapshots)
- [x] OPTN/UNOS waitlist and removals (US)
- [x] Silver staging per source
- [x] Gold marts and first export artifacts, wired into the portfolio
- [x] NHS BT (UK)
- [x] ONT (España)
- [x] Eurotransplant
- [x] Scandiatransplant
- [x] ANZDATA
- [ ] JSON Schema validation of each artifact (the `jsonschema` dependency is
      declared but the export only enforces size caps today)
- [ ] PMP rates against World Bank population

## License

MIT. See [LICENSE](LICENSE).

## Data attribution

Every snapshot records its source URL and SHA256 in `meta.json`. Redistribution
of the aggregates must respect each registry's own terms.

**OPTN/UNOS.** HRSA requires the following acknowledgment, reproduced verbatim:

> *This work was supported in part by Health Resources and Services Administration contract HHSH250-2019-00001C. The content is the responsibility of the authors alone and does not necessarily reflect the views or policies of the Department of Health and Human Services, nor does mention of trade names, commercial products, or organizations imply endorsement by the U.S. Government.*

The contract number is the one current for this dataset's snapshot. If the
pipeline is refreshed against a newer OPTN contract period, check HRSA's
[citing data](https://www.hrsa.gov/optn/data/view-data-reports/citing-data) page
and update it.

**Other registries.** CENATRA (Mexico) publishes under Libre Uso MX, open reuse
with attribution. ONT (Spain), Eurotransplant and Scandiatransplant publish no
explicit reuse terms, so their figures are treated as cited aggregates pending
clarification.

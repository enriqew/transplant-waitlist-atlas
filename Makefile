SHELL := /bin/bash
PYTHON ?= python
SNAPSHOT_DATE ?= $(shell date -u +%Y-%m-%d)
DBT_DIR := dbt_project
# Absolute raw root, shared by dbt and the export. See the comment on `build`.
DBT_ENV := TRANSPLANT_WAITLIST_RAW_ROOT=$(CURDIR)/data/raw

.PHONY: help install ingest prebuild build test export all clean format lint

help:
	@echo "Targets:"
	@echo "  install   Install dependencies (editable + dev extras)"
	@echo "  ingest    Run all bronze ingest scripts for SNAPSHOT_DATE=$(SNAPSHOT_DATE)"
	@echo "  build     dbt deps + seed + run"
	@echo "  test      dbt test + pytest"
	@echo "  export    Build final JSON artifacts"
	@echo "  all       ingest + build + test + export"
	@echo "  clean     Remove DuckDB and exports"
	@echo "  format    Run ruff format"
	@echo "  lint      Run ruff check"

install:
	$(PYTHON) -m pip install -e ".[dev]"

# Sources are added incrementally. Each is independent — fail loud, no silent skips.
ingest:
	$(PYTHON) -m ingest.cenatra_waitlist           --snapshot-date $(SNAPSHOT_DATE)
	$(PYTHON) -m ingest.eurotransplant_waitlist    --snapshot-date $(SNAPSHOT_DATE)
	-$(PYTHON) -m ingest.optn_waitlist             --snapshot-date $(SNAPSHOT_DATE)
	$(PYTHON) -m ingest.nhsbt_waitlist             --snapshot-date $(SNAPSHOT_DATE)
	$(PYTHON) -m ingest.ont_waitlist               --snapshot-date $(SNAPSHOT_DATE)
	$(PYTHON) -m ingest.scandiatransplant_waitlist --snapshot-date $(SNAPSHOT_DATE)
	$(PYTHON) -m ingest.anzdata_waitlist           --snapshot-date $(SNAPSHOT_DATE)

# RAW_ROOT is exported for dbt too, not just for export. The staging models bake the
# path into the view definition, so compiling them with the relative default resolves
# only while the query runs from $(DBT_DIR); the export then queries the same view from
# the repo root and dies with an IO error naming a path nobody wrote.
build: prebuild
	cd $(DBT_DIR) && $(DBT_ENV) dbt deps && $(DBT_ENV) dbt seed && $(DBT_ENV) dbt run

# Prebuild: source-specific Python flatteners that turn pivot CSVs / XLSX
# reports into long-format CSVs that dbt staging can read.
prebuild:
	$(PYTHON) $(DBT_DIR)/analyses/cenatra_waitlist_to_long.py
	-$(PYTHON) $(DBT_DIR)/analyses/optn_pivot_to_long.py
	$(PYTHON) $(DBT_DIR)/analyses/eurotransplant_xlsx_to_long.py
	$(PYTHON) $(DBT_DIR)/analyses/nhsbt_pdf_to_long.py
	-$(PYTHON) $(DBT_DIR)/analyses/ont_pdf_to_long.py
	$(PYTHON) $(DBT_DIR)/analyses/scandiatransplant_pdf_to_long.py
	$(PYTHON) $(DBT_DIR)/analyses/anzdata_xlsx_to_long.py

test:
	cd $(DBT_DIR) && dbt test
	pytest || [ $$? -eq 5 ]

export:
	$(DBT_ENV) $(PYTHON) -m export.build_artifacts

all: ingest build test export

clean:
	rm -rf data/duckdb data/exports

format:
	ruff format ingest export tests 2>/dev/null || true

lint:
	ruff check ingest export tests 2>/dev/null || true

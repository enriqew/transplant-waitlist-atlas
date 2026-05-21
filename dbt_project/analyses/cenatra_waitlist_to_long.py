"""Silver prebuild: CENATRA quarterly patient-level CSVs → long aggregate CSV.

CENATRA publishes one CSV per quarter (`pacientes_espera_organo_tejido`), each
containing one row per patient currently registered to wait for an organ at the
end of that quarter. Schema is *tall* already; the job here is to:

  1. Read all 22 quarterly snapshots (2020-Q1 through 2025-Q4).
  2. Extract reporting year + quarter from the filename
     (CENATRA does not encode the period in any column).
  3. Map Spanish organ labels to the canonical taxonomy
     (`riñón → kidney`, `córnea → cornea`, ...). Casing drifts between years
     (lowercase 2020-2024, mixed case in 2025); UPPER + accent-preserving lookup.
  4. Map state codes (`codigo_entidad_federativa_residencia_paciente`) using the
     ISO 3166-2:MX canonical codes seed. Use residence when present, else fall
     back to establishment (mirrors atlas's `stg_cenatra_waitlist`).
  5. Aggregate COUNT(*) by (year, quarter, state, organ).
  6. Emit `cenatra_long.csv` ready for dbt staging.

Output schema:

    metric_type      stock (CENATRA only publishes end-of-quarter snapshots)
    country_iso3     'MEX' constant
    region_code      ISO 3166-2:MX state code (e.g. 'AGU', 'JAL', 'CMX')
    year             integer (2020-2025)
    quarter          1-4
    organ            canonical code: kidney, liver, heart, lung, pancreas,
                     kidney_pancreas, cornea, bone_marrow, other_tissue
    patients         COUNT(*) of patients in that slice

Documented data quirks (see project_transplant_atlas_data_quirks memory):
  - State sentinels 97/99 = "No Disponible" → treat as NULL.
  - Reporting period only in filename (regex `(1er|2do|3er|4to)Trimestre(\\d{4})`).
  - Organ casing inconsistent across years; UPPER before lookup.
"""

from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = REPO_ROOT / "data" / "raw" / "cenatra_waitlist__pacientes_espera_organo_tejido"

# Two naming conventions in the wild:
#   1erTrimestre2025.csv             (camelCase, no separators)
#   (1er_trimestre_2025).csv         (lowercase, underscore-separated)
QUARTER_RE = re.compile(r"(1er|2do|3er|4to)[ _]?trimestre[ _]?(\d{4})", re.IGNORECASE)
QUARTER_MAP = {"1er": 1, "2do": 2, "3er": 3, "4to": 4}

# Mexican state codes (cve_geo INT → ISO 3166-2:MX 3-letter). 32 entries.
# Mirrors transplant-atlas's mx_state_codes seed.
CVE_GEO_TO_STATE_CODE: dict[int, str] = {
    1: "AGU", 2: "BCN", 3: "BCS", 4: "CAM", 5: "COA", 6: "COL", 7: "CHP", 8: "CHH",
    9: "CMX", 10: "DUR", 11: "GUA", 12: "GRO", 13: "HID", 14: "JAL", 15: "MEX",
    16: "MIC", 17: "MOR", 18: "NAY", 19: "NLE", 20: "OAX", 21: "PUE", 22: "QUE",
    23: "ROO", 24: "SLP", 25: "SIN", 26: "SON", 27: "TAB", 28: "TAM", 29: "TLA",
    30: "VER", 31: "YUC", 32: "ZAC",
}

# Spanish organ → canonical code. CENATRA casing varies across releases, so we
# UPPER the input before lookup. Accent marks are *preserved* (the data has
# both `RIÑÓN` and `RIÑON` and `RINON` — we handle all three).
ORGAN_TRANSLATIONS: dict[str, str] = {
    "RIÑÓN": "kidney",
    "RIÑON": "kidney",
    "RINON": "kidney",
    "RIÑÓN-RIÑÓN": "kidney",
    "HÍGADO": "liver",
    "HIGADO": "liver",
    "HÍGADO-RIÑÓN": "liver",
    "CORAZÓN": "heart",
    "CORAZON": "heart",
    "CORAZÓN-RIÑÓN": "heart",
    "PULMÓN": "lung",
    "PULMON": "lung",
    "PÁNCREAS": "pancreas",
    "PANCREAS": "pancreas",
    "CÓRNEA": "cornea",
    "CORNEA": "cornea",
    "MÉDULA ÓSEA": "bone_marrow",
    "MEDULA OSEA": "bone_marrow",
    "C.P.H.": "bone_marrow",
    "CPH": "bone_marrow",
    "RIÑÓN-PÁNCREAS": "kidney_pancreas",
    "RINON-PANCREAS": "kidney_pancreas",
    "HUESO": "other_tissue",
    "HUESOS": "other_tissue",
    "PIEL": "other_tissue",
    "VÁLVULA CARDIACA": "other_tissue",
    "VÁLVULAS": "other_tissue",
    "TEJIDO CARDIOVASCULAR (VÁLVULAS)": "other_tissue",
    "TEJIDO MUSCULOESQUELÉTICO": "other_tissue",
    "INTESTINO": "intestine",
    "PARATIROIDES": "other_tissue",
    "BANCO CORNEAS": "cornea",
    # Combined organs — map to the closest canonical category (mirrors OPTN's
    # multi-organ rollups). CORAZÓN-PULMÓN is the dedicated heart-lung block;
    # PULMÓN-PULMÓN (bilateral) is still a lung waitlist.
    "CORAZÓN-PULMÓN": "heart_lung",
    "PULMÓN-PULMÓN": "lung",
    # VCA — Vascularised Composite Allotransplantation (face, hand, limbs).
    # Same canonical bucket as OPTN's six VCA columns.
    "CARA": "vca",
    "MANO": "vca",
    "EXTREMIDADES": "vca",
}

# Sentinels CENATRA uses for "No Disponible" on entity codes.
STATE_SENTINELS = {97, 99}


def _latest_snapshot_dir() -> Path:
    if not RAW_ROOT.exists():
        raise SystemExit(f"no CENATRA waitlist bronze under {RAW_ROOT}")
    candidates = sorted(p for p in RAW_ROOT.iterdir() if p.is_dir())
    if not candidates:
        raise SystemExit(f"no dated subdirectories under {RAW_ROOT}")
    # Walk newest-first; return first dir that has at least one quarterly CSV.
    for candidate in reversed(candidates):
        csvs = [p for p in candidate.glob("*.csv") if not p.name.endswith("_long.csv")]
        if csvs:
            return candidate
    raise SystemExit(
        f"no CENATRA quarterly CSVs found under any snapshot in {RAW_ROOT}; "
        "run `python -m ingest.cenatra_waitlist --force` to re-download"
    )


def _period_from_filename(name: str) -> tuple[int, int] | None:
    """Extract (year, quarter) from `..._1erTrimestre_2025.csv` style names."""

    m = QUARTER_RE.search(name)
    if not m:
        return None
    quarter = QUARTER_MAP.get(m.group(1).lower())
    if quarter is None:
        return None
    return int(m.group(2)), quarter


def _safe_state(raw_residence: str, raw_establishment: str) -> str | None:
    """Pick residence cve_geo when present (preferred — reflects demand), else
    establishment. Skip sentinels (97/99) and out-of-range values."""

    for raw in (raw_residence, raw_establishment):
        try:
            cve = int(raw)
        except (TypeError, ValueError):
            continue
        if cve in STATE_SENTINELS:
            continue
        code = CVE_GEO_TO_STATE_CODE.get(cve)
        if code:
            return code
    return None


def main() -> int:
    snapshot = _latest_snapshot_dir()
    # Skip prebuild output (`cenatra_long.csv`) when re-running — only consume
    # CENATRA bronze CSVs.
    csvs = sorted(p for p in snapshot.glob("*.csv") if not p.name.endswith("_long.csv"))
    if not csvs:
        print(f"ERROR: no CSVs in {snapshot}", file=sys.stderr)
        return 1

    counts: Counter[tuple[int, int, str, str]] = Counter()
    files_processed = 0
    rows_seen = 0
    rows_dropped_no_period = 0
    rows_dropped_no_state = 0
    rows_dropped_no_organ = 0
    unknown_organs: Counter[str] = Counter()

    for csv_path in csvs:
        period = _period_from_filename(csv_path.name)
        if period is None:
            print(f"WARN: cannot extract period from {csv_path.name}", file=sys.stderr)
            rows_dropped_no_period += 1
            continue
        year, quarter = period

        with csv_path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows_seen += 1

                # State: residence preferred, establishment fallback. Sentinel-aware.
                state = _safe_state(
                    row.get("codigo_entidad_federativa_residencia_paciente", ""),
                    row.get("codigo_entidad_federativa_establecimiento", ""),
                )
                if state is None:
                    rows_dropped_no_state += 1
                    continue

                organ_raw = (row.get("organo") or "").strip().upper()
                organ = ORGAN_TRANSLATIONS.get(organ_raw)
                if organ is None:
                    rows_dropped_no_organ += 1
                    unknown_organs[organ_raw] += 1
                    continue

                counts[(year, quarter, state, organ)] += 1
        files_processed += 1

    out_path = snapshot / "cenatra_long.csv"
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["metric_type", "country_iso3", "region_code", "year", "quarter", "organ", "patients"]
        )
        for (year, quarter, state, organ), n in sorted(counts.items()):
            writer.writerow(["stock", "MEX", state, year, quarter, organ, n])

    print(f"wrote {len(counts):,} aggregate rows → {out_path}")
    print(
        f"  files={files_processed}  patient_rows_seen={rows_seen:,}  "
        f"dropped_no_state={rows_dropped_no_state:,}  "
        f"dropped_no_organ={rows_dropped_no_organ:,}"
    )
    if unknown_organs:
        print(f"  unknown organ values (top 10): {unknown_organs.most_common(10)}")
    return 0 if counts else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
#
# Full ETL: transform the raw Dorich exports into one normalized CSV per
# n2odb table, then load them into the Django MySQL (the `db` service in the
# fluxapp devcontainer).
#
# Run inside the DorichScraper devcontainer:
#     ./etl.sh
#
# The load step shells out to a Django management command in the mounted
# fluxapp project, which reads fluxapp/.env (DATABASE_URL -> host `db`).
#
set -euo pipefail

SCRAPER_DIR="${SCRAPER_DIR:-/workspaces/DorichScraper}"
FLUXAPP_DIR="${FLUXAPP_DIR:-/workspaces/fluxapp}"

# Directory the transform writes the per-table CSVs to (Publication.csv,
# Site.csv, Experiment.csv, Treatment.csv, RawMeasurementTreatment.csv).
TABLES_DIR="${TABLES_DIR:-DorichData/cleaned}"

# Django management command that loads the per-table CSVs. There is NO safe
# default: `import_rawdata` expects a single denormalized RawData.csv and was
# written for a different project, so a per-table loader must be chosen
# explicitly to avoid loading the wrong way:
#     LOAD_CMD=load_tables ./etl.sh
LOAD_CMD="${LOAD_CMD:-}"

echo "== [1/2] Transform: DorichData/ raw exports -> per-table CSVs in $TABLES_DIR =="
cd "$SCRAPER_DIR"
python build_tables.py --data DorichData --out "$TABLES_DIR"

if [[ -z "$LOAD_CMD" ]]; then
    echo "== [2/2] Load: SKIPPED — no loader chosen." >&2
    echo "   The transform produced normalized per-table CSVs in $TABLES_DIR." >&2
    echo "   Set LOAD_CMD to a management command that loads them in FK order" >&2
    echo "   (Publication, Site, Experiment, Treatment, RawMeasurementTreatment), e.g." >&2
    echo "   LOAD_CMD=load_tables ./etl.sh" >&2
    exit 0
fi

echo "== [2/2] Load: $TABLES_DIR/*.csv -> Django MySQL via 'manage.py $LOAD_CMD' =="
cd "$FLUXAPP_DIR"
python manage.py "$LOAD_CMD" "$SCRAPER_DIR/$TABLES_DIR" "$@"

echo "ETL complete."

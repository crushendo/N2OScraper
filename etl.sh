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

# Django management command that loads the per-table CSVs in FK order. The load
# is OPT-IN so you can inspect the transformed CSVs first. The loader is
# `load_tables`: it writes an undo manifest and is reversible with
# `--undo <manifest>`. Dry-run first, then load for real:
#     LOAD_CMD=load_tables ./etl.sh --dry-run        # parse + roll back
#     LOAD_CMD=load_tables ./etl.sh                  # commit + write manifest
LOAD_CMD="${LOAD_CMD:-}"

# Fetch current MAX(PK) of each table from the live DB so generated surrogate
# keys start above them and don't collide on load. Uses the mounted Django app
# (reads fluxapp/.env -> DATABASE_URL). Empty/unset tables yield 0.
echo "== [1/3] Reading existing key offsets from the DB =="
cd "$FLUXAPP_DIR"
OFFSETS=$(DJANGO_SETTINGS_MODULE=fluxapp.settings python -c "
import django; django.setup()
from django.db.models import Max
from n2odb.models import Publication, Site, Experiment, Treatment, RawMeasurementTreatment
mx=lambda m: m.objects.aggregate(x=Max('pk'))['x'] or 0
print(mx(Publication), mx(Site), mx(Experiment), mx(Treatment), mx(RawMeasurementTreatment))
")
read PUB SITE EXP TRT RAW <<< "$OFFSETS"
echo "   offsets: Publication=$PUB Site=$SITE Experiment=$EXP Treatment=$TRT RawMeasurementTreatment=$RAW"

echo "== [2/3] Transform: DorichData/ raw exports -> per-table CSVs in $TABLES_DIR =="
cd "$SCRAPER_DIR"
python build_tables.py --data DorichData --out "$TABLES_DIR" \
    --pub-offset "$PUB" --site-offset "$SITE" --exp-offset "$EXP" \
    --trt-offset "$TRT" --raw-offset "$RAW"

if [[ -z "$LOAD_CMD" ]]; then
    echo "== [3/3] Load: SKIPPED — no loader chosen." >&2
    echo "   The transform produced normalized per-table CSVs in $TABLES_DIR." >&2
    echo "   Set LOAD_CMD to a management command that loads them in FK order" >&2
    echo "   (Publication, Site, Experiment, Treatment, RawMeasurementTreatment), e.g." >&2
    echo "   LOAD_CMD=load_tables ./etl.sh" >&2
    exit 0
fi

echo "== [3/3] Load: $TABLES_DIR/*.csv -> Django MySQL via 'manage.py $LOAD_CMD' =="
cd "$FLUXAPP_DIR"
python manage.py "$LOAD_CMD" "$SCRAPER_DIR/$TABLES_DIR" "$@"

echo "ETL complete."

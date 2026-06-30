# DorichScraper — Dorich Global N₂O → n2odb transform

Transforms the raw [Dorich Global N₂O EDI data package](https://portal.edirepository.org/)
(`edi.877`) into one normalized CSV per `n2odb` table, ready to load into the
**fluxapp** Django MySQL. The heavy lifting is `build_tables.py`; `etl.sh` chains
the transform + load.

> `dorich_scraper.py` is **superseded** (it only produced a treatment-level
> metadata stub and discarded the daily data). Use `build_tables.py`.

## Inputs (`DorichData/`)

| File | Role |
|---|---|
| `DailyGHG_V1.csv` | **Spine** — daily flux + environmental measurements (~549k rows). Git-ignored (117 MB; fetch from EDI). |
| `Sitelibrary_V1.csv` | Site/experiment library — enrichment join. |
| `Summary_V1.csv` | Per-treatment summary (MAP/MAT, bulk density, soil C, pub year). |
| `source_overrides.csv` | Manual `ref → source` overrides for citation/source resolution. |

## Output (`DorichData/cleaned/`)

One CSV per table, with **db_column** headers (CamelCase): `Publication.csv`,
`Site.csv`, `Experiment.csv`, `Treatment.csv`, `RawMeasurementTreatment.csv`.

## Run

```bash
pip install -r requirements.txt          # stdlib-only transform; no pandas needed

# transform only (no offsets, no DB) — for inspection
python build_tables.py --data DorichData --out DorichData/cleaned --no-citations

# full transform + load (in the devcontainer; see "Container" below)
LOAD_CMD=load_tables ./etl.sh --dry-run   # read DB offsets, transform, dry-run load
LOAD_CMD=load_tables ./etl.sh             # commit
```

Useful `build_tables.py` flags: `--no-citations` (skip DOI/network resolution),
`--keep-gracenet` (don't drop USDA GRACEnet refs), `--allow-unsourced`,
`--find-sources` (write `source_candidates.csv` for review), `--flux-replicates N`
(SE = SD/√N, default 3), and `--pub-offset/--site-offset/--exp-offset/
--trt-offset/--raw-offset` (surrogate-key offsets).

## What `build_tables.py` does

- **Spine join**: `DailyGHG.SiteID == Sitelibrary.Reference` (the experiment),
  **case-insensitive**. Every `(Reference, Treatment)` becomes a Treatment so no
  measurements are dropped; library + Summary only enrich.
- **Treatment-averaged**: the kept daily data is already one row per
  (treatment, day) with `n2osd` = across-replicate SD. `FluxStandardError` is
  derived as `SD/√n` (`--flux-replicates`).
- **No date gaps**: each treatment is emitted as a continuous daily series
  (synthetic gap-days carry date/DOY, `NitrogenApplied=0`, nulls elsewhere),
  sorted by TreatmentID then Date.
- **Derived columns**: `VWCCalculated`/`WFPSCalculated` via
  porosity `= 1 − BD/2.65`; `NitrogenForm` (granular) → `NitrogenType` (broad
  OHE bucket); `Precip` = rain + irrigation, with `IrrigationApplied` separate;
  `NitrogenApplied` is never null (0 or the measured rate).
- **GRACEnet drop**: USDA GRACEnet refs (all-caps site codes + `mnrsmt`,
  `sdaltrot`) and unpublished refs are dropped (already ETL'd separately);
  `--keep-gracenet` disables this.
- **Sources / citations**: resolves each surviving ref to a verified source
  (override > library paper > DOI > metacat); citations via doi.org content
  negotiation, cached to `DorichData/.citation_cache.json`.
- **Surrogate keys**: PKs/FKs are offset (via the `--*-offset` flags) so they
  start above the target DB's current `MAX(pk)` and don't collide on append.

## Container (VSCode "Open in Container")

A devcontainer keeps the data-science stack off your base env and loads into the
**one** fluxapp Django MySQL (no second DB).

1. Start the **fluxapp** devcontainer first (creates the external `n2o_net`
   network and the `db` host). If neither project is up on a fresh machine:
   `docker network create n2o_net`.
2. Open this folder in VSCode → **Reopen in Container**.
3. Run `LOAD_CMD=load_tables ./etl.sh` (dry-run first).

`etl.sh` reads the live `MAX(pk)` per table via the mounted fluxapp app, passes
them as `--*-offset`, transforms, then loads via `manage.py load_tables`
(reversible — see `fluxapp/ETL/README.md`).

## Git / data hygiene

`DailyGHG_V1.csv` (117 MB) and `.citation_cache.json` are git-ignored. The
`cleaned/*.csv` outputs are versioned as load provenance; their
`load_manifest_*.json` is the undo record for that load.

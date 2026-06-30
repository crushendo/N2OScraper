dorich_scraper
================

A small utility to read CSV files in `DorichData/`, clean them, write cleaned CSVs to `DorichData/cleaned/` and store tables in a SQLite DB.

Quickstart
----------

Install requirements:

```bash
pip install -r requirements.txt
```

Run the cleaner (defaults assume the workspace layout provided):

```bash
python dorich_scraper.py --input DorichData --output DorichData/cleaned --db DorichData/dorich.db
```

Notes
-----
- Cleaned CSVs are written with `.clean.csv` suffix.
- Each CSV is written to a table in the SQLite DB; table names are derived from file stems.
- If `sqlalchemy` is not installed the script will still create cleaned CSVs but will skip DB writes.

Run in a container (VSCode "Open in Container")
-----------------------------------------------

This repo ships a devcontainer that keeps the data-science stack off your base
environment and loads the transformed CSVs into the **fluxapp** Django MySQL —
no second database is created.

Architecture:

- The scraper runs in its own container (`.devcontainer/`), separate from the
  lean Django web image.
- It joins a shared Docker network (`n2o_net`) created by the fluxapp
  devcontainer, so it reaches the MySQL container as host `db`.
- The fluxapp project is mounted at `/workspaces/fluxapp`, so the load step runs
  a Django management command using fluxapp's own `.env` / settings.

Steps:

1. Start the **fluxapp** devcontainer first (or `docker compose -f
   fluxapp/.devcontainer/docker-compose.yml up -d db`). This creates the
   `n2o_net` network and the `db` host.
2. Open this folder in VSCode → "Reopen in Container".
3. Run the full ETL:

   ```bash
   ./etl.sh
   ```

   - Step 1 transforms `DorichData/` → cleaned CSVs.
   - Step 2 loads into MySQL, but only if you pick a loader — there is no
     default, because `import_rawdata` belongs to a separate process and was
     not designed for this job (it may still be reusable):

     ```bash
     LOAD_CMD=import_rawdata LOAD_CSV=ETL/RawData.csv ./etl.sh
     ```

# DorichScraper ETL devcontainer.
# Transforms raw CSVs (pandas/numpy/scipy/sklearn stack) and can run the
# Django management command that loads the result into the fluxapp MySQL.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspaces/DorichScraper

# System deps: build toolchain + MySQL client headers so `mysqlclient`
# (installed via the mounted fluxapp requirements at postCreate) can compile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        pkg-config \
        default-libmysqlclient-dev \
        git \
    && rm -rf /var/lib/apt/lists/*

# The scraper's own data-science stack. Django + DB deps are installed at
# postCreate from the mounted fluxapp project so versions match the app exactly.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

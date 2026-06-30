#!/usr/bin/env python3
"""build_tables.py

Transform the Dorich raw exports into one normalized CSV per n2odb table,
complete with surrogate keys and the full daily flux / environmental record.

Outputs (to --out, default DorichData/cleaned/):
    Publication.csv
    Site.csv
    Experiment.csv
    Treatment.csv
    RawMeasurementTreatment.csv   <- the daily-level measurements (the bulk)

Join model (discovered by profiling the data):
    * DailyGHG_V1.SiteID  == Sitelibrary.Reference   (the *experiment*), NOT
      Sitelibrary.SiteID (which is the place name -> Site.SiteName).
    * Joins are CASE-INSENSITIVE: lowercasing recovers ~3x more matches
      (e.g. daily 'gelfand_2016' vs library 'Gelfand_2016').

The DAILY file is the spine: every (Reference, Treatment) seen in the daily
data becomes a Treatment, so no measurements are dropped. The site library and
Summary file only *enrich* (coordinates, paper, texture, MAP/MAT); where a
reference is absent from the library, metadata is left null and the Site falls
back to a (0, 0) placeholder so the NOT NULL FKs still resolve.

Usage:
    python build_tables.py --data DorichData --out DorichData/cleaned
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path

csv.field_size_limit(1 << 24)

_NULLISH = {"", "na", "nan", "none", "null", "n/a"}


# --------------------------------------------------------------------------- #
# Coercion helpers
# --------------------------------------------------------------------------- #
def s(v):
    """Clean string -> str or None."""
    if v is None:
        return None
    v = str(v).strip()
    return None if v.lower() in _NULLISH else v


def f(v):
    """-> float or None."""
    v = s(v)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def lc(v):
    """Normalized (case/space-insensitive) key."""
    return (v or "").strip().lower()


def clip(v, n):
    v = s(v)
    return None if v is None else v[:n]


def to_int_round(v):
    """-> nearest int or None (source may be a float string like '15.24')."""
    x = f(v)
    return None if x is None else int(round(x))


def temp(v):
    """Temperature in C, nulling physically implausible sentinels.

    The source carries missing-value sentinels (tavg=999999, soilt=-100) and a
    few unit-error spikes; anything outside [-60, 60] C is treated as missing.
    """
    x = f(v)
    return x if (x is not None and -60.0 <= x <= 60.0) else None


def frac(v):
    """WFPS/VWC sometimes arrive as percent; express as 0-1 fraction."""
    x = f(v)
    if x is None:
        return None
    return x / 100.0 if x > 1 else x


def parse_date(v):
    """Daily dates are M/D/YYYY (e.g. 1/1/2009). -> (iso, doy) or (None, None)."""
    v = s(v)
    if v is None:
        return None, None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(v, fmt).date()
            return d.isoformat(), d.timetuple().tm_yday
        except ValueError:
            continue
    return None, None


def management_of(n_type, trt_desc):
    """Classify Management from the library's 'N type' / treatment description."""
    nt, td = lc(n_type), lc(trt_desc)
    if not nt:
        return "Zero Input"
    if "urine" in nt or "slurry" in nt:
        return "Organic"
    if "no-till" in td or "no tillage" in td:
        return "No-till"
    return "Conventional"


# Standard particle density (g/cm3) for porosity in the WFPS <-> VWC conversion.
PARTICLE_DENSITY = 2.65

_AUTHOR_RE = re.compile(r"^([A-Z][a-zA-Z'-]{2,})[._-].*\d{4}")


def lead_author(reference):
    """Best-effort first author from an 'Author_Year' style reference code.

    Conservative: only fires when the reference starts with a capitalized word
    AND carries a 4-digit year (e.g. 'Gelfand_2011' -> 'Gelfand',
    'Oates_Arlington_2015' -> 'Oates'). Returns None for acronym site codes
    ('AARS', 'MNMOFS') or year-less names ('China_CS') to avoid false hits.
    """
    m = _AUTHOR_RE.match((reference or "").strip())
    return m.group(1) if m else None


def looks_usda_code(ref):
    """True for all-caps alphanumeric site codes (USDA GRACEnet style: 'MNMOFS',
    'COFOARD1', 'AARS'). False for author/dataset refs ('De_Rosa_2018', 'bel.18').
    """
    ref = (ref or "").strip()
    return bool(ref) and re.fullmatch(r"[A-Z0-9]+", ref) is not None


# Mixed-case refs the all-caps heuristic misses but the user confirmed are
# already-ETL'd USDA GRACEnet (lowercased for matching).
GRACENET_REFS = {"mnrsmt", "sdaltrot"}

_UNPUB_RE = re.compile(
    r"in[\s-]?process|on[\s-]?process|in[\s-]?prep|unpublished|submitted|"
    r"under[\s-]?review|forthcoming", re.I)


def is_unpublished(text):
    """Library 'paper' field holding a status note rather than a real reference."""
    return bool(text) and _UNPUB_RE.search(text) is not None


def fix_allcaps(text):
    """Title-case ALL-CAPS author blocks that arrive uppercase from publisher
    metadata (e.g. 'WAGNER-RIDDLE, C., FURON, A.'). Gated on >=3 all-caps words
    so ordinary citations with a stray acronym are left untouched; digit-bearing
    tokens like 'CO2'/'N2O' are never matched.
    """
    if not text or len(re.findall(r"\b[A-Z]{2,}\b", text)) < 3:
        return text
    return re.sub(r"\b[A-Z]{2,}\b", lambda m: m.group(0).title(), text)


# --------------------------------------------------------------------------- #
# DOI -> full citation. The raw files only carry a DOI; the DB stores a proper
# title + bibliographic citation, so resolve the DOI via doi.org content
# negotiation (APA bibliography + CSL-JSON metadata). Cached to disk.
# --------------------------------------------------------------------------- #
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")


def _extract_doi(raw):
    if not raw:
        return None
    m = _DOI_RE.search(raw)
    return m.group(0).rstrip(").,;]") if m else None


def _clean_text(t):
    """Decode HTML entities (incl. APA-title-cased '&Amp;') and collapse any
    embedded newlines/runs of whitespace to single spaces."""
    if not t:
        return None
    t = re.sub(r"&amp;", "&", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", html.unescape(t))
    return t.strip() or None


def _doi_get(url, accept, timeout=20):
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": "DorichScraper/1.0 (n2o ETL; mailto:ryanackett@gmail.com)",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _scrape_doi(url):
    """Fetch a repository/article page and pull an embedded DOI (publisher pages
    expose <meta name='citation_doi'> or a doi.org link), else None."""
    try:
        page = _doi_get(url, "text/html")
    except Exception:
        return None
    m = (re.search(r'citation_doi"\s+content="(10\.[^"]+)"', page)
         or re.search(r'doi\.org/(10\.[^\s"\'<>]+)', page))
    return m.group(1).rstrip('".,;)') if m else None


def resolve_doi(raw, cache):
    """Return {citation,title,author,year,link} for a DOI string, else None.

    Network failures degrade gracefully (None); results (incl. misses) are
    cached by DOI so reruns are instant and don't re-hit the network.
    """
    doi = _extract_doi(raw)
    if not doi:
        return None
    if doi in cache:
        return cache[doi]
    url = "https://doi.org/" + doi
    out = {"link": url, "citation": None, "title": None, "author": None, "year": None}
    try:
        apa = _doi_get(url, "text/x-bibliography; style=apa; charset=utf-8")
        out["citation"] = _clean_text(" ".join(apa.split()))
    except Exception:
        pass
    try:
        meta = json.loads(_doi_get(url, "application/vnd.citationstyles.csl+json"))
        t = meta.get("title")
        out["title"] = _clean_text(t[0] if isinstance(t, list) else t)
        authors = meta.get("author") or []
        if authors:
            out["author"] = authors[0].get("family") or authors[0].get("name")
        parts = (meta.get("issued") or {}).get("date-parts") or []
        if parts and parts[0]:
            out["year"] = parts[0][0]
    except Exception:
        pass
    cache[doi] = out if (out["citation"] or out["title"]) else None
    time.sleep(0.15)   # be polite to the resolver
    return cache[doi]


# --- n2o.net.au metacat (EML dataset records) ----------------------------- #
_METACAT = "http://www.n2o.net.au/knb/metacat"


def _metacat_docid(src):
    """Extract a metacat docid from a URL or accept a bare docid ('kelly.52')."""
    if not src:
        return None
    src = src.strip()
    m = re.search(r"metacat/([^/?\s]+)", src)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z][\w.]*\.\d[\w.]*", src):   # author.NN style id
        return re.sub(r"\.(auto|manual)$", "", src)      # 'guangdi.76.auto' -> 'guangdi.76'
    return None


def resolve_metacat(src, cache):
    """Resolve an n2o.net.au metacat docid/URL to a dataset citation, else None."""
    docid = _metacat_docid(src)
    if not docid:
        return None
    ck = "metacat:" + docid
    if ck in cache:
        return cache[ck]
    link = f"{_METACAT}/{docid}/html"
    out = {"link": link, "citation": None, "title": None, "author": None, "year": None}
    try:
        xml = _doi_get(f"{_METACAT}/{docid}", "application/xml")
        if "<error>" in xml.lower():
            cache[ck] = None
            time.sleep(0.15)
            return None
        titles = re.findall(r"<title[^>]*>(.*?)</title>", xml, re.S)
        out["title"] = _clean_text(re.sub(r"\s+", " ", titles[0])) if titles else None
        names = []
        for c in re.findall(r"<creator\b.*?</creator>", xml, re.S):
            sur = re.search(r"<surName[^>]*>(.*?)</surName>", c, re.S)
            giv = re.search(r"<givenName[^>]*>(.*?)</givenName>", c, re.S)
            if sur:
                names.append((sur.group(1).strip(), giv.group(1).strip() if giv else ""))
        pd = re.search(r"<pubDate[^>]*>(.*?)</pubDate>", xml, re.S)
        out["year"] = int(re.search(r"\d{4}", pd.group(1)).group(0)) if (pd and re.search(r"\d{4}", pd.group(1))) else None
        yr = f" ({out['year']})" if out["year"] else ""
        if names:
            out["author"] = names[0][0]
            who = "; ".join((f"{s}, {g}".strip().rstrip(",")) for s, g in names)
            out["citation"] = _clean_text(
                f"{who}{yr}. {out['title']}. N2O Network data repository (n2o.net.au). {link}")
        elif out["title"]:                     # dataset with no individual creators
            out["citation"] = _clean_text(
                f"{out['title']}{yr}. N2O Network data repository (n2o.net.au). {link}")
        cache[ck] = out if (out["citation"] or out["title"]) else None
    except Exception:
        cache[ck] = None
    time.sleep(0.15)
    return cache[ck]


def resolve_source(src, cache):
    """Resolve a source string (DOI, n2o.net.au metacat URL/docid, or plain URL)
    to {citation,title,author,year,link}. Used for both the library 'paper'
    field and the user override file."""
    if not src:
        return None
    s_ = src.strip()
    if "n2o.net.au" in s_.lower() or _metacat_docid(s_):
        m = resolve_metacat(s_, cache)
        if m:
            return m
    if _extract_doi(s_):
        return resolve_doi(s_, cache)
    mc = re.search(r"publish\.csiro\.au/\w+/(\w+)", s_, re.I)
    if mc:                                            # CSIRO Publishing -> DOI 10.1071/<id>
        r = resolve_doi("https://doi.org/10.1071/" + mc.group(1), cache)
        if r and (r.get("citation") or r.get("title")):
            return r
    if s_.lower().startswith("http"):                # repository URL: try to recover its DOI
        ck = "url:" + s_
        if ck not in cache:
            doi = _scrape_doi(s_)
            r = resolve_doi("https://doi.org/" + doi, cache) if doi else None
            cache[ck] = r if (r and (r.get("citation") or r.get("title"))) else \
                {"link": s_, "citation": None, "title": None, "author": None, "year": None}
        return cache[ck]
    return None


# --- candidate finder (--find-sources): suggest sources for unsourced refs -- #
def _crossref_search(author, year, kw="nitrous oxide soil emissions", rows=3):
    q = {"query.author": author, "query.bibliographic": kw, "rows": rows}
    if year:
        q["filter"] = f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31"
    try:
        d = json.loads(_doi_get("https://api.crossref.org/works?" + urllib.parse.urlencode(q),
                                "application/json"))
    except Exception:
        return []
    items = []
    for it in d.get("message", {}).get("items", []):
        a = (it.get("author") or [{}])[0]
        items.append((it.get("DOI"), f"{a.get('family', '')}, {a.get('given', '')}".strip(", "),
                      (it.get("title") or [""])[0][:80]))
    time.sleep(0.15)
    return items


def _parse_ref(ref):
    """'de_rosa_2018' -> ('De Rosa','2018'); 'rowlings.34' -> ('Rowlings', None)."""
    ref = (ref or "").strip()
    m = re.match(r"^(.*?)[._](\d{4})$", ref)
    name, year = (m.group(1), m.group(2)) if m else (re.split(r"[._]\d", ref)[0], None)
    return re.sub(r"[_]+", " ", name).strip().title(), year


def write_source_candidates(path, unsourced_refs, ref_name, cache):
    rows = []
    for ref_l in sorted(unsourced_refs):
        ref = ref_name.get(ref_l, ref_l)
        author, year = _parse_ref(ref)
        cands = []
        for variant in dict.fromkeys([ref, re.sub(r"\.(auto|manual)$", "", ref)]):
            m = resolve_metacat(variant, cache)
            if m and m.get("citation"):
                cands.append(("metacat", m["link"], m["title"] or ""))
                break
        for doi, who, title in _crossref_search(author, year):
            if doi:
                cands.append(("crossref", "https://doi.org/" + doi, f"{who} | {title}"))
        if not cands:
            rows.append(dict(ref=ref, source="", kind="NONE", candidate_info="(no candidate found)"))
        for kind, src, info in cands[:4]:
            rows.append(dict(ref=ref, source=src, kind=kind, candidate_info=info))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["ref", "source", "kind", "candidate_info"])
        w.writeheader()
        w.writerows(rows)


def classify_n_type(form):
    """Collapse a granular NitrogenForm into the broad NitrogenType bucket.

    The scheme is reverse-engineered from the curated pairs already in the DB
    (RawMeasurementTreatment), kept coarse so one-hot encoding stays low-dim:
      * an enhancer/inhibitor/coating signal wins over the base material -> ESN
        (SuperU, Agrotain, DMPP, NBPT, nitrapyrin, polymer-coated, PCU);
      * liquid NPK blends / urea-ammonium-nitrate -> UAN;
      * organics (manure/urine/slurry/feces/litter/compost) -> Manure;
      * calcium nitrate -> CAN; anhydrous -> Anhydrous Ammonia; etc.

    Two buckets extend the DB's 12-value vocabulary by user decision:
      * Nitrate (KNO3 / potassium nitrate - nitrate-N, K is inert for cycling);
      * Foliar  (MacroPro and other foliar feeds).
    """
    f_ = lc(form)
    if not f_:
        return None
    if any(k in f_ for k in ("agrotain", "dmpp", "nbpt", "nitrapyrin",
                             "polymer", "coated", "esn", "superu", "super u", "pcu")):
        return "ESN"
    if "anhydrous" in f_ or "anhyrous" in f_:   # 2nd spelling is a typo in the source
        return "Anhydrous Ammonia"
    if any(k in f_ for k in ("urine", "slurry", "manure", "dung", "feces",
                             "faeces", "litter", "compost", "poultry")):
        return "Manure"
    if "uan" in f_ or "urea ammonium nitrate" in f_ or "urea-ammonium" in f_:
        return "UAN"
    if "nh4no3" in f_ or "ammonium nitrate" in f_:
        return "Ammonium Nitrate"
    if "calcium nitrate" in f_ or f_ == "can":
        return "CAN"
    if "kno3" in f_ or "potassium nitrate" in f_:   # nitrate-N (K is inert here)
        return "Nitrate"
    if "macropro" in f_ or "foliar" in f_:
        return "Foliar"
    if "ammonium sulfate" in f_ or "ammonium sulphate" in f_:
        return "Ammonium Sulfate"
    if any(k in f_ for k in ("diammonium phosphate", "monoammonium phosphate",
                             "ammonium phosphate", "10-34-0", "dap")):
        return "Ammonium Phosphate"
    if "urea" in f_:
        return "Urea"
    if "cover crop" in f_:
        return "Cover Crop"
    if "liquid" in f_:          # the DB tended to file liquid NPK blends under UAN
        return "UAN"
    return "Unknown"


# --------------------------------------------------------------------------- #
# Library / Summary enrichment lookups
# --------------------------------------------------------------------------- #
def load_library(path):
    """Index Sitelibrary by lowercase Reference and (Reference, Treatment)."""
    by_ref = {}          # ref_lc -> dict (first row wins for site-level fields)
    by_pair = {}         # (ref_lc, trt_lc) -> row dict
    with open(path, newline="", encoding="utf-8-sig", errors="ignore") as fh:
        for r in csv.DictReader(fh):
            ref = lc(r.get("Reference"))
            if not ref:
                continue
            by_ref.setdefault(ref, r)
            by_pair[(ref, lc(r.get("Treatment")))] = r
    return by_ref, by_pair


def load_summary(path):
    """From Summary_V1 build enrichment lookups:
        map_mat : ref_lc -> (MAP, MAT)        from MeanPrec_mm / MeanTemp_c
        bd      : (ref_lc, trt_lc) -> BulkDensity, ref_lc -> BD (fallback)
        soilc   : (ref_lc, trt_lc) -> soilC_perc, ref_lc -> soilC (fallback)
        pubyear : ref_lc -> PubYear (int)
    """
    map_mat, bd_pair, bd_ref, soilc_pair, soilc_ref, pubyear = {}, {}, {}, {}, {}, {}
    if not Path(path).exists():
        return map_mat, bd_pair, bd_ref, soilc_pair, soilc_ref, pubyear
    with open(path, newline="", encoding="utf-8-sig", errors="ignore") as fh:
        for r in csv.DictReader(fh):
            ref = lc(r.get("Reference"))
            if not ref:
                continue
            trt = lc(r.get("Treatment"))
            map_mat.setdefault(ref, (f(r.get("MeanPrec_mm")), f(r.get("MeanTemp_c"))))
            yr = f(r.get("PubYear"))
            if yr is not None:
                pubyear.setdefault(ref, int(yr))
            bd = f(r.get("BD"))
            if bd is not None:
                bd_pair.setdefault((ref, trt), bd)
                bd_ref.setdefault(ref, bd)
            sc = f(r.get("soilC_perc"))
            if sc is not None:
                soilc_pair.setdefault((ref, trt), sc)
                soilc_ref.setdefault(ref, sc)
    return map_mat, bd_pair, bd_ref, soilc_pair, soilc_ref, pubyear


def load_overrides(path):
    """ref_lc -> verified source (DOI/URL/metacat docid) from a curated CSV."""
    out = {}
    if not path or not Path(path).exists():
        return out
    with open(path, newline="", encoding="utf-8-sig", errors="ignore") as fh:
        for r in csv.DictReader(fh):
            ref = lc(r.get("ref") or r.get("Reference"))
            src = s(r.get("source") or r.get("doi_or_url") or r.get("DOI"))
            if ref and src:
                out[ref] = src
    return out


def citation_of(lib_row):
    """Best paper/citation from a library row, mirroring the original logic."""
    if lib_row is None:
        return None
    return (s(lib_row.get("PrimaryPaper"))
            or s(lib_row.get("SecondaryPaper"))
            or s(lib_row.get("Other repository/location")))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="DorichData")
    ap.add_argument("--out", default="DorichData/cleaned")
    ap.add_argument("--no-citations", action="store_true",
                    help="Skip DOI resolution; leave Citation as the raw DOI.")
    ap.add_argument("--keep-gracenet", action="store_true",
                    help="Keep the all-caps USDA/GRACEnet refs that are absent from "
                         "the site library (dropped by default as already ETL'd).")
    ap.add_argument("--overrides", default=None,
                    help="CSV of verified sources (cols: ref, source) to attach to "
                         "refs lacking a library citation. Default: <data>/source_overrides.csv")
    ap.add_argument("--allow-unsourced", action="store_true",
                    help="Keep refs with no verifiable source (default: drop them).")
    ap.add_argument("--find-sources", action="store_true",
                    help="Search CrossRef/metacat for unsourced refs, write "
                         "<data>/source_candidates.csv for review, and exit.")
    # Surrogate-key offsets: generated PKs start at <offset>+1 so they don't
    # collide with rows already in the target DB. Pass the current MAX(PK) of
    # each table (0 = standalone output starting at 1).
    ap.add_argument("--flux-replicates", type=int, default=3,
                    help="Assumed replicate count n for converting the source SD "
                         "(n2osd) to FluxStandardError = SD/sqrt(n). Default 3.")
    ap.add_argument("--pub-offset", type=int, default=0)
    ap.add_argument("--site-offset", type=int, default=0)
    ap.add_argument("--exp-offset", type=int, default=0)
    ap.add_argument("--trt-offset", type=int, default=0)
    ap.add_argument("--raw-offset", type=int, default=0)
    args = ap.parse_args()

    data = Path(args.data)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    daily_path = data / "DailyGHG_V1.csv"
    if not daily_path.exists():
        sys.exit(f"Missing daily file: {daily_path}")

    # DOI -> citation cache (persisted between runs).
    cache_path = data / ".citation_cache.json"
    doi_cache = {}
    if cache_path.exists():
        try:
            doi_cache = json.loads(cache_path.read_text())
        except Exception:
            doi_cache = {}

    by_ref, by_pair = load_library(data / "Sitelibrary_V1.csv")
    summary, bd_pair, bd_ref, soilc_pair, soilc_ref, pubyear = load_summary(data / "Summary_V1.csv")
    overrides = load_overrides(args.overrides or (data / "source_overrides.csv"))
    if overrides:
        print(f"Loaded {len(overrides)} verified-source overrides.")

    def bd_of(ref_l, trt_l):
        bd = bd_pair.get((ref_l, trt_l))
        return bd if bd is not None else bd_ref.get(ref_l)

    def soilc_of(ref_l, trt_l):
        sc = soilc_pair.get((ref_l, trt_l))
        return sc if sc is not None else soilc_ref.get(ref_l)

    # Refs whose library 'paper' is a status note (e.g. 'Pub is on process').
    unpub_refs = {ref for ref, row in by_ref.items() if is_unpublished(citation_of(row))}
    if unpub_refs:
        print(f"Unpublished refs to drop (must be published): {sorted(unpub_refs)}")

    ref_meta = {}            # ref_lc -> resolved source dict (filled after pass 1)
    unsourced_refs = set()   # ref_lc with no verifiable source (filled after pass 1)

    def source_for(ref_l, ref_orig):
        """Resolve a ref's verified source: override > library paper > the ref
        code as an n2o.net.au metacat docid. Returns {citation,title,...} or None."""
        for cand in (overrides.get(ref_l), citation_of(by_ref.get(ref_l)), ref_orig):
            if not cand:
                continue
            m = resolve_source(cand, doi_cache)
            if m and (m.get("citation") or m.get("link")):
                return m
        return None

    def drop_ref(ref_orig, ref_l):
        if ref_l in unpub_refs:                          # must be published
            return True
        if ref_l in unsourced_refs and not args.allow_unsourced:
            return True                                  # must have a verified source
        if not args.keep_gracenet:
            if ref_l in GRACENET_REFS:                   # confirmed GRACEnet (mixed-case)
                return True
            # Already-ETL'd GRACEnet: all-caps USDA codes absent from the library
            # (blank refs are junk too). Author/dataset refs are kept.
            if ref_l not in by_ref and (not ref_orig or looks_usda_code(ref_orig)):
                return True
        return False

    print(f"Library: {len(by_ref)} references, {len(by_pair)} treatments; "
          f"Summary: {len(summary)} refs, {len(bd_pair)} treatment bulk densities.")

    # ----- Pass 1: discover experiments/treatments and accumulate aggregates -- #
    # Treatment accumulator keyed by (ref_lc, trt_lc).
    trt = OrderedDict()

    def trt_acc(ref_l, trt_l, ref_orig, trt_orig):
        k = (ref_l, trt_l)
        a = trt.get(k)
        if a is None:
            a = dict(ref=ref_l, ref_orig=ref_orig, trt_orig=trt_orig,
                     crops=Counter(), till=Counter(),
                     sand=[0.0, 0], silt=[0.0, 0], clay=[0.0, 0], ph=[0.0, 0])
            trt[k] = a
        return a

    ref_name = {}  # ref_lc -> original-case Reference (first seen), for display names
    n_rows = 0
    dropped_refs, n_drop_rows = set(), 0
    with open(daily_path, newline="", encoding="utf-8-sig", errors="ignore") as fh:
        for row in csv.DictReader(fh):
            n_rows += 1
            ref_orig = s(row.get("SiteID")) or ""
            ref_l = lc(ref_orig)
            if drop_ref(ref_orig, ref_l):
                dropped_refs.add(ref_l); n_drop_rows += 1
                continue
            trt_orig = s(row.get("Treatment")) or ""
            ref_name.setdefault(ref_l, ref_orig)
            a = trt_acc(ref_l, lc(trt_orig), ref_orig, trt_orig)
            crop = s(row.get("Crop"))
            if crop:
                a["crops"][crop] += 1
            till = s(row.get("Tillage.type"))
            if till and till.lower() != "harv":   # 'harv' is a harvest marker, not tillage
                a["till"][till] += 1
            for col, key in (("Sand", "sand"), ("Silt", "silt"),
                             ("Clay", "clay"), ("pH", "ph")):
                v = f(row.get(col))
                if v is not None:
                    a[key][0] += v
                    a[key][1] += 1
    print(f"Daily rows: {n_rows}; distinct treatments: {len(trt)}; "
          f"distinct experiments: {len({k[0] for k in trt})}")
    if n_drop_rows:
        print(f"Dropped {n_drop_rows} rows from {len(dropped_refs)} refs "
              f"(already-ETL'd GRACEnet + unpublished; --keep-gracenet retains GRACEnet).")

    # ----- Resolve a verified source for each surviving ref -------------------- #
    surviving = sorted({k[0] for k in trt})
    print(f"Resolving sources for {len(surviving)} refs (override > library > metacat)...")
    for ref_l in surviving:
        ref_meta[ref_l] = source_for(ref_l, ref_name.get(ref_l))
    unsourced_refs.update(r for r in surviving if ref_meta.get(r) is None)
    sourced = len(surviving) - len(unsourced_refs)
    print(f"  sourced: {sourced}/{len(surviving)}; unsourced: {sorted(unsourced_refs)}")

    if args.find_sources:
        write_source_candidates(data / "source_candidates.csv", unsourced_refs,
                                ref_name, doi_cache)
        cache_path.write_text(json.dumps(doi_cache, indent=0))
        print(f"Wrote candidate sources to {data / 'source_candidates.csv'}. "
              f"Review, fill {data / 'source_overrides.csv'}, then rerun.")
        return
    if unsourced_refs and not args.allow_unsourced:
        print(f"  dropping {len(unsourced_refs)} unsourced refs (use --allow-unsourced "
              f"or add to source_overrides.csv to keep).")

    # ----- Build Publication / Site / Experiment / Treatment with surrogate IDs #
    pub_by_key, pubs = {}, []          # citation-or-ref -> PubID
    site_by_key, sites = {}, []        # (name, lat, lon) -> SiteID
    exp_by_ref, exps = {}, []          # ref_lc -> ExperimentID
    trt_id = {}                        # (ref_lc, trt_lc) -> TreatmentID

    def get_pub(ref_l, lib_row):
        meta = ref_meta.get(ref_l)            # resolved in the source pre-pass
        key = (meta and (meta.get("citation") or meta.get("link"))) or f"ref:{ref_l}"
        pid = pub_by_key.get(key)
        if pid is None:
            if meta and (meta.get("citation") or meta.get("title") or meta.get("link")):
                title = clip(fix_allcaps(meta.get("title")), 255) or ref_name.get(ref_l, ref_l)
                citation = clip(fix_allcaps(meta.get("citation")), 500)
                author = clip(meta.get("author"), 32) or clip(lead_author(ref_name.get(ref_l)), 32)
                if author and author.isupper():     # 'WAGNER-RIDDLE' -> 'Wagner-Riddle'
                    author = re.sub(r"[A-Z]{2,}", lambda m: m.group(0).title(), author)
                yr = meta.get("year") or pubyear.get(ref_l)
                link = clip(meta.get("link"), 255)
            else:                              # only reachable with --allow-unsourced
                title = ref_name.get(ref_l, ref_l)
                citation = None
                author = clip(lead_author(ref_name.get(ref_l)), 32)
                yr = pubyear.get(ref_l)
                link = None
            pid = args.pub_offset + len(pubs) + 1
            pubs.append(dict(PubID=pid, PubTitle=title, LeadAuthor=author,
                             Citation=citation, PubYear=yr, Link=link))
            pub_by_key[key] = pid
        return pid

    def get_site(ref_l, lib_row):
        if lib_row is not None and f(lib_row.get("Latitude")) is not None:
            name = clip(lib_row.get("SiteID"), 255) or ref_name.get(ref_l, ref_l)
            lat = f(lib_row.get("Latitude"))
            lon = f(lib_row.get("Longitude")) or 0.0
        else:
            name = clip(ref_name.get(ref_l, ref_l), 255)  # placeholder for unmatched refs
            lat, lon = 0.0, 0.0
        key = (name, lat, lon)
        sid = site_by_key.get(key)
        if sid is None:
            sid = args.site_offset + len(sites) + 1
            mp, mt = summary.get(ref_l, (None, None))
            sites.append(dict(SiteID=sid, Latitude=lat, Longitude=lon,
                              MAP=mp, MAT=mt, Gracenet=0, SiteName=name))  # Dorich data: never GRACEnet
            site_by_key[key] = sid
        return sid

    def get_exp(ref_l):
        eid = exp_by_ref.get(ref_l)
        if eid is None:
            lib_row = by_ref.get(ref_l)
            eid = args.exp_offset + len(exps) + 1
            exps.append(dict(ExperimentID=eid, ExperimentName=clip(ref_name.get(ref_l, ref_l), 255),
                             SiteID=get_site(ref_l, lib_row),
                             PubID=get_pub(ref_l, lib_row)))
            exp_by_ref[ref_l] = eid
        return eid

    treatments = []
    for (ref_l, trt_l), a in trt.items():
        if drop_ref(a["ref_orig"], ref_l):    # now also drops unsourced refs
            continue
        lib_row = by_pair.get((ref_l, trt_l)) or by_ref.get(ref_l)
        eid = get_exp(ref_l)
        tid = args.trt_offset + len(treatments) + 1
        trt_id[(ref_l, trt_l)] = tid

        def mean(box):
            return round(box[0] / box[1], 4) if box[1] else None

        primary_crop = clip(a["crops"].most_common(1)[0][0], 32) if a["crops"] else "Unknown"
        treatments.append(dict(
            TreatmentID=tid,
            ExperimentID=eid,
            PrimaryCrop=primary_crop or "Unknown",
            Management=clip(management_of(lib_row.get("N type") if lib_row else None,
                                          lib_row.get("Treatment_Description") if lib_row else None), 16)
                       if lib_row else None,
            Tillage=clip(a["till"].most_common(1)[0][0], 32) if a["till"] else None,
            FluxInstrument=clip(lib_row.get("Measurement_method"), 45) if lib_row else None,
            TreatmentName=clip(a["trt_orig"], 45),
            SandMean=mean(a["sand"]), SiltMean=mean(a["silt"]), ClayMean=mean(a["clay"]),
            TreatmentDescription=(s(lib_row.get("Treatment_Description")) if lib_row else None),
            pH=mean(a["ph"]),
            BulkDensity=bd_of(ref_l, trt_l),
            SOMMean=soilc_of(ref_l, trt_l),   # stored as % organic C, unconverted
        ))

    # Persist the DOI cache and report resolution coverage.
    try:
        cache_path.write_text(json.dumps(doi_cache, indent=0))
    except Exception as exc:
        print(f"(warning: could not write citation cache: {exc})")
    resolved = sum(1 for v in doi_cache.values() if v and v.get("citation"))
    with_doi = sum(1 for p in pubs if p["Link"])
    print(f"Citations: {resolved} DOIs resolved to full references; "
          f"{with_doi}/{len(pubs)} publications have a citation"
          + (" (skipped: --no-citations)" if args.no_citations else "") + ".")

    write_csv(out / "Publication.csv",
              ["PubID", "PubTitle", "LeadAuthor", "Citation", "PubYear", "Link"], pubs)
    write_csv(out / "Site.csv",
              ["SiteID", "Latitude", "Longitude", "MAP", "MAT", "Gracenet", "SiteName"], sites)
    write_csv(out / "Experiment.csv",
              ["ExperimentID", "ExperimentName", "SiteID", "PubID"], exps)
    write_csv(out / "Treatment.csv",
              ["TreatmentID", "ExperimentID", "PrimaryCrop", "Management", "Tillage",
               "FluxInstrument", "TreatmentName", "SandMean", "SiltMean", "ClayMean",
               "TreatmentDescription", "pH", "BulkDensity", "SOMMean"],
              treatments)
    print(f"Wrote Publication={len(pubs)} Site={len(sites)} "
          f"Experiment={len(exps)} Treatment={len(treatments)}")

    # ----- Pass 2: stream the daily file -> RawMeasurementTreatment.csv -------- #
    # VWCCalculated / WFPSCalculated are cross-derived from the measured metric
    # via porosity = 1 - BD/2.65 whenever a bulk density is known for the
    # treatment:  WFPS = VWC / porosity  and  VWC = WFPS * porosity.
    # NitrogenForm keeps the granular Fert.Type; NitrogenType is the broad bucket.
    # Precip is the total daily water input (rain + irrigation): the source
    # `rain.irrigation` already sums the two, so Precip = rain.irrigation (else
    # rain), and IrrigationApplied = rain.irrigation - rain on the days it rose.
    # Management flags the management action on that date (fertilizer/tillage/
    # harvest; 'planting' has no daily source). Tillage and Harvest are dedicated
    # 0/1 day flags. VWC/WFPS use the aggregate columns (far more complete than
    # the depth-specific soilM*/WFPS* columns).
    daily_cols = ["RawTreatmentID", "TreatmentID", "Date", "DOY", "N2OFlux",
                  "FluxStandardError", "VWC", "VWCCalculated", "WFPS", "WFPSCalculated",
                  "SoilT", "AirT", "AirTMax", "AirTMin", "Precip", "IrrigationApplied",
                  "NitrogenApplied", "NitrogenForm", "NitrogenType", "SoilNH4", "SoilNO3",
                  "PlantedCrop", "Tillage", "Harvest", "Management"]
    ntype_seen = Counter()      # (form, type) -> n, for the review summary
    calc_vwc = calc_wfps = irrig = n_till = n_harv = n_fert = 0
    kept = skipped_nodate = dup_collisions = 0
    # FluxStandardError is derived from the source SD: SE = SD / sqrt(n). The
    # source has no replicate count, so n is assumed (--flux-replicates).
    sqrt_n = max(args.flux_replicates, 1) ** 0.5

    # Pass 2a: read the daily file into one payload per (TreatmentID, Date). The
    # kept data is already treatment-averaged — one row per treatment-day (verified
    # zero (tid,date) duplicates; n2osd is the across-replicate SD). A collision
    # here would mean an unexpected replicate, so we count and warn rather than
    # silently overwrite. Payloads omit the leading RawTreatmentID pk; it is
    # assigned at write time so the ids stay contiguous in sorted order.
    by_trt = {}                 # tid -> {iso_date: payload}
    with open(daily_path, newline="", encoding="utf-8-sig", errors="ignore") as fin:
        for row in csv.DictReader(fin):
            ref_orig = s(row.get("SiteID")) or ""
            ref_l = lc(ref_orig)
            if drop_ref(ref_orig, ref_l):   # already-ETL'd GRACEnet
                continue
            iso, doy = parse_date(row.get("Date"))
            if iso is None:                 # Date is NOT NULL on the model
                skipped_nodate += 1
                continue
            trt_l = lc(s(row.get("Treatment")) or "")
            tid = trt_id[(ref_l, trt_l)]

            vwc = frac(row.get("soil.M"))
            wfps = frac(row.get("WFPS"))
            bd = bd_of(ref_l, trt_l)
            por = (1 - bd / PARTICLE_DENSITY) if (bd is not None and 0 < bd < PARTICLE_DENSITY) else None
            vwc_calc = wfps_calc = None
            if por:
                if wfps is not None:
                    vwc_calc = round(wfps * por, 4); calc_vwc += 1
                if vwc is not None:
                    wfps_calc = round(vwc / por, 4); calc_wfps += 1

            # Total water input and the irrigation it implies.
            rain = f(row.get("rain"))
            rain_irr = f(row.get("rain.irrigation"))
            if rain_irr is not None:
                precip = rain_irr
                irrigation = None
                if rain is not None and rain_irr - rain > 0:
                    irrigation = round(rain_irr - rain, 4); irrig += 1
            else:
                precip = rain
                irrigation = None

            # Per-day management events.
            form = clip(row.get("Fert.Type"), 64)
            till_type = s(row.get("Tillage.type"))
            napplied = f(row.get("Fertilizer.kgN.ha"))
            harv_event = bool(till_type and till_type.lower() == "harv")
            # A 'harv' marker is a harvest, never tillage, even if Tillage.cm is set.
            till_event = (not harv_event) and bool(s(row.get("Tillage.cm")) or till_type)
            fert_event = bool(form or napplied)

            harvest = 1 if harv_event else None
            tillage = 1 if till_event else None
            # Management holds one action label; flags above capture till/harvest.
            if fert_event:
                management = "fertilizer"
            elif till_event:
                management = "tillage"
            elif harv_event:
                management = "harvest"
            else:
                management = None
            n_fert += fert_event
            n_till += till_event
            n_harv += harv_event

            ntype = clip(classify_n_type(form), 45)
            if form:
                ntype_seen[(form, ntype)] += 1

            sd = f(row.get("n2osd"))                  # source standard deviation
            fse = round(sd / sqrt_n, 4) if sd is not None else None

            # NitrogenApplied is never null: a measured fert-N rate, or 0.
            napplied_out = napplied if napplied is not None else 0

            payload = [
                tid, iso, doy,
                f(row.get("n2o")), fse,
                vwc, vwc_calc, wfps, wfps_calc,
                temp(row.get("soilt")), temp(row.get("tavg")), temp(row.get("tmax")), temp(row.get("tmin")),
                precip, irrigation, napplied_out,
                form, ntype,
                f(row.get("NH4")), f(row.get("NO3")),
                clip(row.get("Crop"), 32),
                tillage, harvest, management,
            ]
            day_map = by_trt.setdefault(tid, {})
            if iso in day_map:
                dup_collisions += 1
            day_map[iso] = payload
            kept += 1

    # Pass 2b: emit one row per calendar day across each treatment's full span,
    # gap-filling any missing day so every treatment is a continuous daily series,
    # sorted by TreatmentID then Date. A synthetic gap-day carries the date/DOY,
    # NitrogenApplied=0, and NULL for every measurement (we do not fabricate data).
    # GAP_FILL payload layout: [tid, iso, doy] + 12 NULL metrics + [0 N] + 8 NULLs.
    written = filled = 0
    with open(out / "RawMeasurementTreatment.csv", "w", newline="", encoding="utf-8") as fout:
        w = csv.writer(fout)
        w.writerow(daily_cols)
        for tid in sorted(by_trt):
            day_map = by_trt[tid]
            lo = date.fromisoformat(min(day_map))   # iso strings sort chronologically
            hi = date.fromisoformat(max(day_map))
            d = lo
            while d <= hi:
                iso = d.isoformat()
                payload = day_map.get(iso)
                if payload is None:
                    payload = ([tid, iso, d.timetuple().tm_yday]
                               + [None] * 12 + [0] + [None] * 8)
                    filled += 1
                written += 1
                w.writerow([args.raw_offset + written] + payload)
                d += timedelta(days=1)

    if dup_collisions:
        print(f"WARNING: {dup_collisions} (TreatmentID,Date) collisions — unexpected "
              f"replicate-level rows; last value kept (data may not be treatment-averaged).")
    print(f"Wrote RawMeasurementTreatment={written} "
          f"(read {kept} daily rows; gap-filled {filled} missing days; "
          f"skipped {skipped_nodate} with no parseable Date; sorted by TreatmentID, Date)")
    print(f"Derived VWCCalculated for {calc_vwc} rows, WFPSCalculated for {calc_wfps} rows; "
          f"IrrigationApplied for {irrig} rows.")
    print(f"Management events: fertilizer={n_fert} tillage={n_till} harvest={n_harv} "
          f"(planting has no daily source).")

    print("\nNitrogenForm -> NitrogenType mapping (review):")
    for (form, ntype), n in sorted(ntype_seen.items(), key=lambda x: (x[0][1] or "", -x[1])):
        print(f"   {ntype or '(none)':<18} <- {form:<28} ({n})")


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})


if __name__ == "__main__":
    main()

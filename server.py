"""Serveur MCP du protocole de scoping review « EIM en Afrique ».

Version fidèle au protocole (protocol.json) : il exécute les recherches telles
que le protocole les décrit, archive les résultats bruts, déduplique, enregistre
les décisions de tri et calcule les comptes du diagramme de flux.

Lancement : python server.py   (transport stdio)
Dépendance : pip install mcp
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

try:  # SDK MCP version 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # SDK MCP version 1
    from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent
# Protocole actif : protocol-v2.json (amélioré) par défaut ; protocol.json = version fidèle.
PROTOCOL_FILE = os.environ.get("EIM_PROTOCOL", "protocol-v2.json")
PROTOCOL = json.loads((ROOT / PROTOCOL_FILE).read_text(encoding="utf-8"))
# Un dossier de données par version de protocole, pour ne jamais mélanger les résultats.
DATA = Path(os.environ.get("EIM_DATA_DIR", ROOT / "data" / PROTOCOL["version"]))
RAW = DATA / "raw"
EXPORTS = DATA / "exports"
USER_AGENT = "eim-scoping-mcp/0.1"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
STAGES = ("title_abstract", "full_text")
DECISIONS = ("include", "exclude", "uncertain")

mcp = FastMCP("eim-scoping-review")


# ---------------------------------------------------------------- stockage

def db() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / "review.db")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS searches(
            id INTEGER PRIMARY KEY, source TEXT, group_id TEXT, query TEXT,
            run_at TEXT, total_found INTEGER, retrieved INTEGER, raw_file TEXT);
        CREATE TABLE IF NOT EXISTS records(
            id INTEGER PRIMARY KEY, source TEXT, source_id TEXT, pmid TEXT, doi TEXT,
            title TEXT, abstract TEXT, authors TEXT, journal TEXT, year INTEGER,
            language TEXT, pub_types TEXT, duplicate_of INTEGER,
            UNIQUE(source, source_id));
        CREATE TABLE IF NOT EXISTS hits(
            search_id INTEGER, record_id INTEGER, UNIQUE(search_id, record_id));
        CREATE TABLE IF NOT EXISTS decisions(
            id INTEGER PRIMARY KEY, record_id INTEGER, stage TEXT, decision TEXT,
            reason_code TEXT, comment TEXT, reviewer TEXT, decided_by TEXT,
            decided_at TEXT);
        """
    )
    return con


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def http_get(url: str, params: dict, post: bool = False) -> bytes:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        url if post else f"{url}?{data.decode()}",
        data=data if post else None,
        headers={"User-Agent": USER_AGENT},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            time.sleep(0.4)  # reste sous la limite de 3 requêtes/s de NCBI
            return body
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


# ------------------------------------------------------------- requêtes

def _group(group_id: str) -> dict:
    for g in PROTOCOL["keyword_groups"]:
        if g["id"] == group_id:
            return g
    raise ValueError(f"Groupe inconnu : {group_id}. Valeurs : "
                     + ", ".join(g["id"] for g in PROTOCOL["keyword_groups"]))


def make_query(source: str, group_id: str) -> str:
    g = _group(group_id)
    p = PROTOCOL
    if source not in SEARCHERS:
        raise ValueError("Source inconnue. Valeurs : pubmed, europepmc")
    y0 = p["period"]["from_year"]
    y1 = p["period"]["to_year"] or datetime.now().year
    langs = p.get("languages") or []
    if "search_blocks" in p:  # stratégie par blocs de concepts (protocole amélioré)
        blocks = []
        for b in p["search_blocks"].values():
            if source == "pubmed":
                terms = [f'"{m}"[MeSH Terms]' for m in b.get("mesh", [])]
                terms += [f'"{t}"[Title/Abstract]' for t in b["text"]]
            else:
                terms = [f'TITLE_ABS:"{t}"' for t in b["text"]]
            blocks.append("(" + " OR ".join(terms) + ")")
        q = " AND ".join(blocks)
        if source == "pubmed":
            q += f' AND ("{y0}/01/01"[Date - Publication] : "{y1}/12/31"[Date - Publication])'
            if langs:
                names = {"eng": "english", "fre": "french"}
                q += " AND (" + " OR ".join(f"{names[l]}[Language]" for l in langs) + ")"
            return q + " NOT (animals[MeSH Terms] NOT humans[MeSH Terms])"
        q += f" AND PUB_YEAR:[{y0} TO {y1}]"
        if langs:
            q += " AND (" + " OR ".join(f"LANG:{l}" for l in langs) + ")"
        return q
    ages = " OR ".join(p["age_terms"])
    if source == "pubmed":
        parts = [f"({g['concept']})", f"({p['geography_term']})"]
        parts += [f"({t})" for t in g["extra"]]
        parts.append(f"({ages})")
        parts.append(f'("{y0}/01/01"[Date - Publication] : "{y1}/12/31"[Date - Publication])')
        parts.append("(english[Language] OR french[Language])")
        return " AND ".join(parts) + " NOT (animals[MeSH Terms] NOT humans[MeSH Terms])"
    if source == "europepmc":
        parts = [f'"{g["concept"]}"', p["geography_term"]] + g["extra"]
        parts.append(f"({ages})")
        parts.append(f"PUB_YEAR:[{y0} TO {y1}]")
        parts.append("(LANG:eng OR LANG:fre)")
        return " AND ".join(parts)
    raise ValueError("Source inconnue. Valeurs : pubmed, europepmc")


# --------------------------------------------------------------- PubMed

def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _pubmed_search(query: str, max_results: int):
    js = json.loads(http_get(EUTILS + "esearch.fcgi", {
        "db": "pubmed", "term": query, "retmode": "json",
        "retmax": min(max_results, 9999), "tool": "eim-scoping-mcp"}, post=True))
    res = js["esearchresult"]
    ids = res.get("idlist", [])
    records, raw_chunks = [], []
    for i in range(0, len(ids), 200):
        xml = http_get(EUTILS + "efetch.fcgi", {
            "db": "pubmed", "id": ",".join(ids[i:i + 200]), "retmode": "xml",
            "tool": "eim-scoping-mcp"}, post=True)
        raw_chunks.append(xml.decode("utf-8", "replace"))
        for art in ET.fromstring(xml).iter("PubmedArticle"):
            pmid = _text(art.find(".//MedlineCitation/PMID"))
            doi = ""
            for aid in art.iter("ArticleId"):
                if aid.get("IdType") == "doi":
                    doi = _text(aid)
            year = _text(art.find(".//JournalIssue/PubDate/Year")) or \
                _text(art.find(".//JournalIssue/PubDate/MedlineDate"))[:4]
            authors = "; ".join(
                f"{_text(a.find('LastName'))} {_text(a.find('Initials'))}".strip()
                for a in art.iter("Author") if a.find("LastName") is not None)
            records.append({
                "source_id": pmid, "pmid": pmid, "doi": doi,
                "title": _text(art.find(".//ArticleTitle")),
                "abstract": "\n".join(_text(a) for a in art.iter("AbstractText")),
                "authors": authors,
                "journal": _text(art.find(".//Journal/Title")),
                "year": int(year) if year.isdigit() else None,
                "language": "; ".join(_text(l) for l in art.iter("Language")),
                "pub_types": "; ".join(_text(t) for t in art.iter("PublicationType")),
            })
    return int(res.get("count", 0)), records, "\n".join(raw_chunks)


# ----------------------------------------------------------- Europe PMC

def _epmc_search(query: str, max_results: int):
    total, records, raw_pages, cursor = 0, [], [], "*"
    while len(records) < max_results:
        body = http_get(EPMC + "POST", {"query": query, "format": "json", "resultType": "core",
                                        "pageSize": 1000, "cursorMark": cursor}, post=True)
        raw_pages.append(body.decode("utf-8", "replace"))
        js = json.loads(body)
        total = js.get("hitCount", 0)
        results = js.get("resultList", {}).get("result", [])
        for r in results:
            journal = (r.get("journalInfo") or {}).get("journal") or {}
            records.append({
                "source_id": f"{r.get('source')}:{r.get('id')}",
                "pmid": r.get("pmid") or "", "doi": r.get("doi") or "",
                "title": r.get("title") or "",
                "abstract": re.sub(r"<[^>]+>", "", r.get("abstractText") or ""),
                "authors": r.get("authorString") or "",
                "journal": journal.get("title") or (r.get("bookOrReportDetails") or {}).get("publisher", ""),
                "year": int(r["pubYear"]) if str(r.get("pubYear", "")).isdigit() else None,
                "language": r.get("language") or "",
                "pub_types": "; ".join((r.get("pubTypeList") or {}).get("pubType", [])),
            })
        nxt = js.get("nextCursorMark")
        if not results or not nxt or nxt == cursor:
            break
        cursor = nxt
    return total, records[:max_results], "\n".join(raw_pages)


SEARCHERS = {"pubmed": _pubmed_search, "europepmc": _epmc_search}


def _store(con, source: str, search_id: int, recs: list[dict]) -> int:
    new = 0
    for r in recs:
        cur = con.execute(
            "INSERT OR IGNORE INTO records(source, source_id, pmid, doi, title, abstract,"
            " authors, journal, year, language, pub_types) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (source, r["source_id"], r["pmid"], r["doi"].lower(), r["title"], r["abstract"],
             r["authors"], r["journal"], r["year"], r["language"], r["pub_types"]))
        new += cur.rowcount
        rid = con.execute("SELECT id FROM records WHERE source=? AND source_id=?",
                          (source, r["source_id"])).fetchone()["id"]
        con.execute("INSERT OR IGNORE INTO hits VALUES(?,?)", (search_id, rid))
    return new


def _norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _latest_human(con, stage: str) -> dict[int, sqlite3.Row]:
    rows = con.execute(
        "SELECT * FROM decisions WHERE stage=? AND decided_by='human' ORDER BY id", (stage,))
    return {r["record_id"]: r for r in rows}


# ------------------------------------------------------------------ outils

@mcp.resource("protocol://eim-afrique")
def protocol_resource() -> str:
    """Le protocole de la scoping review, sous forme structurée."""
    return json.dumps(PROTOCOL, ensure_ascii=False, indent=2)


@mcp.tool()
def get_protocol() -> dict:
    """Retourne le protocole : objectifs, définition des EIM, critères d'inclusion et
    d'exclusion, groupes de mots-clés, sources automatisées et non automatisées."""
    return PROTOCOL


@mcp.tool()
def build_queries(source: str = "pubmed") -> dict:
    """Construit, sans les exécuter, les équations de recherche des six groupes de
    mots-clés du protocole pour une source ('pubmed' ou 'europepmc')."""
    return {"source": source,
            "interpretation": PROTOCOL["query_interpretation"],
            "queries": {g["id"]: make_query(source, g["id"]) for g in PROTOCOL["keyword_groups"]}}


@mcp.tool()
def run_search(source: str, group_id: str = "all", max_results: int = 5000) -> dict:
    """Exécute la recherche du protocole sur une source ('pubmed' ou 'europepmc') pour un
    groupe de mots-clés (K1 à K6) ou pour tous ('all'). Archive la réponse brute, la
    requête et la date, puis enregistre les références. Relancer ajoute sans dupliquer."""
    if source not in SEARCHERS:
        raise ValueError("Source inconnue. Valeurs : pubmed, europepmc")
    groups = [g["id"] for g in PROTOCOL["keyword_groups"]] if group_id == "all" else [group_id]
    RAW.mkdir(parents=True, exist_ok=True)
    out = []
    con = db()
    for gid in groups:
        query = make_query(source, gid)
        total, recs, raw = SEARCHERS[source](query, max_results)
        stamp = now()
        raw_file = RAW / f"{stamp.replace(':', '')}_{source}_{gid}.txt"
        raw_file.write_text(raw, encoding="utf-8")
        cur = con.execute(
            "INSERT INTO searches(source, group_id, query, run_at, total_found, retrieved, raw_file)"
            " VALUES(?,?,?,?,?,?,?)", (source, gid, query, stamp, total, len(recs), str(raw_file)))
        new = _store(con, source, cur.lastrowid, recs)
        con.commit()
        out.append({"group": gid, "query": query, "total_found": total,
                    "retrieved": len(recs), "new_records": new,
                    "truncated": total > len(recs), "raw_file": str(raw_file)})
    return {"source": source, "run_at": now(), "searches": out}


@mcp.tool()
def deduplicate() -> dict:
    """Marque les doublons entre sources. Clés, dans l'ordre : PMID, DOI, puis titre
    normalisé + année. La notice PubMed est conservée de préférence. Rien n'est supprimé."""
    con = db()
    con.execute("UPDATE records SET duplicate_of=NULL")
    seen: dict[tuple, int] = {}
    dup = {"pmid": 0, "doi": 0, "title_year": 0}
    for r in con.execute("SELECT * FROM records ORDER BY (source!='pubmed'), id").fetchall():
        keys = []
        if r["pmid"]:
            keys.append(("pmid", r["pmid"]))
        if r["doi"]:
            keys.append(("doi", r["doi"]))
        nt = _norm_title(r["title"])
        if len(nt) > 25:
            keys.append(("title_year", f"{nt}|{r['year']}"))
        match = next((k for k in keys if k in seen), None)
        if match:
            con.execute("UPDATE records SET duplicate_of=? WHERE id=?", (seen[match], r["id"]))
            dup[match[0]] += 1
            master = seen[match]
        else:
            master = r["id"]
        for k in keys:
            seen.setdefault(k, master)
    con.commit()
    total = con.execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
    n_dup = sum(dup.values())
    return {"total_records": total, "duplicates": n_dup, "unique": total - n_dup,
            "duplicates_by_key": dup}


@mcp.tool()
def list_records(status: str = "unscreened", stage: str = "title_abstract",
                 limit: int = 20, offset: int = 0) -> dict:
    """Liste les références uniques. status : 'unscreened' (sans décision humaine à cette
    étape), 'include', 'exclude', 'uncertain' ou 'all'. À l'étape 'full_text', seules les
    références incluses ou incertaines au tri titres/résumés sont proposées."""
    if stage not in STAGES:
        raise ValueError(f"stage : {STAGES}")
    con = db()
    latest = _latest_human(con, stage)
    rows = con.execute("SELECT id, source, pmid, doi, title, journal, year, language"
                       " FROM records WHERE duplicate_of IS NULL ORDER BY id").fetchall()
    if stage == "full_text":
        ta = _latest_human(con, "title_abstract")
        rows = [r for r in rows if r["id"] in ta and ta[r["id"]]["decision"] != "exclude"]
    sel = []
    for r in rows:
        d = latest.get(r["id"])
        st = d["decision"] if d else "unscreened"
        if status in ("all", st):
            sel.append({**dict(r), "status": st})
    return {"total": len(sel), "offset": offset, "records": sel[offset:offset + limit]}


@mcp.tool()
def get_record(record_id: int) -> dict:
    """Retourne une référence complète (résumé compris), les recherches qui l'ont trouvée
    et l'historique de ses décisions (recommandations de l'IA et décisions humaines)."""
    con = db()
    r = con.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    if not r:
        raise ValueError(f"Référence {record_id} introuvable")
    found = con.execute("SELECT s.source, s.group_id, s.run_at FROM hits h JOIN searches s"
                        " ON s.id=h.search_id WHERE h.record_id=?", (record_id,)).fetchall()
    dec = con.execute("SELECT * FROM decisions WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
    return {**dict(r), "found_by": [dict(f) for f in found], "decisions": [dict(d) for d in dec]}


@mcp.tool()
def record_decision(record_id: int, stage: str, decision: str, decided_by: str,
                    reviewer: str, reason_code: str = "", comment: str = "") -> dict:
    """Enregistre une décision de tri (historique en ajout seul).
    stage : 'title_abstract' ou 'full_text'. decision : 'include', 'exclude', 'uncertain'.
    decided_by : 'human' pour une décision du chercheur, 'ai' pour une recommandation de
    l'assistant. Seules les décisions 'human' comptent dans le diagramme de flux : une
    recommandation 'ai' ne doit jamais être enregistrée comme 'human'.
    reason_code est obligatoire pour 'exclude' (voir exclusion_reason_codes du protocole)."""
    codes = PROTOCOL["exclusion_reason_codes"]
    if stage not in STAGES or decision not in DECISIONS or decided_by not in ("human", "ai"):
        raise ValueError(f"stage : {STAGES} ; decision : {DECISIONS} ; decided_by : human | ai")
    if decision == "exclude" and reason_code not in codes:
        raise ValueError("reason_code obligatoire pour une exclusion : " + ", ".join(codes))
    con = db()
    if not con.execute("SELECT 1 FROM records WHERE id=?", (record_id,)).fetchone():
        raise ValueError(f"Référence {record_id} introuvable")
    con.execute("INSERT INTO decisions(record_id, stage, decision, reason_code, comment,"
                " reviewer, decided_by, decided_at) VALUES(?,?,?,?,?,?,?,?)",
                (record_id, stage, decision, reason_code, comment, reviewer, decided_by, now()))
    con.commit()
    return {"record_id": record_id, "stage": stage, "decision": decision,
            "decided_by": decided_by, "counts_in_flow_diagram": decided_by == "human"}


def _parse_ris(text: str) -> list[dict]:
    recs, cur = [], {}
    tags = {"TI": "title", "T1": "title", "JO": "journal", "JF": "journal", "T2": "journal",
            "PY": "year", "Y1": "year", "DO": "doi", "AB": "abstract", "N2": "abstract",
            "LA": "language", "AN": "pmid"}
    for line in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])  - ?(.*)$", line.strip("﻿"))
        if not m:
            continue
        tag, val = m.group(1), m.group(2).strip()
        if tag == "ER":
            if cur:
                recs.append(cur)
            cur = {}
        elif tag in ("AU", "A1"):
            cur["authors"] = "; ".join(filter(None, [cur.get("authors"), val]))
        elif tag in tags:
            cur.setdefault(tags[tag], val)
    if cur:
        recs.append(cur)
    return recs


@mcp.tool()
def import_records(file_path: str, source_name: str) -> dict:
    """Importe des références trouvées manuellement (AJOL, African Index Medicus, Google
    Scholar, EMBASE, etc.) depuis un fichier .ris ou .csv exporté de la source. Le CSV doit
    avoir une colonne 'title' ; 'authors', 'year', 'journal', 'doi', 'pmid', 'abstract',
    'language' sont facultatives. source_name est le nom de la source, utilisé dans le
    diagramme de flux. Lancer deduplicate ensuite."""
    path = Path(file_path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() == ".ris":
        raw = _parse_ris(text)
    elif path.suffix.lower() == ".csv":
        raw = [{k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
               for row in csv.DictReader(text.splitlines())]
    else:
        raise ValueError("Format accepté : .ris ou .csv")
    recs = []
    for r in raw:
        title = r.get("title", "")
        if not title:
            continue
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", r.get("doi", ""))
        pmid = r.get("pmid", "") if r.get("pmid", "").isdigit() else ""
        ym = re.search(r"\d{4}", r.get("year", ""))
        recs.append({"source_id": doi or pmid or _norm_title(title)[:120], "pmid": pmid,
                     "doi": doi, "title": title, "abstract": r.get("abstract", ""),
                     "authors": r.get("authors", ""), "journal": r.get("journal", ""),
                     "year": int(ym.group()) if ym else None,
                     "language": r.get("language", ""), "pub_types": ""})
    source = "import:" + source_name.strip()
    RAW.mkdir(parents=True, exist_ok=True)
    stamp = now()
    raw_file = RAW / f"{stamp.replace(':', '')}_import_{re.sub(r'[^A-Za-z0-9]+', '-', source_name)}{path.suffix}"
    raw_file.write_text(text, encoding="utf-8")
    con = db()
    cur = con.execute(
        "INSERT INTO searches(source, group_id, query, run_at, total_found, retrieved, raw_file)"
        " VALUES(?,?,?,?,?,?,?)",
        (source, "manual", f"import de {path.name}", stamp, len(raw), len(recs), str(raw_file)))
    new = _store(con, source, cur.lastrowid, recs)
    con.commit()
    return {"source": source, "read": len(raw), "imported": len(recs), "new_records": new,
            "skipped_without_title": len(raw) - len(recs)}


@mcp.tool()
def check_known_items(identifiers: list[str]) -> dict:
    """Contrôle du rappel : pour une liste de PMID ou de DOI d'articles que le chercheur
    sait pertinents, indique lesquels sont présents dans les résultats et lesquels ont été
    manqués par la stratégie de recherche. Un article manqué signale une requête à corriger."""
    con = db()
    found, missed = [], []
    for ident in identifiers:
        i = re.sub(r"^https?://(dx\.)?doi\.org/", "", ident.strip()).lower()
        r = con.execute("SELECT id, source, title FROM records WHERE pmid=? OR doi=?",
                        (i, i)).fetchone()
        (found.append({"identifier": ident, **dict(r)}) if r else missed.append(ident))
    n = len(identifiers)
    return {"checked": n, "found": len(found), "missed": missed,
            "recall": round(len(found) / n, 3) if n else None, "found_records": found}


@mcp.tool()
def flow_counts() -> dict:
    """Comptes du diagramme de flux de sélection, à partir des décisions humaines
    uniquement : identifiées par source, doublons, triées, exclues par motif, incluses."""
    con = db()
    by_source = {r["source"]: r["c"] for r in con.execute(
        "SELECT source, COUNT(*) c FROM records GROUP BY source")}
    dups = con.execute("SELECT COUNT(*) c FROM records WHERE duplicate_of IS NOT NULL").fetchone()["c"]
    unique = {r["id"] for r in con.execute("SELECT id FROM records WHERE duplicate_of IS NULL")}
    out = {"identified_by_source": by_source, "identified_total": sum(by_source.values()),
           "duplicates_removed": dups, "unique_records": len(unique)}
    for stage in STAGES:
        latest = {k: v for k, v in _latest_human(con, stage).items() if k in unique}
        reasons: dict[str, int] = {}
        for d in latest.values():
            if d["decision"] == "exclude":
                reasons[d["reason_code"]] = reasons.get(d["reason_code"], 0) + 1
        out[stage] = {
            "screened": len(latest),
            "included": sum(d["decision"] == "include" for d in latest.values()),
            "uncertain": sum(d["decision"] == "uncertain" for d in latest.values()),
            "excluded": sum(reasons.values()),
            "excluded_by_reason": reasons,
        }
    out["title_abstract"]["not_yet_screened"] = len(unique) - out["title_abstract"]["screened"]
    out["searches"] = [dict(r) for r in con.execute(
        "SELECT source, group_id, run_at, total_found, retrieved FROM searches ORDER BY id")]
    return out


@mcp.tool()
def export_records(fmt: str = "csv", status: str = "all") -> dict:
    """Exporte les références uniques en 'csv' (tableur) ou 'ris' (Zotero, Rayyan,
    ASReview). status filtre selon la décision humaine au tri titres/résumés."""
    if fmt not in ("csv", "ris"):
        raise ValueError("fmt : csv | ris")
    con = db()
    latest = _latest_human(con, "title_abstract")
    rows = []
    for r in con.execute("SELECT * FROM records WHERE duplicate_of IS NULL ORDER BY id"):
        d = latest.get(r["id"])
        st = d["decision"] if d else "unscreened"
        if status in ("all", st):
            rows.append({**dict(r), "status": st, "reason_code": d["reason_code"] if d else ""})
    EXPORTS.mkdir(parents=True, exist_ok=True)
    path = EXPORTS / f"references_{status}_{now().replace(':', '')}.{fmt}"
    if fmt == "csv":
        cols = ["id", "source", "pmid", "doi", "title", "authors", "journal", "year",
                "language", "pub_types", "status", "reason_code", "abstract"]
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    else:
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write("TY  - JOUR\n")
                for a in filter(None, (r["authors"] or "").replace(",", ";").split(";")):
                    f.write(f"AU  - {a.strip()}\n")
                for tag, key in (("TI", "title"), ("JO", "journal"), ("PY", "year"),
                                 ("DO", "doi"), ("AN", "pmid"), ("LA", "language"), ("AB", "abstract")):
                    if r[key]:
                        f.write(f"{tag}  - {str(r[key]).replace(chr(10), ' ')}\n")
                f.write("ER  - \n\n")
    return {"file": str(path), "records": len(rows)}


if __name__ == "__main__":
    mcp.run()

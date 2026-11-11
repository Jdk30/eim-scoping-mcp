"""Application web locale de la scoping review « EIM en Afrique ».

Utilise la même base de données et les mêmes fonctions que le serveur MCP.
Lancement : python webapp.py   puis ouvrir http://127.0.0.1:8765
Dépendances : celles du paquet mcp (starlette, uvicorn), rien d'autre.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Route

import server as s

WEB = Path(__file__).resolve().parent / "web"
SAMPLE_FILE = s.DATA / "sample_300_ids.json"


def sample_ids() -> list[int]:
    return json.loads(SAMPLE_FILE.read_text()) if SAMPLE_FILE.exists() else []


def latest(con, stage: str, who: str) -> dict[int, dict]:
    rows = con.execute("SELECT * FROM decisions WHERE stage=? AND decided_by=? ORDER BY id",
                       (stage, who))
    return {r["record_id"]: dict(r) for r in rows}


def scope_ids(con, scope: str, stage: str) -> list[int]:
    """Références uniques dans l'ordre de tri : échantillon (ordre aléatoire fixé) ou tout."""
    unique = [r["id"] for r in con.execute(
        "SELECT id FROM records WHERE duplicate_of IS NULL ORDER BY id")]
    if scope == "sample":
        keep = set(unique)
        ids = [i for i in sample_ids() if i in keep]
    else:
        ids = unique
    if stage == "full_text":
        ta = latest(con, "title_abstract", "human")
        ids = [i for i in ids if i in ta and ta[i]["decision"] != "exclude"]
    return ids


def agreement(con, stage: str) -> dict:
    human, ai = latest(con, stage, "human"), latest(con, stage, "ai")
    both = [i for i in human if i in ai]
    cats = list(s.DECISIONS)
    matrix = {h: {a: 0 for a in cats} for h in cats}
    for i in both:
        matrix[human[i]["decision"]][ai[i]["decision"]] += 1
    n = len(both)
    out = {"n": n, "matrix": matrix, "recall": None, "kappa": None, "missed": []}
    if n:
        h_inc = [i for i in both if human[i]["decision"] == "include"]
        if h_inc:
            kept = [i for i in h_inc if ai[i]["decision"] != "exclude"]
            out["recall"] = round(len(kept) / len(h_inc), 3)
            out["human_includes"] = len(h_inc)
            out["missed"] = [i for i in h_inc if ai[i]["decision"] == "exclude"]
        po = sum(matrix[c][c] for c in cats) / n
        hc = Counter(human[i]["decision"] for i in both)
        ac = Counter(ai[i]["decision"] for i in both)
        pe = sum(hc[c] * ac[c] for c in cats) / (n * n)
        out["observed_agreement"] = round(po, 3)
        out["kappa"] = round((po - pe) / (1 - pe), 3) if pe < 1 else None
    return out


# ------------------------------------------------------------------ routes

async def index(request: Request):
    return HTMLResponse((WEB / "index.html").read_text(encoding="utf-8"))


async def api_summary(request: Request):
    con = s.db()
    flow = s.flow_counts()
    prog = {}
    for scope in ("sample", "all"):
        ids = scope_ids(con, scope, "title_abstract")
        done = latest(con, "title_abstract", "human")
        prog[scope] = {"total": len(ids), "screened": sum(i in done for i in ids)}
    ai = latest(con, "title_abstract", "ai")
    return JSONResponse({
        "protocol": {k: s.PROTOCOL.get(k) for k in ("title", "version", "status")},
        "flow": flow, "progress": prog,
        "ai_recommendations": dict(Counter(d["decision"] for d in ai.values())),
        "data_dir": str(s.DATA)})


async def api_protocol(request: Request):
    return JSONResponse(s.PROTOCOL)


async def api_records(request: Request):
    q = request.query_params
    scope, status = q.get("scope", "all"), q.get("status", "all")
    stage = q.get("stage", "title_abstract")
    text = (q.get("q") or "").strip().lower()
    offset, limit = int(q.get("offset", 0)), min(int(q.get("limit", 25)), 200)
    con = s.db()
    done = latest(con, stage, "human")
    rows = {r["id"]: r for r in con.execute(
        "SELECT id, source, pmid, doi, title, journal, year, language FROM records"
        " WHERE duplicate_of IS NULL")}
    out = []
    for i in scope_ids(con, scope, stage):
        r = rows[i]
        st = done[i]["decision"] if i in done else "unscreened"
        if status not in ("all", st):
            continue
        if text and text not in (r["title"] or "").lower() and text not in (r["journal"] or "").lower():
            continue
        out.append({**dict(r), "status": st})
    return JSONResponse({"total": len(out), "records": out[offset:offset + limit]})


async def api_next(request: Request):
    q = request.query_params
    scope, stage = q.get("scope", "sample"), q.get("stage", "title_abstract")
    con = s.db()
    ids = scope_ids(con, scope, stage)
    done = latest(con, stage, "human")
    nxt = next((i for i in ids if i not in done), None)
    return JSONResponse({"record_id": nxt, "total": len(ids),
                         "screened": sum(i in done for i in ids)})


async def api_record(request: Request):
    rid = int(request.path_params["rid"])
    stage = request.query_params.get("stage", "title_abstract")
    try:
        rec = s.get_record(rid)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    human = [d for d in rec["decisions"] if d["decided_by"] == "human" and d["stage"] == stage]
    ai = [d for d in rec["decisions"] if d["decided_by"] == "ai" and d["stage"] == stage]
    rec["human_decision"] = human[-1] if human else None
    # Tri à l'aveugle : la recommandation de l'IA n'est transmise qu'après la décision humaine.
    rec["ai_recommendation"] = (ai[-1] if ai else None) if human else None
    rec["ai_available"] = bool(ai)
    rec["history"] = [d for d in rec.pop("decisions") if d["decided_by"] == "human"]
    return JSONResponse(rec)


async def api_decision(request: Request):
    b = await request.json()
    try:
        res = s.record_decision(int(b["record_id"]), b.get("stage", "title_abstract"),
                                b["decision"], "human", (b.get("reviewer") or "").strip() or "anonyme",
                                b.get("reason_code", ""), b.get("comment", ""))
    except (ValueError, KeyError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(res)


async def api_agreement(request: Request):
    stage = request.query_params.get("stage", "title_abstract")
    con = s.db()
    out = agreement(con, stage)
    out["missed_records"] = [
        dict(con.execute("SELECT id, title, year FROM records WHERE id=?", (i,)).fetchone())
        for i in out.pop("missed")]
    return JSONResponse(out)


async def api_export(request: Request):
    q = request.query_params
    try:
        res = s.export_records(q.get("fmt", "csv"), q.get("status", "all"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return FileResponse(res["file"], filename=Path(res["file"]).name)


app = Starlette(routes=[
    Route("/", index),
    Route("/api/summary", api_summary),
    Route("/api/protocol", api_protocol),
    Route("/api/records", api_records),
    Route("/api/next", api_next),
    Route("/api/record/{rid:int}", api_record),
    Route("/api/decision", api_decision, methods=["POST"]),
    Route("/api/agreement", api_agreement),
    Route("/api/export", api_export),
])

if __name__ == "__main__":
    # Écoute sur la machine locale uniquement : l'application n'a pas d'authentification.
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("EIM_WEB_PORT", "8765")),
                log_level="warning")

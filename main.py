"""Ad Integrity Lab — public web app.

Modules
  1. Policy scan of a landing page across three evidence channels
  2. Canvas Lab: build canvas-only clones and measure whether they bypass
  3. Ad copy: restricted-term rewrites + full RSA sets
  4. Conversion integrity: tCPA poisoning simulation

Long tasks run as jobs so a public HTTPS proxy never has to hold a 60s request.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
import warnings
from collections import defaultdict, deque
from pathlib import Path

warnings.filterwarnings("ignore")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from engine.canvas import run_experiment
from engine.conversions import TAMPER_VECTORS, simulate
from engine.copy import build_rsa, rewrite
from engine.fetch import (
    ARTIFACT_DIR, UnsafeTarget, assert_public_host, capture, normalize_url,
)
from engine.rules import VERTICALS
from engine.scan import scan_dict

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
LOG = DATA / "experiments.jsonl"
STATIC = BASE / "static"
STATIC.mkdir(exist_ok=True)

app = FastAPI(title="Ad Integrity Lab", docs_url="/api/docs", redoc_url=None)
templates = Jinja2Templates(directory=str(BASE / "templates"))

# Only one browser capture at a time: the box has 4 vCPU and targets deserve
# polite pacing rather than parallel hammering.
_CAPTURE_SEM = asyncio.Semaphore(1)

# Per-IP token bucket: 12 job submissions per 10 minutes.
_RATE: dict[str, deque] = defaultdict(deque)
RATE_WINDOW = 600
RATE_MAX = 12

JOBS: dict[str, dict] = {}
JOB_TTL = 1800


class ScanReq(BaseModel):
    url: str = Field(min_length=4, max_length=500)


class CanvasReq(BaseModel):
    url: str = Field(min_length=4, max_length=500)


class CopyReq(BaseModel):
    text: str = Field(min_length=2, max_length=2000)
    vertical: str = "immigration"


class RsaReq(BaseModel):
    vertical: str = "immigration"
    geo: str = Field(default="", max_length=60)
    brand: str = Field(default="", max_length=60)
    product: str = Field(default="", max_length=40)


class ConvReq(BaseModel):
    impressions: int = Field(default=200_000, ge=10_000, le=1_000_000)
    target_cpa: float = Field(default=45.0, ge=1.0, le=2000.0)
    budget: float = Field(default=20_000.0, ge=100.0, le=1_000_000.0)
    injection_rate: float = Field(default=0.4, ge=0.0, le=3.0)
    dedup_effectiveness: float = Field(default=0.0, ge=0.0, le=1.0)
    value_based: bool = False
    seed: int = Field(default=7, ge=0, le=10_000)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate(ip: str) -> None:
    now = time.time()
    q = _RATE[ip]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_MAX:
        raise HTTPException(429, "Rate limit reached. Try again in a few minutes.")
    q.append(now)


def _gc_jobs() -> None:
    now = time.time()
    for jid in [j for j, v in JOBS.items() if now - v.get("created", 0) > JOB_TTL]:
        JOBS.pop(jid, None)


def _log(entry: dict) -> None:
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry["id"] = uuid.uuid4().hex[:12]
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"verticals": [{"id": v.id, "name": v.name} for v in VERTICALS]},
    )


@app.get("/api/health")
async def health():
    return {"ok": True, "artifacts": len(list(ARTIFACT_DIR.glob("*"))), "jobs": len(JOBS)}


@app.get("/api/verticals")
async def verticals():
    return [{"id": v.id, "name": v.name, "policy": v.policy, "gate": v.gate,
             "cert": v.cert, "docs": v.docs} for v in VERTICALS]


@app.get("/api/vectors")
async def vectors():
    return TAMPER_VECTORS


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

async def _run_scan(job_id: str, url: str) -> None:
    JOBS[job_id]["status"] = "running"
    JOBS[job_id]["stage"] = "rendering target"
    try:
        async with _CAPTURE_SEM:
            cap = await capture(url, want_ocr=True)
            JOBS[job_id]["stage"] = "scoring policy signals"
            result = scan_dict(cap)
        JOBS[job_id].update(status="done", result=result)
        _log({"kind": "scan", "url": result["final_url"],
              "verdict": result["verdict"], "primary": result["primary"],
              "cloaking": result["cloaking"].get("verdict"),
              "artifacts": [Path(result["screenshot"]).name] if result.get("screenshot") else []})
    except UnsafeTarget as exc:
        JOBS[job_id].update(status="error", error=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        JOBS[job_id].update(status="error", error=f"{type(exc).__name__}: {exc}")


async def _run_canvas(job_id: str, url: str) -> None:
    JOBS[job_id]["status"] = "running"
    JOBS[job_id]["stage"] = "rendering target"
    try:
        async with _CAPTURE_SEM:
            cap = await capture(url, want_ocr=True)
            JOBS[job_id]["stage"] = "building canvas clones"
            scan_data = scan_dict(cap)
            terms = []
            for v in scan_data["verticals"]:
                for h in v["hits"]:
                    if h["term"] not in terms:
                        terms.append(h["term"])
            terms = terms[:10] or ["visa"]
            JOBS[job_id]["stage"] = "measuring DOM vs OCR channels"
            exp = await asyncio.to_thread(run_experiment, cap, terms)
        result = {
            "url": cap.final_url or url,
            "terms": exp.terms,
            "numbers": exp.numbers,
            "verdict": exp.verdict,
            "baseline": exp.baseline,
            "image_mode": {k: v for k, v in exp.image_mode.items() if k != "dom_text"},
            "text_mode": {k: v for k, v in exp.text_mode.items() if k != "dom_text"},
            "screenshot": Path(cap.screenshot).name if cap.screenshot else "",
            "artifacts": [Path(p).name for p in (
                exp.image_mode.get("path"), exp.text_mode.get("path"),
                exp.image_mode.get("measured_screenshot"), exp.text_mode.get("measured_screenshot"),
            ) if p],
            "scan_verdict": scan_data["verdict"],
        }
        JOBS[job_id].update(status="done", result=result)
        _log({"kind": "canvas", "url": result["url"], "verdict": exp.verdict,
              "numbers": exp.numbers, "artifacts": result["artifacts"]})
    except UnsafeTarget as exc:
        JOBS[job_id].update(status="error", error=str(exc))
    except Exception as exc:  # pragma: no cover
        JOBS[job_id].update(status="error", error=f"{type(exc).__name__}: {exc}")


def _new_job(kind: str) -> str:
    _gc_jobs()
    jid = uuid.uuid4().hex[:16]
    JOBS[jid] = {"id": jid, "kind": kind, "status": "queued", "created": time.time()}
    return jid


@app.post("/api/scan")
async def start_scan(req: ScanReq, request: Request):
    ip = _client_ip(request)
    _check_rate(ip)
    try:
        url = normalize_url(req.url)
        assert_public_host(normalize_url(req.url).split("//")[1].split("/")[0].split(":")[0])
    except UnsafeTarget as exc:
        raise HTTPException(400, str(exc))
    jid = _new_job("scan")
    asyncio.create_task(_run_scan(jid, url))
    return {"job": jid}


@app.post("/api/canvas")
async def start_canvas(req: CanvasReq, request: Request):
    ip = _client_ip(request)
    _check_rate(ip)
    try:
        url = normalize_url(req.url)
        assert_public_host(normalize_url(req.url).split("//")[1].split("/")[0].split(":")[0])
    except UnsafeTarget as exc:
        raise HTTPException(400, str(exc))
    jid = _new_job("canvas")
    asyncio.create_task(_run_canvas(jid, url))
    return {"job": jid}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown or expired job.")
    return job


# --------------------------------------------------------------------------
# synchronous modules
# --------------------------------------------------------------------------

@app.post("/api/copy")
async def copy_rewrite(req: CopyReq):
    res = rewrite(req.text, req.vertical)
    return {
        "original": res.original,
        "rewritten": res.rewritten,
        "risk": res.risk,
        "applied": [a.__dict__ for a in res.applied],
        "lint_before": [l.__dict__ for l in res.lint_before],
        "lint_after": [l.__dict__ for l in res.lint_after],
        "warnings": res.warnings,
    }


@app.post("/api/rsa")
async def rsa(req: RsaReq):
    out = build_rsa(req.vertical, req.geo, req.brand, req.product)
    _log({"kind": "rsa", "vertical": req.vertical, "geo": req.geo, "brand": req.brand})
    return out


@app.post("/api/conversions")
async def conversions(req: ConvReq):
    out = simulate(
        impressions=req.impressions, tCPA=req.target_cpa, budget=req.budget,
        injection_rate=req.injection_rate, dedup_effectiveness=req.dedup_effectiveness,
        value_based=req.value_based, seed=req.seed,
    )
    _log({"kind": "conversions", "inputs": out["inputs"], "totals": out["totals"],
          "verdict": out["verdict"]})
    return out


@app.get("/api/experiments")
async def experiments(limit: int = 40):
    if not LOG.exists():
        return []
    lines = LOG.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


@app.get("/artifacts/{name}")
async def artifact(name: str):
    if "/" in name or ".." in name:
        raise HTTPException(400, "Bad artifact name.")
    path = ARTIFACT_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Artifact not found.")
    media = "text/html" if name.endswith(".html") else "image/png" if name.endswith(".png") else "application/octet-stream"
    headers = {"Cache-Control": "public, max-age=600"}
    if name.endswith(".html"):
        # Clone artifacts are inert but never let them share an origin policy.
        headers["Content-Security-Policy"] = "sandbox allow-scripts"
    return FileResponse(path, media_type=media, headers=headers, filename=None)


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return JSONResponse({"error": "not found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)),
                log_level="warning")

"""FastAPI web service for Schemattiq.

- POST /api/design         runs the Render Workflow pipeline, logs the session,
                            and places a Vobiz HITL call when the BOM is over the
                            call threshold
- POST /api/voice-summary  ElevenLabs spoken summary (audio/mpeg)
- POST /api/transcribe     ElevenLabs speech-to-text for voice-to-spec
- POST /api/hitl/simulate  ₹15,400 drone BOM demo -> Grantex block + Vobiz call
- GET  /api/hitl/{id}      HITL status for the UI to poll
- /vobiz/*                 VobizXML webhooks (answer, gather, hangup)
- GET  /api/stats, /api/sessions, /api/config, /health
- GET  /                   the frontend (frontend/index.html)

    uvicorn api:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field
from render import RenderAsync

import greptile_hw  # noqa: F401 - loads .env for local dev
import pinelabs
import store
import vobiz
import voice

# "<workflow service name>/<task name>"
PIPELINE_SLUG = os.getenv("PIPELINE_TASK_SLUG", "schemattiq-workflow/schemattiq_pipeline")
# Over-budget BOMs above this total also trigger a live Vobiz call.
VOBIZ_CALL_THRESHOLD_INR = float(os.getenv("VOBIZ_CALL_THRESHOLD_INR", "5000"))
FRONTEND = Path(__file__).parent / "frontend" / "index.html"

app = FastAPI(title="Schemattiq API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
store.init()


class DesignRequest(BaseModel):
    idea: str = Field(min_length=3, max_length=2000)
    budget_inr: int = Field(gt=0, le=10_000_000)
    phone: str = Field(default="", max_length=20)
    client_id: str | None = Field(default=None, max_length=64)


class SummaryRequest(BaseModel):
    session_id: str | None = None
    result: dict | None = None
    # Speak this text as-is instead of summarising a design (handy for testing).
    text: str | None = Field(default=None, min_length=1, max_length=600)


class SimulateRequest(BaseModel):
    phone: str = Field(default="", max_length=20)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "pipeline": PIPELINE_SLUG}


@app.get("/api/config")
async def config() -> dict:
    return {
        "vobiz_enabled": vobiz.configured(),
        "voice_enabled": bool(os.getenv("ELEVENLABS_API_KEY")),
        "vobiz_call_threshold_inr": VOBIZ_CALL_THRESHOLD_INR,
        "pinelabs_checkout_url": os.getenv("PINELABS_CHECKOUT_URL", ""),
        "pinelabs_enabled": pinelabs.configured(),
        "pinelabs_test_mode": "uat" in pinelabs._base(),
    }


@app.get("/api/stats")
async def stats() -> dict:
    return store.stats()


def _require_admin(request: Request) -> None:
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN is not set on the server")
    given = request.headers.get("X-Admin-Token", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Wrong or missing admin token")


@app.get("/api/sessions")
async def sessions(request: Request, limit: int = 50) -> list[dict]:
    # Contains what people typed, so it's admin-only.
    _require_admin(request)
    return store.recent_sessions(min(limit, 200))


class TrackRequest(BaseModel):
    client_id: str | None = Field(default=None, max_length=64)
    source: str | None = Field(default=None, max_length=100)
    referrer: str | None = Field(default=None, max_length=500)


def _visit_source(req: TrackRequest, request: Request) -> str:
    tag = re.sub(r"[^a-z0-9_.-]", "", (req.source or "").lower())[:40]
    if tag:
        return tag
    host = (urlparse(req.referrer or "").hostname or "").removeprefix("www.")
    own = {request.url.hostname, "localhost", "127.0.0.1"}
    return host if host and host not in own else "direct"


@app.post("/api/track", status_code=204)
async def track(req: TrackRequest, request: Request) -> Response:
    """Anonymous page-view log: browser ID, traffic source, device type and a
    salted hash of the IP (the raw IP is never stored)."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else "")
    salt = os.getenv("ANALYTICS_SALT") or os.getenv("ADMIN_TOKEN") or "schemattiq"
    ip_hash = hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:16] if ip else None
    ua = request.headers.get("User-Agent", "")
    device = "mobile" if re.search(r"Mobi|Android|iPhone|iPad", ua) else "desktop"
    store.log_visit(req.client_id, _visit_source(req, request), device, ip_hash)
    return Response(status_code=204)


@app.get("/api/admin/usage")
async def admin_usage(request: Request) -> dict:
    _require_admin(request)
    return store.usage()


@app.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(FRONTEND.with_name("admin.html"))


async def _start_hitl_call(title: str, total: float, budget: float, phone: str,
                           session_id: str | None = None) -> dict:
    hid = store.create_hitl(title, total, budget, phone, session_id)
    if not phone:
        return {"id": hid, "status": "HITL_REQUIRED", "detail": "No phone number given, so no approval call was placed."}
    try:
        call = await vobiz.place_approval_call(phone, hid)
    except vobiz.VobizError as e:
        store.update_hitl(hid, "CALL_FAILED", detail=str(e))
        return {"id": hid, "status": "CALL_FAILED", "detail": str(e)}
    store.update_hitl(hid, "CALLING", call_uuid=call.get("request_uuid"))
    return {"id": hid, "status": "CALLING"}


@app.post("/api/design")
async def design(req: DesignRequest) -> dict:
    sid = store.start_session(req.idea, req.budget_inr, req.client_id)
    try:
        run = await RenderAsync().workflows.run_task(PIPELINE_SLUG, [req.idea, req.budget_inr, ""])
    except Exception as e:  # noqa: BLE001 - surface workflow errors to the client
        store.finish_session(sid, None, error=str(e))
        raise HTTPException(status_code=502, detail=f"Workflow run failed: {e}") from e

    status = str(getattr(run.status, "value", run.status))
    if status != "completed" or not run.results:
        error = getattr(run, "error", None) or status
        store.finish_session(sid, None, error=str(error))
        raise HTTPException(status_code=502, detail={"task_run_id": run.id, "status": status, "error": error})

    result = run.results[0]
    store.finish_session(sid, result)

    hitl = None
    total = result.get("total_inr")
    if (result.get("hitl_required") and isinstance(total, (int, float))
            and total > VOBIZ_CALL_THRESHOLD_INR):
        hitl = await _start_hitl_call(req.idea[:60], total, req.budget_inr, req.phone, sid)
    return {"session_id": sid, "task_run_id": run.id, "result": result, "hitl": hitl}


@app.post("/api/voice-summary")
async def voice_summary(req: SummaryRequest) -> Response:
    if req.text:
        text = req.text.strip()
    else:
        result = req.result or (store.get_session_result(req.session_id) if req.session_id else None)
        if not result:
            raise HTTPException(status_code=404, detail="No result for that session")
        text = voice.summary_text(result)
    try:
        audio = await voice.text_to_speech(text)
    except voice.VoiceError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return Response(audio, media_type="audio/mpeg", headers={"X-Summary-Text": text.encode("ascii", "ignore").decode()})


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)) -> dict:
    data = await audio.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio too large (max 10 MB)")
    try:
        text = await voice.speech_to_text(data, audio.filename or "speech.webm", audio.content_type or "audio/webm")
    except voice.VoiceError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"text": text}


@app.post("/api/hitl/simulate")
async def simulate_hitl(req: SimulateRequest) -> dict:
    """Demo: a ₹15,400 drone BOM against a ₹10,000 policy cap."""
    hitl = await _start_hitl_call("drone", 15400, 10000, req.phone)
    return {"grantex_posture": "HITL_REQUIRED", "checkout": "BLOCKED", "hitl": hitl}


@app.get("/api/hitl/{hid}")
async def hitl_status(hid: str) -> dict:
    h = store.get_hitl(hid)
    if not h:
        raise HTTPException(status_code=404, detail="Unknown HITL request")
    h.pop("phone_last4", None)
    return h


# --- Pine Labs checkout -----------------------------------------------------

class CheckoutRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


@app.post("/api/checkout")
async def checkout(req: CheckoutRequest) -> dict:
    """Create (or reuse) a Pine Labs payment link for an approved design.
    The amount comes from the stored result, never from the browser."""
    result = store.get_session_result(req.session_id)
    if not result:
        raise HTTPException(status_code=404, detail="Unknown design session")
    posture = str(result.get("grantex_posture", ""))
    if not (posture.startswith("APPROVED") or store.hitl_approved_for_session(req.session_id)):
        raise HTTPException(status_code=403, detail=f"Checkout blocked by Grantex ({posture})")
    total = result.get("total_inr")
    if not isinstance(total, (int, float)) or total < 1:
        raise HTTPException(status_code=409, detail="This design has no priced BOM total")

    existing = store.payment_for_session(req.session_id)
    if existing and existing["status"] in ("CREATED", "PROCESSED", "PAID"):
        return {**existing, "payment_link": existing["url"], "test_mode": "uat" in pinelabs._base()}
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    try:
        link = await pinelabs.create_payment_link(
            total, reference=f"sq-{req.session_id}-{uuid.uuid4().hex[:6]}",
            description=f"Schemattiq BOM: {(result.get('design') or {}).get('mcu') or 'hardware kit'}",
            callback_url=f"{base}/pay/return" if base else None,
        )
    except pinelabs.PineLabsError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    store.save_payment(link["payment_link_id"], req.session_id, total, link["payment_link"], link.get("status", "CREATED"))
    return {"payment_link_id": link["payment_link_id"], "payment_link": link["payment_link"],
            "status": link.get("status", "CREATED"), "amount_inr": total, "test_mode": "uat" in pinelabs._base()}


@app.get("/api/checkout/{payment_link_id}")
async def checkout_status(payment_link_id: str) -> dict:
    p = store.get_payment(payment_link_id)
    if not p:
        raise HTTPException(status_code=404, detail="Unknown payment link")
    try:
        link = await pinelabs.get_payment_link(payment_link_id)
        store.update_payment_status(payment_link_id, link.get("status", p["status"]))
        p["status"] = link.get("status", p["status"])
    except pinelabs.PineLabsError as e:
        p["detail"] = str(e)
    return p


@app.get("/pay/return")
async def pay_return() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Schemattiq payment</title><body style='font:16px system-ui;background:#0b0d12;color:#e6e9f0;"
        "display:grid;place-items:center;min-height:100vh;margin:0'><div style='text-align:center;padding:16px'>"
        "<h1>Payment submitted</h1><p>You can close this tab and return to Schemattiq — the BOM tab "
        "shows the confirmed status.</p></div>")


# --- Vobiz webhooks ---------------------------------------------------------

def _xml(body: str) -> Response:
    return Response(body, media_type="application/xml")


def _webhook_hitl(request: Request, form: dict) -> dict:
    h = store.get_hitl(request.query_params.get("hitl_id", ""))
    if not h:
        raise HTTPException(status_code=404)
    call_uuid = form.get("CallUUID")
    if h.get("call_uuid") and call_uuid and call_uuid != h["call_uuid"]:
        raise HTTPException(status_code=403)
    return h


@app.api_route("/vobiz/answer", methods=["GET", "POST"])
async def vobiz_answer(request: Request) -> Response:
    form = dict(await request.form()) if request.method == "POST" else {}
    h = _webhook_hitl(request, form)
    base = os.getenv("PUBLIC_BASE_URL", str(request.base_url)).rstrip("/")
    return _xml(vobiz.answer_xml(h, f"{base}/vobiz/gather?hitl_id={h['id']}"))


@app.post("/vobiz/gather")
async def vobiz_gather(request: Request) -> Response:
    form = dict(await request.form())
    h = _webhook_hitl(request, form)
    digit = str(form.get("Digits", "")).strip()
    approved = {"1": True, "2": False}.get(digit)
    if approved is not None:
        store.update_hitl(h["id"], "APPROVED" if approved else "REJECTED", detail=f"DTMF {digit}")
    return _xml(vobiz.result_xml(approved))


@app.post("/vobiz/hangup")
async def vobiz_hangup(request: Request) -> Response:
    form = dict(await request.form())
    h = _webhook_hitl(request, form)
    if h["status"] == "CALLING":
        store.update_hitl(h["id"], "NO_ANSWER", detail=form.get("HangupCause"))
    return Response(status_code=204)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND)


@app.get("/config.js")
async def frontend_config() -> FileResponse:
    return FileResponse(FRONTEND.with_name("config.js"), media_type="application/javascript")

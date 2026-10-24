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

import os
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from render import RenderAsync

import greptile_hw  # noqa: F401 - loads .env for local dev
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
    }


@app.get("/api/stats")
async def stats() -> dict:
    return store.stats()


@app.get("/api/sessions")
async def sessions(limit: int = 50) -> list[dict]:
    return store.recent_sessions(min(limit, 200))


async def _start_hitl_call(title: str, total: float, budget: float, phone: str,
                           session_id: str | None = None) -> dict:
    hid = store.create_hitl(title, total, budget, phone, session_id)
    if not phone:
        return {"id": hid, "status": "HITL_REQUIRED", "detail": "No phone number given; approve in AgenticOrg."}
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

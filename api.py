"""FastAPI web service: one HTTP call from the Replit frontend runs the whole
Schemattiq pipeline as a Render Workflow task and returns its JSON result.

    uvicorn api:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from render import RenderAsync

# "<workflow service name>/<task name>"
PIPELINE_SLUG = os.getenv("PIPELINE_TASK_SLUG", "schemattiq-workflow/schemattiq_pipeline")

app = FastAPI(title="Schemattiq API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class DesignRequest(BaseModel):
    idea: str = Field(min_length=3, max_length=2000)
    budget_inr: int = Field(gt=0, le=10_000_000)
    phone: str = ""


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "pipeline": PIPELINE_SLUG}


@app.post("/api/design")
async def design(req: DesignRequest) -> dict:
    render = RenderAsync()
    try:
        run = await render.workflows.run_task(PIPELINE_SLUG, [req.idea, req.budget_inr, req.phone])
    except Exception as e:  # noqa: BLE001 - surface workflow errors to the client
        raise HTTPException(status_code=502, detail=f"Workflow run failed: {e}") from e

    status = str(getattr(run.status, "value", run.status))
    if status != "completed":
        raise HTTPException(status_code=502, detail={
            "task_run_id": run.id, "status": status, "error": getattr(run, "error", None),
        })
    return {"task_run_id": run.id, "result": run.results[0] if run.results else None}

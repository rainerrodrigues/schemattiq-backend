# Schemattiq backend

Turns a hardware idea + budget into a firmware/circuit design, compile-checks the
firmware in a **Render Sandbox**, prices the BOM, and gates spend — orchestrated as
a **Render Workflow**.

```
Replit frontend ──POST /api/design──▶ api.py (Render Web Service)
                                         │ render.workflows.run_task
                                         ▼
                         workflows_worker.py (Render Workflow)
   schemattiq_pipeline:
     1. Greptile  → grounded headers + safe ESP32 GPIOs (falls back to local pin rules)
     2. AgenticOrg Rohan_Firmware_Lead → design_hardware_spec
     3. ctx.run(verify_firmware_in_sandbox) → g++ -fsyntax-only in a Render Sandbox
     4. AgenticOrg Meera_BOM_Buyer → buyer_discovery_preview (OACP)
     5. Grantex budget gate → APPROVED / HITL_ESCALATED_*
     6. Vobiz voice HITL flag (Step 4)
```

## Files

| File | Purpose |
| --- | --- |
| `workflows_worker.py` | Render Workflow tasks `schemattiq_pipeline` and `verify_firmware_in_sandbox` |
| `api.py` | FastAPI wrapper: `POST /api/design`, `GET /health` |
| `greptile_hw.py` | Greptile query + ESP32 GPIO safety checks |
| `sandbox_stubs/` | Arduino/Wire/SPI stub headers so sketches pass `g++ -fsyntax-only` |

## Deploy on Render

1. **Workflow** — Dashboard → New → Workflow, connect this repo.
   - Name: `schemattiq-workflow` (must match `PIPELINE_TASK_SLUG`)
   - Build: `pip install -r requirements.txt`
   - Start: `render-workflows workflows_worker:app`
   - Env: `AGENTICORG_API_KEY`, `AGENTICORG_COMPANY_ID`, `GREPTILE_API_KEY`, `GITHUB_TOKEN`,
     `RENDER_API_KEY`, `RENDER_WORKSPACE_ID` (see `.env.example`)
2. **Web service** — Dashboard → New → Blueprint with this repo (`render.yaml`),
   then set `RENDER_API_KEY`.

## API

```http
POST /api/design
{"idea": "Soil moisture monitor with OLED and pump relay", "budget_inr": 2500, "phone": "+91..."}
```

Returns `{"task_run_id": "...", "result": {architecture, sandbox_verification, oacp_bom_cart,
total_inr, grantex_posture, vobiz_hitl_called, ...}}`.

`sandbox_verification.status` is one of `ok`, `syntax_error`, `missing_library`
(a third-party header like `Adafruit_BME280.h` isn't available in the sandbox),
or `no_toolchain`.

## Local dev

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in keys
uvicorn api:app --reload
```

# Schemattiq

Multi-agent firmware, wiring and governed procurement studio. Describe (or speak)
a gadget and a ₹ budget cap; a governed AgenticOrg hardware company designs the
circuit, a **Render Sandbox** compile-checks the firmware, the BOM is priced
against Indian vendors, and a Grantex CFO guard approves or escalates. Over-budget
escalations ring a real phone through **Vobiz** for press-1 approval.

```
Replit / Render  frontend/index.html
        │  POST /api/design          (one HTTP call)
        ▼
api.py  (Render Web Service) ── SQLite session log, ElevenLabs voice, Vobiz HITL webhooks
        │  render.workflows.run_task
        ▼
workflows_worker.py  (Render Workflow)
  schemattiq_pipeline
    1. Greptile → grounded headers + safe ESP32 GPIOs (falls back to local pin rules)
    2. AgenticOrg Rohan_Firmware_Lead → design_hardware_spec (strict JSON)
    3. ctx.run(verify_firmware_in_sandbox) → g++ / iverilog in a Render Sandbox
    4. AgenticOrg Meera_BOM_Buyer → buyer_discovery_preview (OACP)
    5. Priya_CFO_Guard policy → over cap: re-quote with cheaper swaps, then HITL
        │
        ▼  over budget AND total > ₹5,000 AND phone given
api.py → Vobiz POST /Account/{id}/Call/ → /vobiz/answer (Speak + Gather) → /vobiz/gather (Digits=1 → APPROVED)
```

## Files

| File | Purpose |
| --- | --- |
| `workflows_worker.py` | Render Workflow tasks `schemattiq_pipeline`, `verify_firmware_in_sandbox` |
| `api.py` | FastAPI: design, voice, HITL, Vobiz webhooks, stats, serves the frontend |
| `greptile_hw.py` | Greptile query + ESP32 GPIO safety checks |
| `voice.py` | ElevenLabs TTS (`eleven_flash_v2_5`) and STT (Scribe) |
| `vobiz.py` | Vobiz outbound call + VobizXML |
| `store.py` | SQLite log of sessions and HITL approvals |
| `frontend/index.html` | Single-screen studio (no build step) |
| `sandbox_stubs/` | Arduino/Wire/SPI stub headers for `g++ -fsyntax-only` |
| `agenticorg_setup/` | Org chart CSV, agent prompts, setup check script |

## API

| Method | Path | |
| --- | --- | --- |
| POST | `/api/design` | `{idea, budget_inr, phone?, client_id?}` → `{session_id, result, hitl}` |
| POST | `/api/voice-summary` | `{session_id}` → `audio/mpeg` |
| POST | `/api/transcribe` | multipart `audio` → `{text}` |
| POST | `/api/hitl/simulate` | `{phone}` → ₹15,400 drone BOM, Grantex block + Vobiz call |
| GET | `/api/hitl/{id}` | `HITL_REQUIRED` · `CALLING` · `APPROVED` · `REJECTED` · `NO_ANSWER` · `CALL_FAILED` |
| GET | `/api/stats`, `/api/sessions` | Live counters and the session log (demo proof) |
| GET | `/api/config`, `/health` | |

## Deploy

1. **AgenticOrg**: follow [`agenticorg_setup/README.md`](agenticorg_setup/README.md).
2. **Render Workflow**: Dashboard → New → Workflow, connect this repo.
   - Name `schemattiq-workflow` (must match `PIPELINE_TASK_SLUG`)
   - Build `pip install -r requirements.txt` · Start `render-workflows workflows_worker:app`
   - Env: AgenticOrg, Greptile and Render variables from `.env.example`
3. **Render Web Service**: Dashboard → New → Blueprint with this repo (`render.yaml`).
   Fill in the `sync: false` variables. Set `PUBLIC_BASE_URL` to the service's own URL.
4. **Replit frontend**: create an HTML/CSS/JS Repl, paste `frontend/index.html`, and
   set `window.SCHEMATTIQ_API` at the top to the web service URL. The web service
   also serves the same page at `/`, so it works there as a fallback.

## Local dev

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in keys
uvicorn api:app --reload
```

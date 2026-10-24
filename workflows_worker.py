"""Render Workflow worker: the Schemattiq design pipeline.

Tasks:
  verify_firmware_in_sandbox  - syntax-checks generated C++ in a Render Sandbox
  schemattiq_pipeline         - Greptile context -> Rohan (firmware) -> sandbox
                                check -> Meera (BOM buyer) -> Grantex budget gate

Run on Render as a Workflow with start command:
    render-workflows workflows_worker:app
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

from agenticorg import AgenticOrg, AgentRunResult
from render import RenderAsync, TaskContext, Workflows
from render.experimental.sandbox import SandboxExecExit, SandboxExecOutput

import greptile_hw

app = Workflows()

# Agent UUIDs, or agent types (types also need AGENTICORG_COMPANY_ID).
ROHAN_AGENT = os.getenv("ROHAN_AGENT_ID", "Rohan_Firmware_Lead")
MEERA_AGENT = os.getenv("MEERA_AGENT_ID", "Meera_BOM_Buyer")
BOM_MERCHANT_ID = os.getenv("BOM_MERCHANT_ID", "schemattiq_india_bom")

STUBS_DIR = Path(__file__).parent / "sandbox_stubs"
FALLBACK_FIRMWARE = "void setup() {}\nvoid loop() {}\n"


def _ao_client() -> AgenticOrg:
    return AgenticOrg(api_key=os.environ["AGENTICORG_API_KEY"])


async def _run_agent(agent: str, action: str, inputs: dict) -> AgentRunResult:
    # The AgenticOrg SDK is synchronous; keep it off the event loop.
    def call() -> AgentRunResult:
        with _ao_client() as client:
            return client.agents.run(
                agent, action=action, inputs=inputs,
                company_id=os.getenv("AGENTICORG_COMPANY_ID") or None,
            )
    return await asyncio.to_thread(call)


def _agent_summary(result: AgentRunResult) -> dict:
    d = asdict(result)
    d.pop("raw", None)
    return d


def _extract_firmware(output: dict) -> str:
    for key in ("firmware_stub", "firmware_code", "firmware", "code"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return FALLBACK_FIRMWARE


@app.task(name="verify_firmware_in_sandbox", timeout_seconds=180)
async def verify_firmware_in_sandbox(ctx: TaskContext, firmware_code: str) -> dict:
    """Syntax-check generated firmware with g++ inside a Render Sandbox.

    Arduino.h/Wire.h are stubbed so plain sketches compile. Third-party library
    headers can't be resolved, so those are reported as `missing_library`
    rather than as a syntax error.
    """
    sandboxes = RenderAsync().experimental.sandboxes
    sb = await sandboxes.create(timeout_seconds=300)
    try:
        # Upload as files instead of a heredoc so the code can't break the shell.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "fw"
            (work / "stubs").mkdir(parents=True)
            (work / "main.cpp").write_text(firmware_code, encoding="utf-8")
            for stub in STUBS_DIR.glob("*.h"):
                (work / "stubs" / stub.name).write_text(stub.read_text(encoding="utf-8"), encoding="utf-8")
            await sandboxes.copy_to(sb.id, work, "fw")

        cmd = (
            "cd ~/fw && command -v g++ >/dev/null || { echo 'NO_TOOLCHAIN'; exit 127; }; "
            "g++ -std=gnu++17 -fsyntax-only -Wall -I stubs -x c++ main.cpp 2>&1"
        )
        chunks, exit_code = [], None
        async for event in sandboxes.exec(sb.id, cmd):
            if isinstance(event, SandboxExecOutput):
                chunks.append(event.data)
            elif isinstance(event, SandboxExecExit):
                exit_code = event.exit_code
        log = "".join(chunks)
    finally:
        await sandboxes.terminate(sb.id)

    missing = re.findall(r"fatal error: ([\w./-]+): No such file", log)
    if "NO_TOOLCHAIN" in log:
        status = "no_toolchain"
    elif exit_code == 0:
        status = "ok"
    elif missing:
        status = "missing_library"
    else:
        status = "syntax_error"
    return {
        "sandbox_id": sb.id,
        "verified": status == "ok",
        "status": status,
        "exit_code": exit_code,
        "missing_headers": missing,
        "log": log[-2000:],
    }


@app.task(name="schemattiq_pipeline", timeout_seconds=600)
async def schemattiq_pipeline(ctx: TaskContext, idea: str, budget_inr: int, phone: str = "") -> dict:
    # 1. Greptile ground truth. Never fatal: fall back to local ESP32 pin rules.
    try:
        hw = await asyncio.to_thread(greptile_hw.query_hardware_context, [idea])
        hw_context, hw_source = hw.to_prompt(), "greptile"
    except Exception as e:  # noqa: BLE001 - any Greptile failure degrades gracefully
        hw_context = (f"Safe ESP32 output GPIOs: {greptile_hw.ESP32_SAFE_OUTPUT}. "
                      "Avoid flash pins 6-11, strapping pins 0/2/5/12/15; 34-39 are input-only.")
        hw_source = f"fallback ({type(e).__name__}: {e})"[:300]

    # 2. Rohan: firmware + circuit architecture.
    arch = await _run_agent(ROHAN_AGENT, "design_hardware_spec", {
        "prompt": idea, "greptile_context": hw_context, "budget_inr": budget_inr,
    })

    # 3. Chained task on its own compute: compile-check in a Render Sandbox.
    sandbox_check = await ctx.run(verify_firmware_in_sandbox, _extract_firmware(arch.output))

    # 4. Meera: OACP buyer discovery for the BOM.
    commerce = await _run_agent(MEERA_AGENT, "buyer_discovery_preview", {
        "merchant_id": BOM_MERCHANT_ID, "design": arch.output, "max_budget_inr": budget_inr,
    })

    # 5. Grantex spend posture. A missing total is escalated, never auto-approved.
    total = commerce.output.get("total_inr")
    if not isinstance(total, (int, float)):
        grantex_status = "HITL_ESCALATED_UNKNOWN_COST"
    elif total <= budget_inr:
        grantex_status = "APPROVED"
    else:
        grantex_status = "HITL_ESCALATED_OVER_BUDGET"

    # 6. Vobiz voice HITL call is wired in Step 4; flag whether it should fire.
    needs_call = grantex_status != "APPROVED" and bool(phone) and bool(os.getenv("VOBIZ_AUTH_ID"))

    return {
        "hardware_context_source": hw_source,
        "architecture": _agent_summary(arch),
        "sandbox_verification": sandbox_check,
        "oacp_bom_cart": _agent_summary(commerce),
        "total_inr": total,
        "budget_inr": budget_inr,
        "grantex_posture": grantex_status,
        "vobiz_hitl_called": needs_call,
    }


if __name__ == "__main__":
    app.start()

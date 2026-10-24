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
import json
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
# Priya_CFO_Guard: how many times an over-budget cart goes back to Meera for swaps.
MAX_REQUOTES = int(os.getenv("MAX_REQUOTES", "1"))

STUBS_DIR = Path(__file__).parent / "sandbox_stubs"
FALLBACK_FIRMWARE = "void setup() {}\nvoid loop() {}\n"


# Output contracts sent in each run's `task`, so a built-in chat-style agent
# (e.g. solution_architect_agent, ecommerce_buyer) can stand in for Rohan/Meera.
ROHAN_TASK = (
    "Act as a Senior Mechatronics & Embedded Architect. Design the circuit and ESP32 firmware for: {idea}. "
    "Budget: ₹{budget}. Respect these pin rules and headers:\n{context}\n"
    "Reply with ONLY a JSON object: {{\"mcu\": str, \"components\": [{{\"name\": str, \"qty\": int, "
    "\"est_inr\": number, \"pin_mapping\": {{\"<signal>\": <gpio int>}}}}], \"mermaid_wiring_diagram\": "
    "str (Mermaid flowchart, no code fences), \"firmware_code\": str (compilable ESP32 Arduino C++)}}. "
    "Never assign conflicting GPIOs or strapping pins. List only real parts in `components` "
    "(no total or subtotal rows)."
)
MEERA_TASK = (
    "Act as a supply-chain buyer for Indian electronics (Robu.in, Element14, SP Road prices). Price every "
    "component in `design.components` with an Indian SKU and GST HSN code (8542/8544 etc.); budget ₹{budget}. "
    "{reason}Reply with ONLY a JSON object: {{\"total_inr\": number (GST-inclusive), \"line_items\": "
    "[{{\"name\": str, \"sku\": str, \"vendor\": str, \"qty\": int, \"unit_inr\": number, \"hsn\": str}}], "
    "\"swaps\": [{{\"from\": str, \"to\": str, \"saving_inr\": number}}]}}."
)

# Run statuses that pause the pipeline instead of producing output.
PAUSED_STATUSES = {
    "hitl_triggered": "HITL_PENDING_IN_AGENTICORG",  # waits for approval in the AgenticOrg UI
    "budget_exceeded": "AGENTICORG_BUDGET_EXCEEDED",
}


def _ao_client() -> AgenticOrg:
    # AGENTICORG_BASE_URL is read by the SDK; agent runs can take a while.
    return AgenticOrg(api_key=os.environ["AGENTICORG_API_KEY"], timeout=120)


async def _run_agent(agent: str, action: str, inputs: dict, task: str) -> AgentRunResult:
    """Run an AgenticOrg agent (UUID preferred). `task` is a plain-language
    version of the request, which chat-style agents read."""
    def call() -> AgentRunResult:
        with _ao_client() as client:
            return client.agents.run(
                agent, action=action, inputs={"task": task, **inputs},
                company_id=os.getenv("AGENTICORG_COMPANY_ID") or None,
            )
    # The AgenticOrg SDK is synchronous; keep it off the event loop.
    result = await asyncio.to_thread(call)
    if result.status == "failed":
        raise RuntimeError(f"AgenticOrg agent {agent} failed: {result.error or 'no error message'}")
    return result


def _agent_summary(result: AgentRunResult) -> dict:
    d = asdict(result)
    d.pop("raw", None)
    return d


def _agent_json(output: dict) -> dict:
    """Agents are prompted for strict JSON, but it may arrive as a string field
    (often fenced in ```json). Return the parsed object, else the output as-is."""
    if any(k in output for k in ("firmware_code", "components", "total_inr")):
        return output
    for value in output.values():
        if isinstance(value, str) and "{" in value:
            fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", value, re.S)
            text = fenced.group(1) if fenced else value[value.find("{"): value.rfind("}") + 1]
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return output


def _extract_firmware(spec: dict) -> str:
    for key in ("firmware_code", "firmware_stub", "firmware", "code"):
        value = spec.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return FALLBACK_FIRMWARE


_BUS_SIGNAL = re.compile(r"SDA|SCL|I2C|MOSI|MISO|SCK|SCLK|CLK|ONE_?WIRE|1WIRE", re.I)


def _is_mcu_board(name: str, mcu: str | None) -> bool:
    n = (name or "").lower()
    return bool(mcu and n and (n in mcu.lower() or mcu.lower().startswith(n))) or (
        "esp32" in n and any(w in n for w in ("board", "devkit", "module", "wroom", "wrover")))


def _pin_checks(spec: dict) -> list[dict]:
    """One row per peripheral pin with ESP32 safety warnings. The MCU board's
    own rows are dropped (they repeat the peripherals' pins), and a GPIO used
    by two different non-bus signals is flagged as a conflict."""
    rows = []
    for comp in spec.get("components") or []:
        if not isinstance(comp, dict) or _is_mcu_board(comp.get("name", ""), spec.get("mcu")):
            continue
        pins = comp.get("pin_mapping")
        if not isinstance(pins, dict):
            continue
        for signal, gpio in greptile_hw._parse_pins(pins).items():
            rows.append({"component": comp.get("name"), "signal": signal, "gpio": gpio,
                         "warning": greptile_hw.check_pin(signal, gpio)})
    users: dict[int, set] = {}
    for r in rows:
        if not _BUS_SIGNAL.search(r["signal"]):
            users.setdefault(r["gpio"], set()).add(f"{r['component']}.{r['signal']}")
    for r in rows:
        if not r["warning"] and len(users.get(r["gpio"], ())) > 1:
            r["warning"] = f"GPIO{r['gpio']} is shared by: {', '.join(sorted(users[r['gpio']]))}"
    return rows


def _is_hdl(code: str) -> bool:
    return bool(re.search(r"^\s*module\s+\w+", code, re.M) and "endmodule" in code)


def _total_inr(cart: dict) -> float | None:
    total = cart.get("total_inr")
    if isinstance(total, str):
        try:
            total = float(total.replace(",", "").replace("₹", "").strip())
        except ValueError:
            return None
    return total if isinstance(total, (int, float)) else None


@app.task(name="verify_firmware_in_sandbox", timeout_seconds=180)
async def verify_firmware_in_sandbox(ctx: TaskContext, firmware_code: str) -> dict:
    """Syntax-check generated firmware inside a Render Sandbox.

    C++: g++ -fsyntax-only with Arduino/Wire/SPI stubbed so plain sketches
    compile. Third-party library headers can't be resolved, so those are
    reported as `missing_library` rather than as a syntax error.
    SystemVerilog: iverilog -g2012 (or verilator --lint-only) if installed.
    """
    hdl = _is_hdl(firmware_code)
    sandboxes = RenderAsync().experimental.sandboxes
    try:
        sb = await sandboxes.create(timeout_seconds=300)
    except Exception as e:  # noqa: BLE001 - e.g. Sandboxes not enabled on the workspace (404)
        return {"sandbox_id": None, "language": "systemverilog" if hdl else "cpp", "verified": False,
                "status": "sandbox_unavailable", "exit_code": None, "missing_headers": [],
                "log": f"Could not create a Render Sandbox: {e}"[:2000]}
    try:
        # Upload as files instead of a heredoc so the code can't break the shell.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "fw"
            (work / "stubs").mkdir(parents=True)
            (work / ("main.sv" if hdl else "main.cpp")).write_text(firmware_code, encoding="utf-8")
            for stub in STUBS_DIR.glob("*.h"):
                (work / "stubs" / stub.name).write_text(stub.read_text(encoding="utf-8"), encoding="utf-8")
            await sandboxes.copy_to(sb.id, work, "fw")

        if hdl:
            cmd = (
                "cd ~/fw && if command -v iverilog >/dev/null; then iverilog -g2012 -o /dev/null main.sv 2>&1; "
                "elif command -v verilator >/dev/null; then verilator --lint-only -Wno-fatal main.sv 2>&1; "
                "else echo 'NO_TOOLCHAIN'; exit 127; fi"
            )
        else:
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
        "language": "systemverilog" if hdl else "cpp",
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

    trace = [{"agent": "Greptile", "step": "hardware ground truth", "status": hw_source.split(" ")[0]}]

    def paused(result: AgentRunResult, agent: str, step: str, **partial) -> dict:
        # The agent run is waiting in AgenticOrg (HITL approval or budget);
        # report that instead of continuing with an empty output.
        posture = PAUSED_STATUSES[result.status]
        trace.append({"agent": agent, "step": step, "status": result.status})
        return {"hardware_context_source": hw_source, "budget_inr": budget_inr, "total_inr": None,
                "grantex_posture": posture, "hitl_required": True,
                "agenticorg_run_id": result.run_id, "hitl_trigger": result.hitl_trigger,
                "trace": trace, **partial}

    # 2. Rohan: firmware + circuit architecture.
    arch = await _run_agent(ROHAN_AGENT, "design_hardware_spec", {
        "prompt": idea, "greptile_context": hw_context, "budget_inr": budget_inr,
    }, task=ROHAN_TASK.format(idea=idea, budget=budget_inr, context=hw_context))
    spec = _agent_json(arch.output)
    design = {
        "mcu": spec.get("mcu"),
        "components": spec.get("components", []),
        "mermaid_wiring_diagram": spec.get("mermaid_wiring_diagram"),
        "firmware_code": _extract_firmware(spec),
        "pin_checks": _pin_checks(spec),
    }
    if arch.status in PAUSED_STATUSES and not spec.get("mcu"):
        return paused(arch, "Rohan_Firmware_Lead", "design_hardware_spec")
    if arch.status not in PAUSED_STATUSES:
        trace.append({"agent": "Rohan_Firmware_Lead", "step": "design_hardware_spec", "status": arch.status})

    # 3. Chained task on its own compute: compile-check in a Render Sandbox.
    sandbox_check = await ctx.run(verify_firmware_in_sandbox, _extract_firmware(spec))

    # A design held for approval in AgenticOrg (e.g. confidence below the
    # agent's floor) is shown and compile-checked, but nothing is bought.
    if arch.status in PAUSED_STATUSES:
        result = paused(arch, "Rohan_Firmware_Lead", "design_hardware_spec",
                        design=design, sandbox_verification=sandbox_check)
        trace.append({"agent": "Render Sandbox", "step": "firmware syntax check",
                      "status": sandbox_check.get("status")})
        return result
    trace.append({"agent": "Render Sandbox", "step": "firmware syntax check", "status": sandbox_check.get("status")})

    # 4. Meera: OACP buyer discovery for the BOM.
    buyer_inputs = {"merchant_id": BOM_MERCHANT_ID, "design": spec, "max_budget_inr": budget_inr}
    commerce = await _run_agent(MEERA_AGENT, "buyer_discovery_preview", buyer_inputs,
                                task=MEERA_TASK.format(budget=budget_inr, reason=""))
    if commerce.status in PAUSED_STATUSES:
        return paused(commerce, "Meera_BOM_Buyer", "buyer_discovery_preview",
                      design=design, sandbox_verification=sandbox_check)
    cart = _agent_json(commerce.output)
    total = _total_inr(cart)

    # 5. Priya_CFO_Guard policy: over the cap -> back to Meera for cheaper
    #    drop-in swaps (MAX_REQUOTES times), then HITL. A missing total is
    #    escalated, never auto-approved.
    requotes = []
    while total is not None and total > budget_inr and len(requotes) < MAX_REQUOTES:
        requotes.append({"total_inr": total, "cart": cart})
        trace.append({"agent": "Meera_BOM_Buyer", "step": "buyer_discovery_preview",
                      "status": "re-quote requested", "total_inr": total})
        reason = (f"Over budget by ₹{total - budget_inr:.0f}. Swap to cheaper "
                  "drop-in parts with the same pin mapping and interface.")
        commerce = await _run_agent(MEERA_AGENT, "buyer_discovery_preview", {
            **buyer_inputs, "previous_cart": cart, "requote_reason": reason,
        }, task=MEERA_TASK.format(budget=budget_inr, reason=reason + " List each swap in `swaps`. "))
        if commerce.status in PAUSED_STATUSES:
            return paused(commerce, "Meera_BOM_Buyer", "buyer_discovery_preview (re-quote)",
                          design=design, sandbox_verification=sandbox_check, requotes=requotes)
        cart = _agent_json(commerce.output)
        total = _total_inr(cart)

    if total is None:
        grantex_status = "HITL_ESCALATED_UNKNOWN_COST"
    elif total <= budget_inr:
        grantex_status = "APPROVED_AFTER_SWAP" if requotes else "APPROVED"
    else:
        grantex_status = "HITL_ESCALATED_OVER_BUDGET"

    # 6. The web service (api.py) places the Vobiz HITL call, since it also
    #    hosts the call's webhooks. `phone` stays unused here so numbers don't
    #    land in workflow logs.
    trace.append({"agent": "Meera_BOM_Buyer", "step": "buyer_discovery_preview", "status": commerce.status,
                  "total_inr": total})
    trace.append({"agent": "Priya_CFO_Guard", "step": "Grantex spend gate", "status": grantex_status})
    return {
        "hardware_context_source": hw_source,
        "design": design,
        "architecture": _agent_summary(arch),
        "sandbox_verification": sandbox_check,
        "bom": cart,
        "oacp_bom_cart": _agent_summary(commerce),
        "requotes": requotes,
        "total_inr": total,
        "budget_inr": budget_inr,
        "grantex_posture": grantex_status,
        "hitl_required": grantex_status.startswith("HITL"),
        "trace": trace,
    }


if __name__ == "__main__":
    app.start()

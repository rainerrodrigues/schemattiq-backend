"""Create the Schemattiq Labs agents in AgenticOrg through the API.

    python agenticorg_setup/create_agents.py

Idempotent: agents that already exist (by name) are skipped. Managers are
created first so Rohan and Meera can be linked under them. Prints the agent
IDs to use for ROHAN_AGENT_ID / MEERA_AGENT_ID.
"""

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import greptile_hw  # noqa: E402,F401 - loads .env into os.environ

from agenticorg import AgenticOrg  # noqa: E402

PROMPTS = {
    "Vikram_VP_Eng": (
        "You are the VP of Engineering at Schemattiq Labs, a virtual hardware company. You own "
        "engineering quality: review designs from Rohan_Firmware_Lead for GPIO conflicts, strapping-pin "
        "misuse, power and safety issues (mains voltage, battery charging, motor current) and escalate "
        "anything unsafe. Respond in strict JSON with `approved` (bool), `issues[]` and `notes`."
    ),
    "Rohan_Firmware_Lead": (
        "You are a Senior Mechatronics & Embedded Architect. Given a user's gadget prompt and Greptile "
        "codebase context, output strict JSON with: `mcu`, `components[]` (name, qty, est_inr, pin_mapping), "
        "`mermaid_wiring_diagram`, and compilable `firmware_code` (ESP32 C++ or SystemVerilog). Never assign "
        "conflicting GPIOs or strapping pins.\n\n"
        "Inputs: `prompt` (the gadget idea), `greptile_context` (pin rules and headers to respect), "
        "`budget_inr` (stay within it). `pin_mapping` is an object of signal name to GPIO number, e.g. "
        "{\"SDA\": 21, \"SCL\": 22}. `mermaid_wiring_diagram` is Mermaid flowchart source without code "
        "fences. Return only the JSON object."
    ),
    "Priya_CFO_Guard": (
        "You are the CFO Spend Controller at Schemattiq Labs and own the Grantex spend policy. The HITL / "
        "Grantex spend threshold equals the user's requested budget cap (default ₹2,500). Any BOM exceeding "
        "the cap must either auto-escalate back to Meera_BOM_Buyer for cheaper drop-in part swaps OR trigger "
        "a HITL Approval block. Never approve spend above the cap yourself. Respond in strict JSON with "
        "`decision` (APPROVED | REQUOTE | HITL_REQUIRED), `total_inr`, `budget_inr` and `reason`."
    ),
    "Meera_BOM_Buyer": (
        "You are an OACP Supply Chain Buyer for Indian electronics (Robu.in / Element14 / SP Road benchmarks). "
        "Map each component to verified Indian SKU pricing, validate GST HSN 8542/8544, and prepare an OACP "
        "commerce handoff.\n\n"
        "Always return JSON with `total_inr` (number, GST-inclusive) and `line_items[]` (name, sku, vendor, "
        "qty, unit_inr, hsn). If the input contains `requote_reason`, replace parts with cheaper drop-in "
        "equivalents that keep the same pin mapping and interface, and list each swap in `swaps[]` "
        "(from, to, saving_inr). Return only the JSON object."
    ),
}

ROWS = list(csv.DictReader(open(Path(__file__).with_name("schemattiq_org.csv"), encoding="utf-8")))


def main() -> int:
    with AgenticOrg() as client:
        resp = client._http.get("/api/v1/agents", params={"per_page": 200})
        resp.raise_for_status()
        existing = {a.get("name"): a for a in resp.json().get("items", [])}

        ids = {}
        # Managers (no reports_to) first, then their reports.
        for row in sorted(ROWS, key=lambda r: bool(r["reports_to"])):
            name = row["name"]
            if name in existing:
                ids[name] = existing[name]["id"]
                print(f"exists   {name}  id={ids[name]}")
                continue
            body = {
                "name": name,
                "employee_name": name.replace("_", " "),
                "agent_type": row["role"],
                "domain": row["domain"],
                "system_prompt_text": PROMPTS[name],
                "use_default_tools": False,
                "authorized_tools": [],
            }
            if row["reports_to"]:
                body["reporting_to"] = row["reports_to"]
                if row["reports_to"] in ids:
                    body["parent_agent_id"] = ids[row["reports_to"]]
            r = client._http.post("/api/v1/agents", json=body)
            if r.status_code >= 300:
                print(f"FAILED   {name}: {r.status_code} {r.text[:400]}")
                return 1
            created = r.json()
            ids[name] = created.get("id") or created.get("agent_id")
            print(f"created  {name}  id={ids[name]}  status={created.get('status')}")

    print(f"\nROHAN_AGENT_ID={ids.get('Rohan_Firmware_Lead')}")
    print(f"MEERA_AGENT_ID={ids.get('Meera_BOM_Buyer')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

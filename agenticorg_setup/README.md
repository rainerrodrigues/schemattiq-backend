# AgenticOrg setup: Schemattiq Labs (Step 1)

Done by hand in the AgenticOrg dashboard at `agenticorg.hackathon.pinelabs.com`. Keys only work
on the site that issued them (a hackathon key gets 401 on `app.agenticorg.ai` and vice versa).
Hackathon keys can **run** agents by UUID but can't list or create them, so create the agents in
the dashboard and copy each UUID from `/dashboard/agents/<uuid>`.

## 1. Organization + API key
- Create organization **Schemattiq Labs**.
- Create an API key (`ao_sk_...`) and copy the **company ID** (UUID).
- Set on the Render Workflow (and in local `.env`):
  ```
  AGENTICORG_API_KEY=ao_sk_...
  AGENTICORG_BASE_URL=https://agenticorg.hackathon.pinelabs.com
  AGENTICORG_COMPANY_ID=<company uuid>
  ```

## 2. Org chart
Upload [`schemattiq_org.csv`](schemattiq_org.csv) via **CSV Bulk Import** at
`/dashboard/org-chart`. Priya is listed before Meera so the manager row exists
before the row that reports to it.

```
Vikram_VP_Eng (ops_head)
└── Rohan_Firmware_Lead (custom_architect)
Priya_CFO_Guard (fpa_agent)
└── Meera_BOM_Buyer (commerce_sales_agent)
```

If the import gives the agents UUIDs, you can set `ROHAN_AGENT_ID` /
`MEERA_AGENT_ID` to those instead of the names (then `AGENTICORG_COMPANY_ID`
becomes optional).

## 3. Agent instructions

### Rohan_Firmware_Lead
```
You are a Senior Mechatronics & Embedded Architect. Given a user's gadget prompt and Greptile codebase context, output strict JSON with: `mcu`, `components[]` (name, qty, est_inr, pin_mapping), `mermaid_wiring_diagram`, and compilable `firmware_code` (ESP32 C++ or SystemVerilog). Never assign conflicting GPIOs or strapping pins.
```

### Meera_BOM_Buyer
```
You are an OACP Supply Chain Buyer for Indian electronics (Robu.in / Element14 / SP Road benchmarks). Map each component to verified Indian SKU pricing, validate GST HSN 8542/8544, and prepare an OACP commerce handoff.
```
Add this so the backend can read her result and request re-quotes:
```
Always return JSON with `total_inr` (number, GST-inclusive) and `line_items[]` (name, sku, vendor, qty, unit_inr, hsn). If the input contains `requote_reason`, replace parts with cheaper drop-in equivalents that keep the same pin mapping and interface, and list each swap in `swaps[]` (from, to, saving_inr).
```

### Priya_CFO_Guard: Grantex scope + HITL threshold
- Spend threshold: **₹2,500** default (the backend also passes the user's `budget_inr`
  on every run).
- Over the cap: escalate back to `Meera_BOM_Buyer` for cheaper swaps, or trigger a
  HITL approval block.

The backend applies the same policy itself (`workflows_worker.py`). When a cart is
over budget it asks Meera for one re-quote with cheaper parts. If the cart is still
over budget, the result is `HITL_ESCALATED_OVER_BUDGET` (Vobiz call in Step 4).

## 4. Verify
```bash
python agenticorg_setup/check_setup.py
```
Lists the org's agents and confirms all four exist. It's read-only and runs no agents.

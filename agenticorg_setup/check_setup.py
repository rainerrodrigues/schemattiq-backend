"""Read-only check that the Schemattiq Labs org chart exists in AgenticOrg.

    python agenticorg_setup/check_setup.py
"""

import csv
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import greptile_hw  # noqa: E402,F401 - loads .env into os.environ

from agenticorg import AgenticOrg  # noqa: E402

EXPECTED = [row["name"] for row in csv.DictReader(open(Path(__file__).with_name("schemattiq_org.csv")))]


def main() -> int:
    base = os.getenv("AGENTICORG_BASE_URL")
    if not base:
        print("AGENTICORG_BASE_URL not set; the SDK would default to app.agenticorg.ai")
    with AgenticOrg() as client:
        # agents.list() only returns the first page; ask for everything.
        resp = client._http.get("/api/v1/agents", params={"per_page": 200})
        resp.raise_for_status()
        agents = resp.json().get("items", [])
    seen = {}
    for a in agents:
        for key in ("name", "agent_name", "display_name", "agent_type"):
            if a.get(key):
                seen[a[key]] = a
    missing = [n for n in EXPECTED if n not in seen]
    for n in EXPECTED:
        a = seen.get(n)
        print(f"{'OK ' if a else 'MISSING'}  {n}" + (f"  id={a.get('id')}  type={a.get('agent_type')}" if a else ""))
    if not os.getenv("AGENTICORG_COMPANY_ID"):
        print("Note: AGENTICORG_COMPANY_ID is empty; required when running agents by name.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())

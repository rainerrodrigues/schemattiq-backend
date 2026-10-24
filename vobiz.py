"""Vobiz: outbound PSTN call for Grantex HITL approval (press 1 to approve).

Docs: https://www.vobiz.ai/docs/call/make-call and /docs/xml/gather
"""

from __future__ import annotations

import os
import re
from xml.sax.saxutils import escape, quoteattr

import httpx

VOBIZ_BASE = "https://api.vobiz.ai/api/v1"


class VobizError(RuntimeError):
    pass


def configured() -> bool:
    return all(os.getenv(k) for k in ("VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN", "VOBIZ_FROM_NUMBER", "PUBLIC_BASE_URL"))


def normalize_phone(phone: str) -> str:
    """E.164 digits without '+'. Bare 10-digit numbers are assumed Indian (+91)."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        digits = "91" + digits
    if not 11 <= len(digits) <= 15:
        raise VobizError("Phone number must be 10 digits (India) or full international format")
    return digits


async def place_approval_call(to_phone: str, hitl_id: str) -> dict:
    if not configured():
        raise VobizError("Vobiz is not configured (VOBIZ_AUTH_ID/TOKEN/FROM_NUMBER, PUBLIC_BASE_URL)")
    auth_id = os.environ["VOBIZ_AUTH_ID"]
    base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.post(
            f"{VOBIZ_BASE}/Account/{auth_id}/Call/",
            headers={"X-Auth-ID": auth_id, "X-Auth-Token": os.environ["VOBIZ_AUTH_TOKEN"]},
            json={
                "from": normalize_phone(os.environ["VOBIZ_FROM_NUMBER"]),
                "to": normalize_phone(to_phone),
                "answer_url": f"{base}/vobiz/answer?hitl_id={hitl_id}",
                "answer_method": "POST",
                "hangup_url": f"{base}/vobiz/hangup?hitl_id={hitl_id}",
                "hangup_method": "POST",
                "caller_name": "Schemattiq CFO Guard",
                "time_limit": 120,
            },
        )
    if r.status_code >= 300:
        raise VobizError(f"Vobiz call {r.status_code}: {r.text[:300]}")
    return r.json()


def _rupees(amount: float) -> str:
    return f"{amount:,.0f} rupees"


def answer_xml(hitl: dict, action_url: str) -> str:
    total, cap = hitl["total_inr"], hitl["budget_inr"]
    prompt = (
        f"Alert from Schemattiq CFO Guard. Engineering requested a {_rupees(total)} "
        f"{hitl['title']} bill of materials, exceeding policy by {_rupees(total - cap)}. "
        "Press 1 to authorize the Grantex override and release the Pine Labs payment link. "
        "Press 2 to reject."
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
        f'  <Gather action={quoteattr(action_url)} method="POST" inputType="dtmf" '
        'numDigits="1" executionTimeout="15">\n'
        f'    <Speak voice="WOMAN" language="en-GB">{escape(prompt)}</Speak>\n'
        "  </Gather>\n"
        '  <Speak language="en-GB">No input received. The request stays blocked. Goodbye.</Speak>\n'
        "</Response>"
    )


def result_xml(approved: bool | None) -> str:
    msg = {
        True: "Approved. The Grantex override is recorded and the payment link is released. Goodbye.",
        False: "Rejected. The purchase stays blocked. Goodbye.",
        None: "Invalid input. The request stays blocked. Goodbye.",
    }[approved]
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
            f'  <Speak language="en-GB">{msg}</Speak>\n  <Hangup/>\n</Response>')

"""ElevenLabs: speech-to-text for voice-to-spec capture, and a spoken summary
of the generated design."""

from __future__ import annotations

import os

import httpx

ELEVEN_BASE = "https://api.elevenlabs.io/v1"
# "Rachel", a stock voice available on every account; override with your own.
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8mRAgk"


class VoiceError(RuntimeError):
    pass


def _key() -> str:
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        raise VoiceError("ELEVENLABS_API_KEY is not set")
    return key


def summary_text(result: dict) -> str:
    """Two-sentence spoken summary of a pipeline result."""
    design = result.get("design") or {}
    parts = [c.get("name") for c in design.get("components") or [] if isinstance(c, dict) and c.get("name")]
    mcu = design.get("mcu") or "an ESP32"
    using = f"using {mcu}" + (f" with {', '.join(parts[:3])}" if parts else "")
    first = f"Designed your gadget {using}."

    total = result.get("total_inr")
    posture = str(result.get("grantex_posture", ""))
    cost = f"{total:,.0f} rupees" if isinstance(total, (int, float)) else "not yet priced"
    if posture.startswith("APPROVED"):
        verdict = "Grantex budget approved."
    elif posture == "HITL_ESCALATED_OVER_BUDGET":
        verdict = "It exceeds your budget, so the CFO guard has escalated it for human approval."
    else:
        verdict = "The CFO guard has escalated it for human review."
    return f"{first} Total verified BOM is {cost}. {verdict}"


async def text_to_speech(text: str) -> bytes:
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            f"{ELEVEN_BASE}/text-to-speech/{voice_id}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": _key(), "Accept": "audio/mpeg"},
            json={"text": text, "model_id": os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")},
        )
    if r.status_code != 200:
        raise VoiceError(f"ElevenLabs TTS {r.status_code}: {r.text[:300]}")
    return r.content


async def speech_to_text(audio: bytes, filename: str, content_type: str) -> str:
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(
            f"{ELEVEN_BASE}/speech-to-text",
            headers={"xi-api-key": _key()},
            data={"model_id": os.getenv("ELEVENLABS_STT_MODEL", "scribe_v1")},
            files={"file": (filename, audio, content_type)},
        )
    if r.status_code != 200:
        raise VoiceError(f"ElevenLabs STT {r.status_code}: {r.text[:300]}")
    return r.json().get("text", "").strip()

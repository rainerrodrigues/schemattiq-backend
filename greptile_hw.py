"""Greptile hardware ground-truth helper.

Queries hardware repos connected in Greptile with a user's
sensor/actuator list, so the firmware agent (Rohan_Firmware_Lead) gets real
header imports and GPIO pins that are checked against a local safety table.

Stdlib only. Needs GREPTILE_API_KEY and GITHUB_TOKEN in the environment.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

GREPTILE_BASE = os.getenv("GREPTILE_BASE_URL", "https://api.greptile.com/v2")
# NOTE: /v2/query now returns 404; set GREPTILE_QUERY_PATH once the v3 path is known.
GREPTILE_QUERY_PATH = os.getenv("GREPTILE_QUERY_PATH", "/query")

# Repos to index. Both use "master" as their default branch.
# Adafruit_Sensor is tiny (<1 MB) and indexes fast; esp-iot-solution is ~200 MB,
# so expect it to take noticeably longer than 3 minutes on the first index.
HARDWARE_REPOS = [
    {"remote": "github", "repository": "adafruit/Adafruit_Sensor", "branch": "master"},
    {"remote": "github", "repository": "espressif/esp-iot-solution", "branch": "master"},
]

# ESP32 (classic) pin rules, used to veto whatever pins the LLM suggests.
ESP32_FLASH_PINS = {6, 7, 8, 9, 10, 11}          # wired to SPI flash, never use
ESP32_INPUT_ONLY = {34, 35, 36, 37, 38, 39}      # no output driver, no pull-ups
ESP32_STRAPPING = {0, 2, 5, 12, 15}              # affect boot mode
ESP32_VALID = set(range(0, 40)) - {20, 24, 28, 29, 30, 31}
ESP32_SAFE_OUTPUT = sorted(
    ESP32_VALID - ESP32_FLASH_PINS - ESP32_INPUT_ONLY - ESP32_STRAPPING - {1, 3}  # 1/3 = UART0
)


class GreptileError(RuntimeError):
    pass


def _load_dotenv(path: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")) -> None:
    """Minimal .env loader; real environment variables take precedence."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if value.strip():
                    os.environ.setdefault(key.strip(), value.strip().strip('"\''))


_load_dotenv()


def _headers() -> dict:
    if not os.environ.get("GREPTILE_API_KEY"):
        raise GreptileError("Missing environment variable GREPTILE_API_KEY")
    headers = {
        "Authorization": f"Bearer {os.environ['GREPTILE_API_KEY']}",
        "Content-Type": "application/json",
    }
    # Required for indexing and querying; status checks work without it.
    if os.environ.get("GITHUB_TOKEN"):
        headers["X-GitHub-Token"] = os.environ["GITHUB_TOKEN"]
    return headers


def _request(method: str, path: str, body: dict | None = None, timeout: float = 120) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GREPTILE_BASE + path, data=data, method=method, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise GreptileError(f"{method} {path} -> {e.code}: {e.read().decode(errors='replace')}") from None


# Indexing is no longer done via the API: Greptile retired POST /repositories and
# status polling (410 Gone). Connect repos in app.greptile.com instead.

# --- Querying ---------------------------------------------------------------

@dataclass
class ComponentSpec:
    name: str
    headers: list[str] = field(default_factory=list)
    driver: str | None = None
    bus: str | None = None
    pins: dict[str, int] = field(default_factory=dict)
    pin_warnings: list[str] = field(default_factory=list)


@dataclass
class HardwareContext:
    board: str
    components: list[ComponentSpec]
    sources: list[dict]
    raw_answer: str

    def to_prompt(self) -> str:
        """Render as a context block for Rohan_Firmware_Lead's system prompt."""
        lines = [f"## Hardware ground truth (board: {self.board}, source: Greptile)"]
        for c in self.components:
            lines.append(f"- {c.name}" + (f" [{c.bus}]" if c.bus else ""))
            for h in c.headers:
                lines.append(f"    #include <{h}>")
            if c.driver:
                lines.append(f"    driver/component: {c.driver}")
            if c.pins:
                lines.append("    pins: " + ", ".join(f"{k}=GPIO{v}" for k, v in c.pins.items()))
            for w in c.pin_warnings:
                lines.append(f"    WARNING: {w}")
        lines.append(f"Safe general-purpose output GPIOs: {ESP32_SAFE_OUTPUT}")
        if self.sources:
            lines.append("Reference files:")
            for s in self.sources[:10]:
                lines.append(f"  - {s.get('repository')}/{s.get('filepath')}"
                             f" L{s.get('linestart')}-{s.get('lineend')}")
        return "\n".join(lines)


_QUERY_TEMPLATE = """You are helping write ESP32 firmware for a {board} board.
Components: {components}

Using ONLY code present in the indexed repositories, for each component return:
- the exact header file(s) to #include (as they appear in the repo)
- the driver/component name in the repo, if any
- the bus it uses (I2C, SPI, UART, ADC, PWM, GPIO, 1-Wire)
- a pin assignment on {board} (GPIO numbers), avoiding flash pins 6-11,
  strapping pins 0/2/5/12/15, and using 34-39 for inputs only.
  Share I2C SDA/SCL across I2C devices.

Respond with ONLY a JSON object, no prose, in this shape:
{{"components": [{{"name": str, "headers": [str], "driver": str|null,
  "bus": str|null, "pins": {{"<signal>": int}}}}]}}"""


def _extract_json(text: str) -> dict | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_pins(raw: dict) -> dict[str, int]:
    """Accept 21, "21" or "GPIO21"; drop anything else."""
    pins = {}
    for signal, value in raw.items():
        m = re.fullmatch(r"(?:GPIO)?\s*(\d+)", str(value).strip(), re.I)
        if m:
            pins[str(signal)] = int(m.group(1))
    return pins


def check_pin(signal: str, gpio: int) -> str | None:
    """Return a warning if this pin is unsafe on ESP32 for the given signal."""
    if gpio not in ESP32_VALID:
        return f"{signal}=GPIO{gpio} does not exist on ESP32"
    if gpio in ESP32_FLASH_PINS:
        return f"{signal}=GPIO{gpio} is wired to SPI flash; pick from {ESP32_SAFE_OUTPUT}"
    if gpio in ESP32_STRAPPING:
        return f"{signal}=GPIO{gpio} is a strapping pin and can break boot"
    if gpio in ESP32_INPUT_ONLY and not re.search(r"\b(in|input|adc|echo|rx|miso|int|irq|sense|data_in)\b",
                                                   signal.lower().replace("_", " ")):
        return f"{signal}=GPIO{gpio} is input-only; cannot drive outputs"
    return None


def query_hardware_context(
    components: list[str],
    board: str = "ESP32-DevKitC",
    repos: list[dict] | None = None,
    session_id: str | None = None,
) -> HardwareContext:
    """Ask Greptile for headers and pins for each sensor/actuator, then vet the pins."""
    body = {
        "messages": [{
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": _QUERY_TEMPLATE.format(board=board, components=", ".join(components)),
        }],
        "repositories": repos or HARDWARE_REPOS,
        "sessionId": session_id or str(uuid.uuid4()),
        "stream": False,
        "genius": True,
    }
    resp = _request("POST", GREPTILE_QUERY_PATH, body, timeout=180)
    answer = resp.get("message", "")
    parsed = _extract_json(answer) or {"components": []}

    specs = []
    for item in parsed.get("components", []):
        pins = _parse_pins(item.get("pins") or {})
        warnings = [w for s, g in pins.items() if (w := check_pin(s, g))]
        specs.append(ComponentSpec(
            name=item.get("name", "?"),
            headers=item.get("headers") or [],
            driver=item.get("driver"),
            bus=item.get("bus"),
            pins=pins,
            pin_warnings=warnings,
        ))

    # Flag two components claiming the same GPIO (shared I2C lines are fine).
    owners: dict[int, list[str]] = {}
    for c in specs:
        for s, g in c.pins.items():
            if not re.search(r"sda|scl", s, re.I):
                owners.setdefault(g, []).append(f"{c.name}.{s}")
    for g, users in owners.items():
        if len(users) > 1:
            for c in specs:
                if any(u.startswith(c.name + ".") for u in users):
                    c.pin_warnings.append(f"GPIO{g} is assigned to multiple signals: {users}")

    return HardwareContext(board=board, components=specs,
                           sources=resp.get("sources", []), raw_answer=answer)

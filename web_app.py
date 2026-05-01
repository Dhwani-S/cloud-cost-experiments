"""
FinOps Pricing Scout — Web UI with LLM agent loop.

Runs a FastAPI server that:
  1. Serves the web UI at /
  2. Runs the LLM agent on POST /api/run (streams SSE events)
  3. Manages a `prefab serve` subprocess for the dashboard

Run:
  uvicorn web_app:app --port 8080
  Then open http://localhost:8080
"""

import json
import os
import subprocess
import sys
import asyncio
import csv
import io
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from sse_starlette.sse import EventSourceResponse
from google import genai
import httpx

load_dotenv(override=True)

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
DATA_DIR.mkdir(exist_ok=True)
GENERATED_APP = HERE / "generated_app.py"
DASHBOARD_DATA = HERE / "dashboard_data.json"
PREFAB_LOG = HERE / "prefab_server.log"

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
if os.getenv("USE_VERTEX", "").lower() == "true":
    llm = genai.Client(
        vertexai=True,
        project=os.getenv("GCP_PROJECT"),
        location=os.getenv("GCP_LOCATION", "us-central1"),
    )
else:
    llm = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

app = FastAPI()

# ── Prefab subprocess management ─────────────────────────────────────────

_prefab_proc = None


def _find_prefab() -> str:
    """Find the prefab executable in the current venv."""
    venv_prefab = HERE / ".venv" / "Scripts" / "prefab.exe"
    if venv_prefab.exists():
        return str(venv_prefab)
    return "prefab"  # fallback to PATH


def start_prefab():
    global _prefab_proc
    stop_prefab()
    log = open(PREFAB_LOG, "a")
    log.write("\n===== restart =====\n")
    log.flush()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    _prefab_proc = subprocess.Popen(
        [_find_prefab(), "serve", "generated_app.py"],
        cwd=str(HERE),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )


def stop_prefab():
    global _prefab_proc
    if _prefab_proc is not None:
        _prefab_proc.terminate()
        try:
            _prefab_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _prefab_proc.kill()
            _prefab_proc.wait()
        _prefab_proc = None


def restart_prefab():
    stop_prefab()
    start_prefab()


# ── Tool functions (same logic as mcp_server.py) ─────────────────────────

_last_fetch_results = None  # auto-saved by fetch_cloud_pricing
_answer_queues: dict[str, asyncio.Queue] = {}  # for elicitation


def tool_fetch_pricing(service="Virtual Machines", region="eastus", top="10"):
    global _last_fetch_results
    top_n = min(int(top), 20)
    url = "https://prices.azure.com/api/retail/prices"
    params = {
        "$filter": (
            f"serviceName eq '{service}' "
            f"and armRegionName eq '{region}' "
            f"and priceType eq 'Consumption'"
        ),
        "$top": str(top_n),
    }
    resp = httpx.get(url, params=params, timeout=15)
    resp.raise_for_status()
    items = resp.json().get("Items", [])
    results = []
    for item in items:
        results.append({
            "sku": item.get("skuName", ""),
            "meter": item.get("meterName", ""),
            "price": item.get("retailPrice", 0),
            "unit": item.get("unitOfMeasure", ""),
            "currency": item.get("currencyCode", "USD"),
            "region": item.get("armRegionName", ""),
            "service": item.get("serviceName", ""),
        })
    _last_fetch_results = results
    # Auto-save to a default file
    auto_name = f"{service.lower().replace(' ', '_')}_{region}.json"
    auto_path = DATA_DIR / auto_name
    auto_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    # Also pre-write dashboard data so it's always ready
    DASHBOARD_DATA.write_text(
        json.dumps({"title": f"{service} Pricing ({region})", "items": results}),
        encoding="utf-8",
    )
    print(f"[FETCH] Got {len(results)} items, saved to {auto_name}, dashboard_data.json updated")
    # Return compact summary (no huge JSON through pipe protocol)
    preview = [{"sku": r["sku"], "price": r["price"]} for r in results[:5]]
    return json.dumps({
        "count": len(results),
        "saved_as": auto_name,
        "preview": preview,
    }, indent=2)


def tool_save_file(filename):
    """Copy the last fetch results to a named file."""
    if _last_fetch_results is None:
        return "Error: no data to save — fetch pricing first"
    safe = Path(filename).name
    if not safe or safe.startswith("."):
        return "Error: invalid filename"
    path = DATA_DIR / safe
    path.write_text(json.dumps(_last_fetch_results, indent=2), encoding="utf-8")
    return f"Saved {len(_last_fetch_results)} items to data/{safe}"


def tool_read_file(filename):
    safe = Path(filename).name
    path = DATA_DIR / safe
    if not path.exists():
        return f"Error: data/{safe} not found"
    return path.read_text(encoding="utf-8")


def tool_show_dashboard(title, filename):
    # Update title if provided, data is already in dashboard_data.json from fetch
    if DASHBOARD_DATA.exists():
        data = json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
        if data.get("items"):
            data["title"] = title
            DASHBOARD_DATA.write_text(json.dumps(data), encoding="utf-8")
            restart_prefab()
            return f"Dashboard generated with {len(data['items'])} items at http://localhost:5175"
    # Fallback: read from the specified file
    safe = Path(filename).name
    path = DATA_DIR / safe
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        try:
            items = json.loads(raw)
            if isinstance(items, list) and items:
                DASHBOARD_DATA.write_text(
                    json.dumps({"title": title, "items": items}), encoding="utf-8"
                )
                restart_prefab()
                return f"Dashboard generated with {len(items)} items at http://localhost:5175"
        except json.JSONDecodeError:
            pass
    return "Error: no pricing data available — fetch pricing first"


def tool_export_csv(filename):
    """Export a JSON data file to CSV format."""
    safe = Path(filename).name
    json_path = DATA_DIR / safe
    if not json_path.exists():
        return f"Error: data/{safe} not found"
    raw = json_path.read_text(encoding="utf-8")
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        return f"Error: data/{safe} is not valid JSON — {e}"
    if not isinstance(items, list) or not items:
        return "Error: no data to export"
    csv_name = safe.rsplit(".", 1)[0] + ".csv"
    csv_path = DATA_DIR / csv_name
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=items[0].keys())
        writer.writeheader()
        writer.writerows(items)
    return f"Exported {len(items)} rows to data/{csv_name} — download at /api/download/{csv_name}"


TOOLS = {
    "fetch_cloud_pricing": {
        "fn": tool_fetch_pricing,
        "params": ["service", "region", "top"],
    },
    "save_to_file": {
        "fn": tool_save_file,
        "params": ["filename"],
    },
    "read_file": {
        "fn": tool_read_file,
        "params": ["filename"],
    },
    "show_pricing_dashboard": {
        "fn": tool_show_dashboard,
        "params": ["title", "filename"],
    },
    "export_to_csv": {
        "fn": tool_export_csv,
        "params": ["filename"],
    },
}

TOOLS_DESC = """1. fetch_cloud_pricing(service: string, region: string, top: integer) — Fetch real-time cloud pricing from Azure. Results are auto-saved; the response includes the filename.
2. save_to_file(filename: string) — Copy the last fetched results to a named file in data/.
3. read_file(filename: string) — Read content from a local file in the data/ directory.
4. show_pricing_dashboard(title: string, filename: string) — Show an interactive pricing dashboard by reading data from a saved file in data/.
5. export_to_csv(filename: string) — Export a saved JSON pricing file to CSV format for download.
6. ask_user(question: string, options: string) — Ask the user a clarifying question. 'options' is an optional comma-separated list of choices. Use this FIRST when the user's request is vague or missing service/region details."""

SYSTEM_PROMPT = f"""You are a FinOps pricing agent. You help users fetch cloud pricing,
save reports, and show dashboards.

Available tools:
{TOOLS_DESC}

Respond with EXACTLY ONE line in one of these formats:
  FUNCTION_CALL: tool_name|arg1|arg2|...
  FINAL_ANSWER: <summary of what you did>

Rules:
- Provide arguments in the order they appear in the tool signature.
- After each FUNCTION_CALL you receive the result — use it for the next step.
- When all tasks are complete, emit FINAL_ANSWER.
- For show_pricing_dashboard, pass the FILENAME from the fetch/save result (e.g. vm_pricing.json).
- For save_to_file, just pass a filename — the last fetched data is saved automatically.
- Do NOT pass large JSON content as arguments — use filenames instead.
- If the user's request is vague or missing service/region, use ask_user FIRST to clarify.
- ask_user returns the user's response. Use their answer in subsequent tool calls.
- For ask_user options, pass a comma-separated list: ask_user|Which service?|Virtual Machines,Storage,SQL Database
- Execute ALL requested steps. Do not stop early.
"""


async def call_llm(prompt: str) -> str:
    loop = asyncio.get_event_loop()
    last_err = None
    for attempt in range(3):
        try:
            resp = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: llm.models.generate_content(model=MODEL, contents=prompt),
                ),
                timeout=60,
            )
            text = (resp.text or "").strip()
            lines = text.splitlines()
            if not lines:
                return "FINAL_ANSWER: LLM returned empty response"
            return lines[0].strip()
        except Exception as e:
            last_err = e
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s
                continue
            raise
    raise last_err


# ── Routes ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (HERE / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/api/download/{filename}")
async def download_file(filename: str):
    safe = Path(filename).name
    path = DATA_DIR / safe
    if not path.exists():
        return HTMLResponse("File not found", status_code=404)
    media = "text/csv" if safe.endswith(".csv") else "application/json"
    return FileResponse(path, filename=safe, media_type=media)


@app.post("/api/answer")
async def answer_question(request: Request):
    body = await request.json()
    sid = body.get("session_id", "")
    answer = body.get("answer", "")
    q = _answer_queues.get(sid)
    if q:
        await q.put(answer)
    return {"ok": True}


@app.post("/api/run")
async def run_agent(request: Request):
    body = await request.json()
    user_prompt = body.get("prompt", "")

    async def stream():
        session_id = str(uuid.uuid4())
        _answer_queues[session_id] = asyncio.Queue()
        history = []

        for i in range(1, 16):
            ctx = "\n".join(history) if history else "(none)"
            prompt = (
                f"{SYSTEM_PROMPT}\n"
                f"Task: {user_prompt}\n\n"
                f"Previous steps:\n{ctx}\n\n"
                f"What is your next single action?"
            )

            yield {"data": json.dumps({"type": "thinking", "text": f"Iteration {i} — calling LLM..."})}

            try:
                text = await call_llm(prompt)
            except Exception as e:
                yield {"data": json.dumps({"type": "error", "text": str(e)})}
                return

            if text.startswith("FINAL_ANSWER:"):
                answer = text.split(":", 1)[1].strip()
                yield {"data": json.dumps({"type": "final", "text": answer})}
                _answer_queues.pop(session_id, None)
                return

            # Robust parsing: handle FUNCTION_CALL:, ASK_USER:, or direct tool_name| formats
            call_text = None
            if text.startswith("FUNCTION_CALL:"):
                call_text = text.split(":", 1)[1].strip()
            elif text.upper().startswith("ASK_USER:"):
                # Handle "ASK_USER: question text" shorthand
                q_text = text.split(":", 1)[1].strip()
                call_text = f"ask_user|{q_text}"
            else:
                for tname in list(TOOLS.keys()) + ["ask_user"]:
                    if tname in text and "|" in text:
                        idx = text.index(tname)
                        call_text = text[idx:].strip()
                        break

            if call_text is None:
                yield {"data": json.dumps({"type": "thinking", "text": f"LLM: {text}"})}
                history.append(f"Step {i}: unexpected format \u2014 {text}")
                continue

            parts = [p.strip() for p in call_text.split("|")]
            func_name = parts[0]
            raw_args = parts[1:]

            # Strip key=value prefixes (e.g. "service=Virtual Machines" -> "Virtual Machines")
            raw_args = [a.split("=", 1)[1] if "=" in a and a.split("=", 1)[0].replace("_", "").isalpha() else a for a in raw_args]

            # \u2500\u2500 Elicitation: ask_user \u2500\u2500
            if func_name == "ask_user":
                question = raw_args[0] if raw_args else "What would you like?"
                options = [o.strip() for o in raw_args[1].split(",") if o.strip()] if len(raw_args) > 1 and raw_args[1] else []
                yield {"data": json.dumps({"type": "question", "text": question, "options": options, "session_id": session_id})}
                try:
                    answer = await asyncio.wait_for(_answer_queues[session_id].get(), timeout=120)
                except asyncio.TimeoutError:
                    answer = "(no response)"
                yield {"data": json.dumps({"type": "tool_result", "result": f"User answered: {answer}"})}
                history.append(f"Step {i}: ask_user({question}) -> User: {answer}")
                continue

            tool = TOOLS.get(func_name)
            if not tool:
                yield {"data": json.dumps({"type": "error", "text": f"Unknown tool: {func_name}"})}
                history.append(f"Step {i}: unknown tool {func_name}")
                continue

            # If more args than expected, join extras into the last arg
            expected = len(tool["params"])
            if len(raw_args) > expected:
                raw_args = raw_args[:expected - 1] + ["|".join(raw_args[expected - 1:])]

            args_preview = ", ".join(
                f"{p}={a[:60]}{'...' if len(a) > 60 else ''}"
                for p, a in zip(tool["params"], raw_args)
            )
            yield {"data": json.dumps({"type": "tool_call", "tool": func_name, "args": args_preview})}

            try:
                result = tool["fn"](*raw_args)
            except Exception as e:
                result = f"ERROR: {e}"

            is_dashboard = func_name == "show_pricing_dashboard"
            preview = result[:300] + "..." if len(result) > 300 else result
            yield {"data": json.dumps({"type": "tool_result", "result": preview})}

            if is_dashboard:
                yield {"data": json.dumps({"type": "dashboard_ready"})}

            history.append(f"Step {i}: {func_name} -> {preview}")

        yield {"data": json.dumps({"type": "final", "text": "Reached max iterations."})}
        _answer_queues.pop(session_id, None)

    return EventSourceResponse(stream())


# ── Startup / Shutdown ───────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    if not DASHBOARD_DATA.exists():
        DASHBOARD_DATA.write_text(
            json.dumps({"title": "FinOps Pricing Scout", "items": []}),
            encoding="utf-8",
        )
    start_prefab()


@app.on_event("shutdown")
async def on_shutdown():
    stop_prefab()

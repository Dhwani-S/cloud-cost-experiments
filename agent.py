"""
FinOps Pricing Scout — Agent that chains all 3 MCP tools.

Connects to mcp_server.py, calls:
  1. fetch_cloud_pricing    (internet)
  2. save_to_file           (file CRUD)
  3. show_pricing_dashboard (Prefab UI)

Run:
  python agent.py
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from google import genai
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import mcp.types as types

load_dotenv(override=True)

# ── Logging to file ──
LOG_FILE = open("agent_logs.txt", "w", encoding="utf-8")

def log(msg=""):
    print(msg)
    LOG_FILE.write(msg + "\n")
    LOG_FILE.flush()


async def handle_elicitation(context, params):
    """Called when the MCP server asks the user for input (elicitation)."""
    log(f"\n  [ELICITATION] Server asks: {params.message}")
    schema = params.requestedSchema or {}
    props = schema.get("properties", {})
    response = {}

    if props:
        for name, info in props.items():
            field_type = info.get("type", "string")
            enum = info.get("enum", [])
            if enum:
                log(f"    Options: {enum}")
            answer = input(f"    {name}: ").strip()
            if field_type == "boolean":
                response[name] = answer.lower() in ("yes", "y", "true", "1")
            else:
                response[name] = answer
    else:
        # Simple elicitation with choices passed as requestedSchema
        answer = input(f"    Your choice: ").strip()
        response["value"] = answer

    log(f"  [ELICITATION] User answered: {response}")
    return types.ElicitResult(action="accept", content=response)
    LOG_FILE.flush()

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
MAX_ITERATIONS = 20
LLM_TIMEOUT = 60

if os.getenv("USE_VERTEX", "").lower() == "true":
    client = genai.Client(
        vertexai=True,
        project=os.getenv("GCP_PROJECT"),
        location=os.getenv("GCP_LOCATION", "us-central1"),
    )
else:
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


async def generate(prompt: str):
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: client.models.generate_content(model=MODEL, contents=prompt),
        ),
        timeout=LLM_TIMEOUT,
    )


def describe_tools(tools) -> str:
    lines = []
    for i, t in enumerate(tools, 1):
        props = (t.inputSchema or {}).get("properties", {})
        params = ", ".join(f"{n}: {p.get('type', '?')}" for n, p in props.items())
        lines.append(f"{i}. {t.name}({params}) — {t.description or ''}")
    return "\n".join(lines)


def coerce(value: str, schema_type: str):
    if schema_type == "integer":
        return int(value)
    if schema_type == "number":
        return float(value)
    if schema_type == "boolean":
        return value.lower() in ("true", "1", "yes")
    return value


async def main():
    import sys
    venv_python = sys.executable  # use the same Python running this script
    server = StdioServerParameters(command=venv_python, args=["mcp_server.py"])

    async with stdio_client(server) as (read, write):
        async with ClientSession(
            read, write,
            elicitation_callback=handle_elicitation
        ) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            tools_desc = describe_tools(tools)
            log(f"Connected — {len(tools)} tools loaded\n")

            system = f"""You are a FinOps pricing agent. You help users fetch cloud pricing,
save reports, and show dashboards.

Available tools:
{tools_desc}

Respond with EXACTLY ONE line in one of these formats:
  FUNCTION_CALL: tool_name|arg1|arg2|...
  FINAL_ANSWER: <summary of what you did>

Rules:
- Provide arguments in the order they appear in the tool signature.
- After each FUNCTION_CALL you receive the result — use it for the next step.
- When all tasks are complete, emit FINAL_ANSWER.
- For show_pricing_dashboard, pass the FULL pricing JSON as the second argument.
- Do NOT skip any of the three steps: fetch, save, then show dashboard.
"""

            try:
                task = input("\nEnter your task (or press Enter for default): ").strip()
            except EOFError:
                task = ""
            if not task:
                task = (
                    "Fetch 'Virtual Machines' pricing for the eastus region (top 10). "
                    "Save the pricing data to a file called azure_vm_pricing.json. "
                    "Read the file back to verify it was saved correctly. "
                    "Export it to CSV format. "
                    "Then show me an interactive pricing dashboard with the title "
                    "'Azure VM Pricing - East US'."
                )

            log(f"Task: {task}\n")
            history: list[str] = []
            last_fetch_result: str = ""  # cache last fetch for save_to_file
            recent_calls: set[str] = set()  # detect duplicate tool calls

            for i in range(1, MAX_ITERATIONS + 1):
                log(f"\n--- Iteration {i} ---")
                ctx = "\n".join(history) if history else "(none)"
                prompt = (
                    f"{system}\n"
                    f"Task: {task}\n\n"
                    f"Previous steps:\n{ctx}\n\n"
                    f"What is your next single action?"
                )

                try:
                    resp = await generate(prompt)
                except Exception as e:
                    log(f"LLM error: {e}")
                    break

                text = (resp.text or "").strip().splitlines()[0].strip()
                log(f"LLM: {text}")

                if text.startswith("FINAL_ANSWER:"):
                    log(f"\n=== Done === {text}")
                    break

                # Normalize alternate formats the LLM produces
                # e.g. "EXPORT_TO_CSV: file.json" or "read_file|file.json"
                TOOL_NAMES = {t.name for t in tools}
                if not text.startswith("FUNCTION_CALL:"):
                    # Check for TOOL_NAME: args or tool_name|args patterns
                    normalized = False
                    for tname in TOOL_NAMES:
                        upper = tname.upper()
                        if text.upper().startswith(upper + ":"):
                            # e.g. "EXPORT_TO_CSV: file.json" → "FUNCTION_CALL: export_to_csv|file.json"
                            rest = text.split(":", 1)[1].strip()
                            # rest might already have pipes, or be space-separated args
                            if "|" in rest:
                                # strip tool name if duplicated: "export_to_csv|file" → "file"
                                parts_check = rest.split("|")
                                if parts_check[0].strip().lower() == tname:
                                    rest = "|".join(parts_check[1:])
                            text = f"FUNCTION_CALL: {tname}|{rest}"
                            normalized = True
                            break
                        elif text.startswith(tname + "|"):
                            # e.g. "read_file|azure_vm_pricing.json"
                            rest = text.split("|", 1)[1]
                            text = f"FUNCTION_CALL: {tname}|{rest}"
                            normalized = True
                            break
                    if not normalized:
                        log("Unexpected format — retrying")
                        history.append(f"Step {i}: unexpected response, skipped")
                        continue
                    log(f"  (normalized to: {text})")

                _, call = text.split(":", 1)
                parts = [p.strip() for p in call.split("|")]
                func_name, raw_args = parts[0], parts[1:]

                tool = next((t for t in tools if t.name == func_name), None)
                if not tool:
                    msg = f"Unknown tool: {func_name}"
                    log(msg)
                    history.append(f"Step {i}: {msg}")
                    continue

                props = (tool.inputSchema or {}).get("properties", {})
                arguments = {}
                for (name, info), val in zip(props.items(), raw_args):
                    arguments[name] = coerce(val, info.get("type", "string"))

                # Skip duplicate tool calls
                call_key = f"{func_name}|{json.dumps(arguments, sort_keys=True)}"
                if call_key in recent_calls:
                    log(f"  (skipping duplicate call to {func_name})")
                    history.append(f"Step {i}: duplicate {func_name} skipped — already done")
                    continue
                recent_calls.add(call_key)

                # If save_to_file content looks truncated, use cached fetch result
                if func_name == "save_to_file" and "content" in arguments:
                    content = arguments["content"]
                    if ("..." in content or content.count("}") < 2) and last_fetch_result:
                        log("  (substituting cached fetch data for truncated content)")
                        arguments["content"] = last_fetch_result

                # If show_pricing_dashboard, use cached data for pricing_json
                if func_name == "show_pricing_dashboard" and "pricing_json" in arguments:
                    pj = arguments["pricing_json"]
                    if ("..." in pj or pj.count("}") < 2) and last_fetch_result:
                        log("  (substituting cached fetch data for truncated pricing_json)")
                        arguments["pricing_json"] = last_fetch_result

                log(f"  -> {func_name}({json.dumps(arguments)[:200]})")
                try:
                    result = await session.call_tool(func_name, arguments=arguments)
                    payload = (
                        result.content[0].text
                        if result.content and hasattr(result.content[0], "text")
                        else str(result)
                    )
                except Exception as e:
                    payload = f"ERROR: {e}"

                # Cache fetch results for later tools
                if func_name == "fetch_cloud_pricing" and not payload.startswith("ERROR") and payload.strip() != "[]":
                    last_fetch_result = payload

                preview = payload[:300] + "..." if len(payload) > 300 else payload
                log(f"  <- {preview}")

                # Build a clear summary for LLM history so it doesn't retry
                if func_name == "fetch_cloud_pricing" and not payload.startswith("ERROR"):
                    try:
                        items = json.loads(payload)
                        if len(items) == 0:
                            summary = "Returned 0 items — the service name may be wrong. Try 'Virtual Machines', 'Storage', or 'SQL Database'."
                        else:
                            summary = f"Successfully fetched {len(items)} pricing items. Data is cached — next call save_to_file with just the filename, content will be auto-filled."
                    except json.JSONDecodeError:
                        summary = preview
                    history.append(f"Step {i}: {func_name}({list(arguments.keys())}) -> {summary}")
                else:
                    history.append(
                        f"Step {i}: {func_name}({list(arguments.keys())}) -> {preview}"
                    )

                # After save_to_file, clear duplicate tracking for read/export/dashboard
                # since the underlying data has changed
                if func_name == "save_to_file":
                    recent_calls = {c for c in recent_calls if not any(
                        c.startswith(t) for t in ("read_file|", "export_to_csv|", "show_pricing_dashboard|")
                    )}

            log("\nCheck data/ folder for the saved file.")
            log("Run 'fastmcp dev apps mcp_server.py' to preview the Prefab dashboard.")
    LOG_FILE.close()


if __name__ == "__main__":
    asyncio.run(main())

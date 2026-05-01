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

from dotenv import load_dotenv
from google import genai
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
MAX_ITERATIONS = 10
LLM_TIMEOUT = 30

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
    server = StdioServerParameters(command="python", args=["mcp_server.py"])

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            tools_desc = describe_tools(tools)
            print(f"Connected — {len(tools)} tools loaded\n")

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

            task = (
                "Fetch Azure Virtual Machines pricing for the eastus region (top 10). "
                "Save the pricing data to a file called azure_vm_pricing.txt. "
                "Then show me an interactive pricing dashboard with the title "
                "'Azure VM Pricing - East US'."
            )

            print(f"Task: {task}\n")
            history: list[str] = []

            for i in range(1, MAX_ITERATIONS + 1):
                print(f"\n--- Iteration {i} ---")
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
                    print(f"LLM error: {e}")
                    break

                text = (resp.text or "").strip().splitlines()[0].strip()
                print(f"LLM: {text}")

                if text.startswith("FINAL_ANSWER:"):
                    print(f"\n=== Done === {text}")
                    break

                if not text.startswith("FUNCTION_CALL:"):
                    print("Unexpected format — retrying")
                    history.append(f"Step {i}: unexpected response, skipped")
                    continue

                _, call = text.split(":", 1)
                parts = [p.strip() for p in call.split("|")]
                func_name, raw_args = parts[0], parts[1:]

                tool = next((t for t in tools if t.name == func_name), None)
                if not tool:
                    msg = f"Unknown tool: {func_name}"
                    print(msg)
                    history.append(f"Step {i}: {msg}")
                    continue

                props = (tool.inputSchema or {}).get("properties", {})
                arguments = {}
                for (name, info), val in zip(props.items(), raw_args):
                    arguments[name] = coerce(val, info.get("type", "string"))

                print(f"  -> {func_name}({json.dumps(arguments)[:200]})")
                try:
                    result = await session.call_tool(func_name, arguments=arguments)
                    payload = (
                        result.content[0].text
                        if result.content and hasattr(result.content[0], "text")
                        else str(result)
                    )
                except Exception as e:
                    payload = f"ERROR: {e}"

                preview = payload[:300] + "..." if len(payload) > 300 else payload
                print(f"  <- {preview}")
                history.append(
                    f"Step {i}: {func_name}({list(arguments.keys())}) -> {preview}"
                )

            print("\nCheck data/ folder for the saved file.")
            print("Run 'fastmcp dev apps mcp_server.py' to preview the Prefab dashboard.")


if __name__ == "__main__":
    asyncio.run(main())

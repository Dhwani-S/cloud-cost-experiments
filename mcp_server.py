"""
FinOps Pricing Scout — MCP server showcasing all MCP protocol features.

MCP Features demonstrated:
  - Tools:         fetch_cloud_pricing, save_to_file, read_file, export_to_csv, show_pricing_dashboard
  - Resources:     pricing://services, pricing://{region}/summary
  - Prompts:       cost_analysis, compare_regions
  - Elicitations:  smart_pricing_report (asks user for preferences)
  - Completions:   auto-complete for region and service arguments

Run:
  # Preview Prefab UIs in browser:
  fastmcp dev apps mcp_server.py

  # Run as MCP server (for agent.py):
  python mcp_server.py
"""

import csv
import io
import json
from pathlib import Path

import httpx
from fastmcp import FastMCP, Context
from fastmcp.server.elicitation import AcceptedElicitation
from prefab_ui.app import PrefabApp
from prefab_ui.components import (
    Card, CardContent, CardHeader, CardTitle,
    Column, H1, H3, Muted, Row, Tab, Tabs, Text,
)
from prefab_ui.components.charts import BarChart, ChartSeries

mcp = FastMCP("FinOpsPricingScout")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── Constants ─────────────────────────────────────────────────────────────

AZURE_REGIONS = [
    "eastus", "eastus2", "westus", "westus2", "westus3",
    "centralus", "northcentralus", "southcentralus",
    "westeurope", "northeurope", "uksouth", "ukwest",
    "eastasia", "southeastasia", "japaneast", "japanwest",
    "australiaeast", "australiasoutheast",
    "canadacentral", "canadaeast",
    "brazilsouth", "koreacentral", "koreasouth",
    "centralindia", "southindia", "westindia",
]

AZURE_SERVICES = [
    "Virtual Machines", "Storage", "SQL Database",
    "Azure Cosmos DB", "Azure App Service", "Azure Functions",
    "Azure Kubernetes Service", "Azure Cache for Redis",
    "Azure Cognitive Services", "Azure Monitor",
]


# ── Tool 1: Internet ─────────────────────────────────────────────────────

@mcp.tool()
def fetch_cloud_pricing(
    service: str = "Virtual Machines",
    region: str = "eastus",
    top: int = 10,
) -> str:
    """Fetch real-time cloud pricing from the Azure Retail Prices API.

    Args:
        service: Azure service name (e.g. 'Virtual Machines', 'Storage', 'SQL Database')
        region: Azure region (e.g. 'eastus', 'westus2', 'westeurope')
        top: Number of pricing items to return (max 20)
    """
    top = min(top, 20)
    url = "https://prices.azure.com/api/retail/prices"
    params = {
        "$filter": (
            f"serviceName eq '{service}' "
            f"and armRegionName eq '{region}' "
            f"and priceType eq 'Consumption'"
        ),
        "$top": str(top),
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

    return json.dumps(results, indent=2)


# ── Tool 2: File CRUD ────────────────────────────────────────────────────

@mcp.tool()
def save_to_file(filename: str, content: str) -> str:
    """Save content to a local file in the data/ directory.

    Args:
        filename: Name of the file (e.g. 'azure_pricing.txt'). No paths allowed.
        content: The text content to write to the file.
    """
    safe_name = Path(filename).name  # strip any path components
    if not safe_name or safe_name.startswith("."):
        return "Error: invalid filename"

    path = DATA_DIR / safe_name
    path.write_text(content, encoding="utf-8")
    return f"Saved {len(content)} chars to data/{safe_name}"


@mcp.tool()
def read_file(filename: str) -> str:
    """Read content from a local file in the data/ directory.

    Args:
        filename: Name of the file to read (e.g. 'azure_pricing.txt').
    """
    safe_name = Path(filename).name
    path = DATA_DIR / safe_name
    if not path.exists():
        return f"Error: data/{safe_name} not found"
    return path.read_text(encoding="utf-8")


@mcp.tool()
def export_to_csv(filename: str) -> str:
    """Export a saved JSON pricing file to CSV format.

    Args:
        filename: Name of the JSON file to export (e.g. 'vm_pricing.json').
    """
    safe = Path(filename).name
    json_path = DATA_DIR / safe
    if not json_path.exists():
        return f"Error: data/{safe} not found"
    items = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(items, list) or not items:
        return "Error: no data to export"
    csv_name = safe.rsplit(".", 1)[0] + ".csv"
    csv_path = DATA_DIR / csv_name
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=items[0].keys())
        writer.writeheader()
        writer.writerows(items)
    return f"Exported {len(items)} rows to data/{csv_name}"


# ── Tool 3: Prefab UI ────────────────────────────────────────────────────

@mcp.tool(app=True)
def show_pricing_dashboard(title: str, pricing_json: str) -> PrefabApp:
    """Show an interactive pricing dashboard in the browser.

    Args:
        title: Dashboard title (e.g. 'Azure VM Pricing - East US')
        pricing_json: JSON array of pricing items from fetch_cloud_pricing
    """
    try:
        items = json.loads(pricing_json)
    except json.JSONDecodeError:
        items = []

    # ── compute stats ──
    prices = [it["price"] for it in items if it.get("price", 0) > 0]
    total_items = len(items)
    avg_price = sum(prices) / len(prices) if prices else 0
    cheapest = min(items, key=lambda x: x.get("price", 999)) if items else {}
    most_expensive = max(items, key=lambda x: x.get("price", 0)) if items else {}

    # ── chart data (top items by price, descending) ──
    chart_data = sorted(
        [{"sku": it["sku"], "price": round(it["price"], 4)}
         for it in items if it.get("price", 0) > 0],
        key=lambda x: x["price"],
        reverse=True,
    )[:10]

    with PrefabApp(css_class="max-w-5xl mx-auto p-6") as app:
        with Card():
            with CardHeader():
                CardTitle(title)
            with CardContent():
                with Tabs(value="overview"):

                    # ── Tab 1: Overview ──
                    with Tab("Overview", value="overview"):
                        with Column(gap=5):
                            with Row(gap=4):
                                with Column(gap=1):
                                    Muted("Total SKUs")
                                    H1(str(total_items))
                                with Column(gap=1):
                                    Muted("Avg Price/hr")
                                    H1(f"${avg_price:.4f}")
                                with Column(gap=1):
                                    Muted("Cheapest")
                                    H1(f"${cheapest.get('price', 0):.4f}")
                                    Muted(cheapest.get("sku", ""))
                                with Column(gap=1):
                                    Muted("Most Expensive")
                                    H1(f"${most_expensive.get('price', 0):.4f}")
                                    Muted(most_expensive.get("sku", ""))

                            H3("Price by SKU")
                            BarChart(
                                data=chart_data,
                                series=[ChartSeries(data_key="price", label="$/hr")],
                                x_axis="sku",
                                show_legend=False,
                            )

                    # ── Tab 2: Data Table ──
                    with Tab("Details", value="details"):
                        with Column(gap=3):
                            H3("All Pricing Items")
                            with Row(gap=3):
                                Text("SKU")
                                Text("Meter")
                                Text("Price")
                                Text("Unit")
                            for it in items:
                                with Row(gap=3):
                                    Text(it.get("sku", ""))
                                    Text(it.get("meter", ""))
                                    Text(f"${it.get('price', 0):.4f}")
                                    Text(it.get("unit", ""))
    return app


# ══════════════════════════════════════════════════════════════════════════
# MCP RESOURCES — expose data the client can read directly
# ══════════════════════════════════════════════════════════════════════════

@mcp.resource("pricing://services")
def list_services() -> str:
    """List all supported Azure service names for pricing queries."""
    return json.dumps(AZURE_SERVICES, indent=2)


@mcp.resource("pricing://regions")
def list_regions() -> str:
    """List all supported Azure region codes."""
    return json.dumps(AZURE_REGIONS, indent=2)


@mcp.resource("pricing://{region}/summary")
def region_summary(region: str) -> str:
    """Get a summary of all saved pricing data for a specific region."""
    summaries = []
    for f in DATA_DIR.glob("*.json"):
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(items, list):
                region_items = [i for i in items if i.get("region") == region]
                if region_items:
                    prices = [i["price"] for i in region_items if i.get("price", 0) > 0]
                    summaries.append({
                        "file": f.name,
                        "count": len(region_items),
                        "avg_price": round(sum(prices) / len(prices), 4) if prices else 0,
                        "min_price": round(min(prices), 4) if prices else 0,
                        "max_price": round(max(prices), 4) if prices else 0,
                    })
        except (json.JSONDecodeError, KeyError):
            continue
    if not summaries:
        return json.dumps({"region": region, "message": "No pricing data saved for this region yet."})
    return json.dumps({"region": region, "files": summaries}, indent=2)


@mcp.resource("pricing://saved-files")
def list_saved_files() -> str:
    """List all saved data files with their sizes."""
    files = []
    for f in sorted(DATA_DIR.iterdir()):
        if f.is_file():
            files.append({"name": f.name, "size_bytes": f.stat().st_size})
    return json.dumps(files, indent=2)


# ══════════════════════════════════════════════════════════════════════════
# MCP PROMPTS — server-defined prompt templates for the LLM
# ══════════════════════════════════════════════════════════════════════════

@mcp.prompt()
def cost_analysis(service: str, region: str) -> str:
    """Generate a prompt to analyze cloud costs for a specific service and region."""
    return (
        f"You are a FinOps analyst. Fetch the current Azure pricing for "
        f"'{service}' in the '{region}' region. Then:\n"
        f"1. Save the data to a file named '{service.lower().replace(' ', '_')}_{region}.json'\n"
        f"2. Identify the top 5 cheapest and top 5 most expensive SKUs\n"
        f"3. Export the results as CSV\n"
        f"4. Show a pricing dashboard\n"
        f"5. Provide a brief cost optimization summary"
    )


@mcp.prompt()
def compare_regions(service: str, region1: str, region2: str) -> str:
    """Generate a prompt to compare pricing across two Azure regions."""
    return (
        f"Compare Azure '{service}' pricing between '{region1}' and '{region2}':\n"
        f"1. Fetch pricing for '{region1}' and save to '{service.lower().replace(' ', '_')}_{region1}.json'\n"
        f"2. Fetch pricing for '{region2}' and save to '{service.lower().replace(' ', '_')}_{region2}.json'\n"
        f"3. Show a dashboard comparing the two regions\n"
        f"4. Export both as CSV files\n"
        f"5. Recommend the cheaper region with reasoning"
    )


@mcp.prompt()
def quick_report(service: str) -> str:
    """Generate a quick pricing report prompt for a service across eastus."""
    return (
        f"Fetch Azure '{service}' pricing for eastus, save the data, "
        f"export it as CSV, and show a dashboard. Give me a one-paragraph summary."
    )


# ══════════════════════════════════════════════════════════════════════════
# MCP ELICITATIONS — ask the user for input during tool execution
# ══════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def smart_pricing_report(ctx: Context) -> str:
    """Interactively build a pricing report by asking the user for preferences.

    This tool demonstrates MCP elicitations — it asks the user questions
    to determine what pricing data to fetch and how to format the report.
    """
    # Ask which service
    svc_result = await ctx.elicit(
        "Which Azure service do you want to analyze?",
        AZURE_SERVICES,
    )
    if not isinstance(svc_result, AcceptedElicitation):
        return "Report cancelled by user."
    service = svc_result.data

    # Ask which region
    region_result = await ctx.elicit(
        f"Which region for {service} pricing?",
        ["eastus", "westus2", "westeurope", "southeastasia", "centralindia"],
    )
    if not isinstance(region_result, AcceptedElicitation):
        return "Report cancelled by user."
    region = region_result.data

    # Ask how many results
    count_result = await ctx.elicit(
        "How many pricing items to fetch?",
        ["5", "10", "15", "20"],
    )
    if not isinstance(count_result, AcceptedElicitation):
        return "Report cancelled by user."
    top = int(count_result.data)

    # Ask for export format
    format_result = await ctx.elicit(
        "Export format?",
        ["JSON only", "CSV only", "Both JSON and CSV"],
    )
    if not isinstance(format_result, AcceptedElicitation):
        return "Report cancelled by user."
    export_format = format_result.data

    # Now execute the report
    await ctx.info(f"Fetching {service} pricing for {region} (top {top})...")
    pricing_json = fetch_cloud_pricing(service, region, top)
    items = json.loads(pricing_json)

    filename = f"{service.lower().replace(' ', '_')}_{region}.json"
    save_to_file(filename, pricing_json)
    report = f"Fetched {len(items)} items for {service} in {region}.\nSaved to data/{filename}."

    if export_format in ("CSV only", "Both JSON and CSV"):
        csv_result = export_to_csv(filename)
        report += f"\n{csv_result}"

    if export_format in ("JSON only", "Both JSON and CSV"):
        report += f"\nJSON data available at data/{filename}"

    await ctx.info("Report complete!")
    return report


# ══════════════════════════════════════════════════════════════════════════
# MCP COMPLETIONS — auto-complete for resource template arguments
# ══════════════════════════════════════════════════════════════════════════

# FastMCP handles completions for resource templates automatically,
# but we can register a low-level handler for richer completions.

_low_level = mcp._mcp_server  # access the underlying MCP SDK server

@_low_level.completion()
async def handle_completion(ref, argument, context=None):
    """Provide auto-complete suggestions for regions and services."""
    import mcp.types as types

    if argument.name in ("region", "region1", "region2"):
        matches = [r for r in AZURE_REGIONS if r.startswith(argument.value.lower())]
        return types.Completion(values=matches[:10])

    if argument.name == "service":
        matches = [s for s in AZURE_SERVICES if s.lower().startswith(argument.value.lower())]
        return types.Completion(values=matches[:10])

    return None


if __name__ == "__main__":
    mcp.run()

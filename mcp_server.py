"""
FinOps Pricing Scout — MCP server with 3 tools for the assignment.

Tools:
  1. fetch_cloud_pricing    — fetches real Azure VM pricing from the internet
  2. save_to_file / read_file — saves/reads data to a local file (CRUD)
  3. show_pricing_dashboard — renders a Prefab UI dashboard (app=True)

Run:
  # Preview Prefab UIs in browser:
  fastmcp dev apps mcp_server.py

  # Run as MCP server (for agent.py):
  python mcp_server.py
"""

import json
from pathlib import Path

import httpx
from fastmcp import FastMCP
from prefab_ui.app import PrefabApp
from prefab_ui.components import (
    Card, CardContent, CardHeader, CardTitle,
    Column, H1, H3, Muted, Row, Tab, Tabs, Text,
)
from prefab_ui.components.charts import BarChart, ChartSeries

mcp = FastMCP("FinOpsPricingScout")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


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


if __name__ == "__main__":
    mcp.run()

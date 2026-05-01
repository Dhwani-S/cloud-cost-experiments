"""
Prefab dashboard template — reads pricing data from dashboard_data.json.

This file is served by `prefab serve generated_app.py`.
The web_app.py agent writes dashboard_data.json, then restarts
this process so the browser picks up the new data.
"""

import json
from pathlib import Path

from prefab_ui.app import PrefabApp
from prefab_ui.components import (
    Card, CardContent, CardHeader, CardTitle,
    Column, H1, H3, Muted, Row, Tab, Tabs, Text,
)
from prefab_ui.components.charts import BarChart, ChartSeries

# ── Load data ────────────────────────────────────────────────────────────

_data_file = Path(__file__).parent / "dashboard_data.json"

if _data_file.exists():
    _raw = json.loads(_data_file.read_text(encoding="utf-8"))
    TITLE = _raw.get("title", "Cloud Pricing Dashboard")
    ITEMS = _raw.get("items", [])
else:
    TITLE = "FinOps Pricing Scout"
    ITEMS = []

# ── Compute stats ────────────────────────────────────────────────────────

_prices = [it["price"] for it in ITEMS if it.get("price", 0) > 0]
TOTAL = len(ITEMS)
AVG = sum(_prices) / len(_prices) if _prices else 0
CHEAPEST = min(ITEMS, key=lambda x: x.get("price", 999)) if ITEMS else {}
EXPENSIVE = max(ITEMS, key=lambda x: x.get("price", 0)) if ITEMS else {}
CHART_DATA = sorted(
    [{"sku": it["sku"], "price": round(it["price"], 4)}
     for it in ITEMS if it.get("price", 0) > 0],
    key=lambda x: x["price"],
    reverse=True,
)[:10]

# ── Build UI ─────────────────────────────────────────────────────────────

with PrefabApp(css_class="max-w-5xl mx-auto p-6") as app:
    with Card():
        with CardHeader():
            CardTitle(TITLE)
        with CardContent():
            if not ITEMS:
                Muted("Waiting for agent to fetch pricing data...")
            else:
                with Tabs(value="overview"):
                    with Tab("Overview", value="overview"):
                        with Column(gap=5):
                            with Row(gap=4):
                                with Column(gap=1):
                                    Muted("Total SKUs")
                                    H1(str(TOTAL))
                                with Column(gap=1):
                                    Muted("Avg Price/hr")
                                    H1(f"${AVG:.4f}")
                                with Column(gap=1):
                                    Muted("Cheapest")
                                    H1(f"${CHEAPEST.get('price', 0):.4f}")
                                    Muted(CHEAPEST.get("sku", ""))
                                with Column(gap=1):
                                    Muted("Most Expensive")
                                    H1(f"${EXPENSIVE.get('price', 0):.4f}")
                                    Muted(EXPENSIVE.get("sku", ""))

                            H3("Price by SKU")
                            BarChart(
                                data=CHART_DATA,
                                series=[ChartSeries(data_key="price", label="$/hr")],
                                x_axis="sku",
                                show_legend=False,
                            )

                    with Tab("Details", value="details"):
                        with Column(gap=3):
                            H3("All Pricing Items")
                            with Row(gap=3):
                                Text("SKU")
                                Text("Meter")
                                Text("Price")
                                Text("Unit")
                            for it in ITEMS:
                                with Row(gap=3):
                                    Text(it.get("sku", ""))
                                    Text(it.get("meter", ""))
                                    Text(f"${it.get('price', 0):.4f}")
                                    Text(it.get("unit", ""))

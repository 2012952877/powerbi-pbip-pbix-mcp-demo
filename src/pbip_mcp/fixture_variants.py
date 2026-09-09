"""Additional exact-content synthetic fixture; never an upload-controlled refresh policy."""

from .synthetic import SCHEMA, _json, fixture_files

RICH_POINTER = "\u9500\u552e \u5206\u6790 \u03a9.pbip"
RICH_PAGES = ("\u6982\u89c8 \u603b\u89c8 \u03a9", "\u660e\u7ec6 \u5206\u6790")
WEIGHTED_MEASURE = "\u52a0\u6743 \u91d1\u989d"
PRODUCT_TABLE = "\u4ea7\u54c1 \u7ef4\u5ea6"
WEIGHT_COLUMN = "\u6743\u91cd"
ROWS = (("A", 10), ("B", 20), ("C", 30))
WEIGHTS = {"A": 2, "B": 3, "C": 4}


def expectations() -> dict:
    return {
        "source_rows": list(ROWS),
        "weights": WEIGHTS,
        "expected_total": sum(amount for _, amount in ROWS),
        "expected_weighted": sum(amount * WEIGHTS[category] for category, amount in ROWS),
        "relationship": f"Sales.Category -> {PRODUCT_TABLE}.Category",
        "pages": list(RICH_PAGES),
        "visual_types": ["card", "clusteredColumnChart", "tableEx"],
    }


def _projection(kind: str, table: str, name: str) -> dict:
    return {
        "field": {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}},
        "queryRef": f"{table}.{name}",
        "nativeQueryRef": name,
    }


def _visual(name: str, kind: str, x: int, y: int, width: int, height: int, query: dict) -> bytes:
    return _json({
        "$schema": SCHEMA + "item/report/definition/visualContainer/2.0.0/schema.json",
        "name": name,
        "position": {"x": x, "y": y, "z": 0, "width": width, "height": height, "tabOrder": 0},
        "visual": {"visualType": kind, "query": {"queryState": query}, "drillFilterOtherVisuals": True},
    })


def rich_fixture_files() -> dict[str, bytes]:
    files = fixture_files()
    files[RICH_POINTER] = files.pop("Synthetic.pbip")
    pages = "Synthetic.Report/definition/pages/"
    files[pages + "pages.json"] = _json({
        "$schema": SCHEMA + "item/report/definition/pagesMetadata/1.0.0/schema.json",
        "pageOrder": ["SyntheticOverview", "Details"], "activePageName": "SyntheticOverview",
    })
    for name, display in zip(("SyntheticOverview", "Details"), RICH_PAGES):
        files[pages + name + "/page.json"] = _json({
            "$schema": SCHEMA + "item/report/definition/page/2.0.0/schema.json",
            "name": name, "displayName": display, "displayOption": "FitToPage", "height": 720, "width": 1280,
        })
    total = _projection("Measure", "Sales", "Total Amount")
    weighted = _projection("Measure", "Sales", WEIGHTED_MEASURE)
    category = _projection("Column", "Sales", "Category")
    files[pages + "SyntheticOverview/visuals/TotalAmountCard/visual.json"] = _visual(
        "TotalAmountCard", "card", 60, 40, 420, 210, {"Values": {"projections": [total]}},
    )
    files[pages + "SyntheticOverview/visuals/WeightedCard/visual.json"] = _visual(
        "WeightedCard", "card", 600, 40, 480, 210, {"Values": {"projections": [weighted]}},
    )
    files[pages + "SyntheticOverview/visuals/CategoryChart/visual.json"] = _visual(
        "CategoryChart", "clusteredColumnChart", 60, 290, 1120, 350,
        {"Category": {"projections": [category]}, "Y": {"projections": [total]}},
    )
    files[pages + "Details/visuals/DetailTable/visual.json"] = _visual(
        "DetailTable", "tableEx", 60, 50, 650, 500,
        {"Values": {"projections": [category, total, weighted]}},
    )
    files[pages + "Details/visuals/WeightedCard/visual.json"] = _visual(
        "WeightedCard", "card", 790, 80, 390, 230, {"Values": {"projections": [weighted]}},
    )
    model = "Synthetic.SemanticModel/definition/"
    files[model + "model.tmdl"] += f"ref table '{PRODUCT_TABLE}'\n".encode("utf-8")
    files[model + "tables/Sales.tmdl"] += (
        f"\n\tmeasure '{WEIGHTED_MEASURE}' = SUMX(Sales, Sales[Amount] * RELATED('{PRODUCT_TABLE}'[{WEIGHT_COLUMN}]))\n"
        "\t\tformatString: 0\n"
        "\t\tlineageTag: c8bcc4e4-2306-4695-9c28-615901381517\n"
    ).encode("utf-8")
    files[model + "tables/Product.tmdl"] = (
        f"table '{PRODUCT_TABLE}'\n"
        "\tlineageTag: 9e6b1291-4fa8-40a9-abbb-80a9c6d56c75\n"
        "\n\tcolumn Category\n\t\tdataType: string\n\t\tisKey\n\t\tsummarizeBy: none\n\t\tsourceColumn: Category\n"
        f"\n\tcolumn '{WEIGHT_COLUMN}'\n\t\tdataType: int64\n\t\tsummarizeBy: none\n\t\tsourceColumn: {WEIGHT_COLUMN}\n"
        f"\n\tpartition '{PRODUCT_TABLE}' = m\n\t\tmode: import\n\t\tsource =\n"
        "\t\t\t\t#table(type table [Category = text, " + WEIGHT_COLUMN
        + ' = Int64.Type], {{"A", 2}, {"B", 3}, {"C", 4}})\n'
    ).encode("utf-8")
    files[model + "relationships.tmdl"] = (
        "relationship 77e2d177-9d70-43fb-ab63-d1b5a7b6aada\n"
        "\tfromColumn: Sales.Category\n"
        f"\ttoColumn: '{PRODUCT_TABLE}'.Category\n"
    ).encode("utf-8")
    return files

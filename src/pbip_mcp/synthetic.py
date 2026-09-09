"""Our original fixture, using Microsoft's published PBIP/PBIR/TMDL schemas."""

import argparse
import json
import zipfile
from pathlib import Path

from .contracts import ProjectInfo

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/"


def _json(document: dict) -> bytes:
    return (json.dumps(document, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def fixture_files() -> dict[str, bytes]:
    report = "Synthetic.Report"
    model = "Synthetic.SemanticModel"
    page = "SyntheticOverview"
    return {
        "Synthetic.pbip": _json({
            "$schema": SCHEMA + "pbip/pbipProperties/1.0.0/schema.json",
            "version": "1.0",
            "artifacts": [{"report": {"path": report}}],
            "settings": {"enableAutoRecovery": False},
        }),
        report + "/definition.pbir": _json({
            "$schema": SCHEMA + "item/report/definitionProperties/2.0.0/schema.json",
            "version": "4.0",
            "datasetReference": {"byPath": {"path": "../" + model}},
        }),
        report + "/definition/version.json": _json({
            "$schema": SCHEMA + "item/report/definition/versionMetadata/1.0.0/schema.json",
            "version": "2.0.0",
        }),
        report + "/definition/report.json": _json({
            "$schema": SCHEMA + "item/report/definition/report/3.0.0/schema.json",
            "themeCollection": {},
            "settings": {"useEnhancedTooltips": True},
        }),
        report + "/definition/pages/pages.json": _json({
            "$schema": SCHEMA + "item/report/definition/pagesMetadata/1.0.0/schema.json",
            "pageOrder": [page],
            "activePageName": page,
        }),
        report + "/definition/pages/" + page + "/page.json": _json({
            "$schema": SCHEMA + "item/report/definition/page/2.0.0/schema.json",
            "name": page,
            "displayName": "Synthetic overview",
            "displayOption": "FitToPage",
            "height": 720,
            "width": 1280,
        }),
        report + "/definition/pages/" + page + "/visuals/TotalAmountCard/visual.json": _json({
            "$schema": SCHEMA + "item/report/definition/visualContainer/2.0.0/schema.json",
            "name": "TotalAmountCard",
            "position": {"x": 80, "y": 80, "z": 0, "width": 360, "height": 240, "tabOrder": 0},
            "visual": {
                "visualType": "card",
                "query": {"queryState": {"Values": {"projections": [{
                    "field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Total Amount"}},
                    "queryRef": "Sales.Total Amount",
                    "nativeQueryRef": "Total Amount",
                }]}}},
                "drillFilterOtherVisuals": True,
            },
        }),
        model + "/definition.pbism": _json({
            "$schema": SCHEMA + "item/semanticModel/definitionProperties/1.0.0/schema.json",
            "version": "4.0",
            "settings": {"qnaEnabled": False},
        }),
        model + "/definition/database.tmdl": b"database\n\tcompatibilityLevel: 1601\n",
        model + "/definition/model.tmdl": (
            "model Model\n"
            "\tculture: en-US\n"
            "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
            "\tsourceQueryCulture: en-US\n"
            "\tdataAccessOptions\n"
            "\t\tlegacyRedirects\n"
            "\t\treturnErrorValuesAsNull\n"
            "\nref table Sales\n"
        ).encode("utf-8"),
        model + "/definition/tables/Sales.tmdl": (
            "table Sales\n"
            "\tlineageTag: 98e66bd7-0a76-4919-8bc6-12e10a740004\n"
            "\n\tmeasure 'Total Amount' = SUM(Sales[Amount])\n"
            "\t\tformatString: 0\n"
            "\t\tlineageTag: b360e064-f985-4453-a5e4-feb55fa62816\n"
            "\n\tcolumn Category\n"
            "\t\tdataType: string\n"
            "\t\tlineageTag: a45faee0-5f29-4baf-8aaf-04f38ef2d88e\n"
            "\t\tsummarizeBy: none\n"
            "\t\tsourceColumn: Category\n"
            "\n\tcolumn Amount\n"
            "\t\tdataType: int64\n"
            "\t\tformatString: 0\n"
            "\t\tlineageTag: ab11c181-6a3c-4b20-9e39-986cb12ee37a\n"
            "\t\tsummarizeBy: sum\n"
            "\t\tsourceColumn: Amount\n"
            "\n\tpartition Sales = m\n"
            "\t\tmode: import\n"
            "\t\tsource =\n"
            "\t\t\t\t#table(type table [Category = text, Amount = Int64.Type], {{\"A\", 10}, {\"B\", 20}, {\"C\", 30}})\n"
            "\n\tannotation PBI_ResultType = Table\n"
        ).encode("utf-8"),
    }


def matches_fixture(archive: zipfile.ZipFile, project: ProjectInfo) -> bool:
    """Exact whole-project equality, not an untrusted uploaded 'safe' flag."""
    from .fixture_variants import RICH_POINTER, rich_fixture_files

    if project.pointer.endswith("Synthetic.pbip"):
        expected, pointer = fixture_files(), "Synthetic.pbip"
    elif project.pointer.endswith(RICH_POINTER):
        expected, pointer = rich_fixture_files(), RICH_POINTER
    else:
        return False
    prefix = project.pointer.removesuffix(pointer)
    actual = {info.filename for info in archive.infolist() if not info.is_dir()}
    if actual != {prefix + path for path in expected}:
        return False
    return all(archive.read(prefix + name) == data for name, data in expected.items())


def create_fixture(directory: Path, archive_path: Path, *, variant: str = "baseline") -> None:
    if variant not in ("baseline", "rich"):
        raise ValueError("Unknown trusted fixture variant.")
    if directory.exists() or archive_path.exists():
        raise FileExistsError("Fixture target already exists; choose a new output location.")
    directory.mkdir(parents=True)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    from .fixture_variants import rich_fixture_files

    files = fixture_files() if variant == "baseline" else rich_fixture_files()
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            target = directory.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            archive.writestr(name, data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a complete embedded-data synthetic PBIP project.")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--zip", type=Path, required=True, dest="archive")
    parser.add_argument("--variant", choices=("baseline", "rich"), default="baseline")
    args = parser.parse_args()
    create_fixture(args.directory, args.archive, variant=args.variant)
    print(json.dumps({"project": str(args.directory), "archive": str(args.archive)}))


if __name__ == "__main__":
    main()

"""
Generate a Power BI project (PBIP) that imports every mart CSV in exports/,
pre-wires the SEQN relationships, and defines a set of headline DAX measures —
so setup is just: (1) run the data pipeline, (2) open the generated .pbip.

Usage (from repo root):
  python scripts/build_dataset.py          # step 1: build exports/*.csv
  python scripts/build_powerbi_project.py  # step 2: generate the .pbip
  # then open powerbi/NHANES.pbip in Power BI Desktop

IMPORTANT — read before opening:
  * Power BI Desktop must have these Preview features enabled
    (File > Options and settings > Options > Preview features):
      - "Store semantic model using TMDL format"
      - "Store reports using enhanced metadata format (PBIR)"
    Power BI Project (.pbip) save support itself is GA in current Desktop.
  * The semantic model reads the CSVs by ABSOLUTE PATH, baked in at generation
    time (see the `ExportsFolder` parameter in the model). If you move the repo,
    re-run this script, or edit that parameter in Power Query (Transform data).
  * This generator was not verifiable end-to-end by its author (no Power BI
    Desktop available at build time). The format is version-sensitive; if
    Desktop reports a load error it will name the offending file — that's the
    thing to fix/report back.

The star-schema marts are included (dim_respondents + fact_* + metrics_summary);
the two legacy wide tables (nhanes_respondents, nhanes_peer_group_summary) are
intentionally left out to keep the model clean.
"""

import json
import uuid
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
EXPORTS = REPO / "exports"
PROJECT_DIR = REPO / "powerbi"
NAME = "NHANES"
MODEL_DIR = PROJECT_DIR / f"{NAME}.SemanticModel"
REPORT_DIR = PROJECT_DIR / f"{NAME}.Report"

# star-schema tables only (skip the two legacy wide files)
TABLES = [
    "dim_respondents", "fact_body_measures", "fact_blood_pressure",
    "fact_labs", "fact_diagnoses", "fact_anomalies", "metrics_summary",
]

# pandas dtype -> (TMDL dataType, M type expression)
TYPE_MAP = {
    "int64":   ("int64",   "Int64.Type"),
    "float64": ("double",  "type number"),
    "bool":    ("boolean", "type logical"),
    "object":  ("string",  "type text"),
}

# safe, single-table measures (attached to their home table). The cross-table
# care-gap measures stay in powerbi/measures.md for manual paste — they're more
# error-prone and a bad measure blocks the whole model from loading.
MEASURES = {
    "dim_respondents": [
        ("Respondents", "COUNTROWS ( dim_respondents )", None),
    ],
    "fact_body_measures": [
        ("BMI Measured",
         "COUNTROWS ( FILTER ( fact_body_measures, NOT ( ISBLANK ( fact_body_measures[bmi] ) ) ) )", None),
        ("Obesity Rate",
         "DIVIDE ( COUNTROWS ( FILTER ( fact_body_measures, fact_body_measures[bmi] >= 30 ) ), [BMI Measured] )",
         "0.0%"),
        ("Overweight Plus Rate",
         "DIVIDE ( COUNTROWS ( FILTER ( fact_body_measures, fact_body_measures[bmi] >= 25 ) ), [BMI Measured] )",
         "0.0%"),
    ],
    "fact_labs": [
        ("HbA1c Measured",
         "COUNTROWS ( FILTER ( fact_labs, NOT ( ISBLANK ( fact_labs[hba1c] ) ) ) )", None),
        ("Prediabetes Rate",
         "DIVIDE ( COUNTROWS ( FILTER ( fact_labs, fact_labs[hba1c] >= 5.7 && fact_labs[hba1c] < 6.5 ) ), [HbA1c Measured] )",
         "0.0%"),
        ("Diabetic-Range Rate",
         "DIVIDE ( COUNTROWS ( FILTER ( fact_labs, fact_labs[hba1c] >= 6.5 ) ), [HbA1c Measured] )",
         "0.0%"),
    ],
    "fact_anomalies": [
        ("Flagged Anomalies", "COUNTROWS ( fact_anomalies )", None),
        ("Respondents With Any Anomaly", "DISTINCTCOUNT ( fact_anomalies[SEQN] )", None),
    ],
    "metrics_summary": [
        # one row per metric, so SUM = the metric's value; lets a chart put
        # value_pct on an axis via a measure (a raw column in a value well
        # renders blank without an aggregation wrapper).
        ("Metric Value", "SUM ( metrics_summary[value_pct] )", "0.0"),
    ],
}


def guid() -> str:
    return str(uuid.uuid4())


def quote(name: str) -> str:
    """TMDL: single-quote a name if it contains a space or special char."""
    if any(c in name for c in " .=:'") or not name:
        return "'" + name.replace("'", "''") + "'"
    return name


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def table_tmdl(table: str, df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = [f"table {quote(table)}", f"\tlineageTag: {guid()}", ""]

    # measures first (nice grouping)
    for mname, expr, fmt in MEASURES.get(table, []):
        lines.append(f"\tmeasure {quote(mname)} = {expr}")
        if fmt:
            lines.append(f"\t\tformatString: {fmt}")
        lines.append(f"\t\tlineageTag: {guid()}")
        lines.append("")

    # columns
    for col in cols:
        dt = str(df[col].dtype)
        tmdl_type, _ = TYPE_MAP.get(dt, ("string", "type text"))
        lines.append(f"\tcolumn {quote(col)}")
        lines.append(f"\t\tdataType: {tmdl_type}")
        lines.append(f"\t\tlineageTag: {guid()}")
        lines.append(f"\t\tsourceColumn: {col}")
        if tmdl_type in ("int64", "double"):
            lines.append("\t\tsummarizeBy: none" if col == "SEQN" else "\t\tsummarizeBy: sum")
        else:
            lines.append("\t\tsummarizeBy: none")
        lines.append("")

    # M partition
    transforms = ", ".join(
        f'{{"{col}", {TYPE_MAP.get(str(df[col].dtype), ("string", "type text"))[1]}}}'
        for col in cols
    )
    ncols = len(cols)
    lines += [
        f"\tpartition {quote(table)} = m",
        "\t\tmode: import",
        "\t\tsource =",
        "\t\t\tlet",
        f'\t\t\t\tSource = Csv.Document(File.Contents(ExportsFolder & "{table}.csv"), '
        f"[Delimiter=\",\", Columns={ncols}, Encoding=65001, QuoteStyle=QuoteStyle.Csv]),",
        "\t\t\t\tPromoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),",
        f"\t\t\t\tTyped = Table.TransformColumnTypes(Promoted, {{{transforms}}})",
        "\t\t\tin",
        "\t\t\t\tTyped",
        "",
    ]
    return "\n".join(lines)


def relationships_tmdl() -> str:
    """dim_respondents[SEQN] (one) -> each fact[SEQN] (many)."""
    blocks = []
    for table in TABLES:
        if table in ("dim_respondents", "metrics_summary"):
            continue  # metrics_summary is standalone (no SEQN)
        blocks.append(
            f"relationship {guid()}\n"
            f"\tfromColumn: {quote(table)}.SEQN\n"
            f"\ttoColumn: dim_respondents.SEQN\n"
        )
    return "\n".join(blocks)


# ---- starter report visuals (PBIR visual.json) ---------------------------
# Field/query shapes mirror Microsoft's official pbip-demo visual.json files.
# A "field" is (kind, entity, property): kind "Measure" or "Column"; entity is
# the home table. queryRef = "<entity>.<property>", the form Desktop writes.

def _field(kind: str, entity: str, prop: str) -> dict:
    return {
        "field": {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
        "queryRef": f"{entity}.{prop}",
        "nativeQueryRef": prop,
    }


def _visual(name, vtype, x, y, w, h, z, query_state, sort_field=None):
    visual = {"visualType": vtype, "query": {"queryState": query_state}, "drillFilterOtherVisuals": True}
    if sort_field:
        kind, entity, prop = sort_field
        visual["query"]["sortDefinition"] = {
            "sort": [{"field": {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
                      "direction": "Descending"}]
        }
    return name, {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.0.0/schema.json",
        "name": name,
        "position": {"x": x, "y": y, "z": z, "width": w, "height": h, "tabOrder": z},
        "visual": visual,
    }


def build_overview_visuals() -> list:
    """Page 1 — Overview: 3 KPI cards + 2 bar charts."""
    return [
        # --- KPI cards (role "Data" takes measures) ---
        _visual("card_respondents", "cardVisual", 20, 20, 400, 120, 1000,
                {"Data": {"projections": [_field("Measure", "dim_respondents", "Respondents")]}}),
        _visual("card_obesity", "cardVisual", 440, 20, 400, 120, 2000,
                {"Data": {"projections": [_field("Measure", "fact_body_measures", "Obesity Rate")]}}),
        _visual("card_diabetic", "cardVisual", 860, 20, 400, 120, 3000,
                {"Data": {"projections": [_field("Measure", "fact_labs", "Diabetic-Range Rate")]}}),
        # --- KPI overview bar: every headline metric, via the Metric Value
        #     measure (a raw column in a value well renders blank) ---
        _visual("bar_kpis", "clusteredBarChart", 20, 160, 610, 540, 4000,
                {"Category": {"projections": [_field("Column", "metrics_summary", "metric")]},
                 "Y": {"projections": [_field("Measure", "metrics_summary", "Metric Value")]}},
                sort_field=("Measure", "metrics_summary", "Metric Value")),
        # --- anomalies by category (Flagged Anomalies is a measure) ---
        _visual("bar_anomalies", "clusteredBarChart", 650, 160, 610, 540, 5000,
                {"Category": {"projections": [_field("Column", "fact_anomalies", "field_category")]},
                 "Y": {"projections": [_field("Measure", "fact_anomalies", "Flagged Anomalies")]}},
                sort_field=("Measure", "fact_anomalies", "Flagged Anomalies")),
    ]


def build_demographics_visuals() -> list:
    """Page 2 — Prevalence by Demographics: baked-in rate measures sliced by
    dim_respondents columns (proven measure-in-Y pattern, all measures exist).
    NOTE: the undiagnosed care-gap measures are intentionally NOT baked into the
    model (a bad cross-table DAX measure blocks the whole model from loading);
    they live in powerbi/measures.md for manual paste. This page uses only the
    safe rate measures so it renders out of the box."""
    return [
        _visual("card_overweight", "cardVisual", 20, 20, 400, 120, 1000,
                {"Data": {"projections": [_field("Measure", "fact_body_measures", "Overweight Plus Rate")]}}),
        _visual("card_prediabetes", "cardVisual", 440, 20, 400, 120, 2000,
                {"Data": {"projections": [_field("Measure", "fact_labs", "Prediabetes Rate")]}}),
        _visual("card_anom_respondents", "cardVisual", 860, 20, 400, 120, 3000,
                {"Data": {"projections": [_field("Measure", "fact_anomalies", "Respondents With Any Anomaly")]}}),
        # obesity rate by age band
        _visual("bar_obesity_by_age", "clusteredBarChart", 20, 160, 610, 260, 4000,
                {"Category": {"projections": [_field("Column", "dim_respondents", "age_band")]},
                 "Y": {"projections": [_field("Measure", "fact_body_measures", "Obesity Rate")]}}),
        # diabetic-range rate by age band
        _visual("bar_diabetic_by_age", "clusteredBarChart", 20, 440, 610, 260, 5000,
                {"Category": {"projections": [_field("Column", "dim_respondents", "age_band")]},
                 "Y": {"projections": [_field("Measure", "fact_labs", "Diabetic-Range Rate")]}}),
        # prediabetes rate by gender
        _visual("bar_prediabetes_by_gender", "clusteredBarChart", 650, 160, 610, 260, 6000,
                {"Category": {"projections": [_field("Column", "dim_respondents", "gender")]},
                 "Y": {"projections": [_field("Measure", "fact_labs", "Prediabetes Rate")]}}),
        # obesity rate by gender
        _visual("bar_obesity_by_gender", "clusteredBarChart", 650, 440, 610, 260, 7000,
                {"Category": {"projections": [_field("Column", "dim_respondents", "gender")]},
                 "Y": {"projections": [_field("Measure", "fact_body_measures", "Obesity Rate")]}}),
    ]


# (page_id, display name, visuals) — page_id is the folder name; first is active
PAGES = [
    ("overview", "Overview", build_overview_visuals),
    ("demographics", "Prevalence by Demographics", build_demographics_visuals),
]


def main():
    if not EXPORTS.exists() or not (EXPORTS / "dim_respondents.csv").exists():
        raise SystemExit("exports/ is missing — run `python scripts/build_dataset.py` first.")

    # Remove stale report artifacts from earlier format attempts so a leftover
    # file can't confuse Desktop (e.g. a legacy report.json sitting next to the
    # new-PBIR definition/ folder). Overwriting everything else in place is fine.
    # ignore_errors so a lock doesn't crash the run — but if the project is open
    # in Power BI Desktop the .tmdl/.json writes below will fail too, so CLOSE it
    # before regenerating.
    import shutil
    stale_legacy_report = REPORT_DIR / "report.json"  # from the legacy attempt
    if stale_legacy_report.exists():
        try:
            stale_legacy_report.unlink()
        except OSError:
            pass

    exports_path = str(EXPORTS.resolve()).replace("\\", "/") + "/"

    # ---- semantic model ----
    write(MODEL_DIR / "definition" / "database.tmdl",
          f"database\n\tcompatibilityLevel: 1567\n")

    model_refs = "\n".join(f"ref table {quote(t)}" for t in TABLES)
    write(MODEL_DIR / "definition" / "model.tmdl",
          "model Model\n"
          "\tculture: en-US\n"
          "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
          "\tsourceQueryCulture: en-US\n\n"
          "ref cultureInfo en-US\n\n"
          + model_refs + "\n")

    write(MODEL_DIR / "definition" / "expressions.tmdl",
          f'expression ExportsFolder = "{exports_path}" meta '
          f'[IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n')

    write(MODEL_DIR / "definition" / "relationships.tmdl", relationships_tmdl())

    for table in TABLES:
        df = pd.read_csv(EXPORTS / f"{table}.csv", nrows=500)
        write(MODEL_DIR / "definition" / "tables" / f"{table}.tmdl", table_tmdl(table, df))

    write(MODEL_DIR / "definition.pbism", json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.0", "settings": {},
    }, indent=2))

    write(MODEL_DIR / ".platform", json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "SemanticModel", "displayName": NAME},
        "config": {"version": "2.0", "logicalId": guid()},
    }, indent=2))

    # ---- report (new PBIR format, multi-page with starter visuals) ----
    # Structure + exact property shapes mirror Microsoft's official pbip-demo
    # (github.com/RuiRomano/pbip-demo). Earlier failures walked up a chain of
    # missing pieces: (1) report.json with only $schema failed at
    # 'visualContainers'; (2) after adding a base theme it failed at
    # 'themeCurrent ... customTheme' — Desktop's ribbon reads BOTH
    # themeCollection.baseTheme AND .customTheme, so the demo always ships a
    # base theme (SharedResources) AND a custom theme (RegisteredResources).
    # This version ships both theme files.
    report_version = "5.61"

    write(REPORT_DIR / "definition.pbir", json.dumps({
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}},
    }, indent=2))

    write(REPORT_DIR / "definition" / "version.json", json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
        "version": "2.0.0",
    }, indent=2))

    write(REPORT_DIR / "definition" / "report.json", json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/1.3.0/schema.json",
        # required by the report/1.3.0 schema (along with $schema + themeCollection);
        # "None" = no mobile-optimized layout
        "layoutOptimization": "None",
        "themeCollection": {
            "baseTheme": {"name": "CY24SU10", "reportVersionAtImport": report_version, "type": "SharedResources"},
            "customTheme": {"name": "NHANES.json", "reportVersionAtImport": report_version, "type": "RegisteredResources"},
        },
        "resourcePackages": [
            {"name": "SharedResources", "type": "SharedResources",
             "items": [{"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": "BaseTheme"}]},
            {"name": "RegisteredResources", "type": "RegisteredResources",
             "items": [{"name": "NHANES.json", "path": "NHANES.json", "type": "CustomTheme"}]},
        ],
        "settings": {"useStylableVisualContainerHeader": True},
    }, indent=2))

    write(REPORT_DIR / "definition" / "pages" / "pages.json", json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
        "pageOrder": [pid for pid, _, _ in PAGES], "activePageName": PAGES[0][0],
    }, indent=2))

    total_visuals = 0
    for pid, pdisplay, vbuilder in PAGES:
        write(REPORT_DIR / "definition" / "pages" / pid / "page.json", json.dumps({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/1.4.0/schema.json",
            "name": pid, "displayName": pdisplay,
            "displayOption": "FitToPage", "height": 720, "width": 1280,
        }, indent=2))
        # visuals — one folder + visual.json each; Desktop discovers them from
        # the folder structure, no need to list them in page.json
        for vname, vdoc in vbuilder():
            write(REPORT_DIR / "definition" / "pages" / pid / "visuals" / vname / "visual.json",
                  json.dumps(vdoc, indent=2))
            total_visuals += 1

    # ship both theme files the report references (else Desktop errors on them):
    # base theme -> StaticResources/SharedResources/BaseThemes/, custom theme ->
    # StaticResources/RegisteredResources/
    base_dst = REPORT_DIR / "StaticResources" / "SharedResources" / "BaseThemes" / "CY24SU10.json"
    base_dst.parent.mkdir(parents=True, exist_ok=True)
    base_dst.write_text((PROJECT_DIR / "templates" / "CY24SU10.json").read_text(encoding="utf-8"), encoding="utf-8")

    custom_dst = REPORT_DIR / "StaticResources" / "RegisteredResources" / "NHANES.json"
    custom_dst.parent.mkdir(parents=True, exist_ok=True)
    custom_dst.write_text((PROJECT_DIR / "templates" / "NHANES-theme.json").read_text(encoding="utf-8"), encoding="utf-8")

    write(REPORT_DIR / ".platform", json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": NAME},
        "config": {"version": "2.0", "logicalId": guid()},
    }, indent=2))

    # ---- .pbip pointer ----
    write(PROJECT_DIR / f"{NAME}.pbip", json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{NAME}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    }, indent=2))

    print(f"Generated {PROJECT_DIR / (NAME + '.pbip')}")
    print(f"  semantic model: {len(TABLES)} tables, reading CSVs from {exports_path}")
    print("  measures:", sum(len(m) for m in MEASURES.values()))
    print(f"  report: {len(PAGES)} pages, {total_visuals} visuals")
    print("\nOpen NHANES.pbip in Power BI Desktop (with TMDL + PBIR preview features enabled).")


if __name__ == "__main__":
    main()

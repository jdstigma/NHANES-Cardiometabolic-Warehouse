"""
Single-script pipeline: download NHANES 2021-2023 cardiometabolic data,
merge on respondent ID, flag statistical outliers, and export a star
schema of CSVs for Power BI.

No database, no Codespaces — everything runs locally.

Usage:
  python scripts/build_dataset.py

Outputs (in exports/), all keyed on SEQN:
  dim_respondents.csv          1 row/respondent: demographics
  fact_body_measures.csv       1 row/respondent: weight/height/BMI/waist + outlier flags
  fact_blood_pressure.csv      1 row/respondent: mean systolic/diastolic + outlier flags
  fact_labs.csv                1 row/respondent: lipids/glucose/HbA1c/insulin + outlier flags
  fact_diagnoses.csv           1 row/respondent: self-reported diagnosis history
  fact_anomalies.csv           1 row per flagged (respondent, field) — long format
  nhanes_respondents.csv       legacy wide table (kept for the original .pbix)
  nhanes_peer_group_summary.csv  legacy aggregate by age band + gender

Data source: CDC NHANES August 2021-August 2023 cycle (file suffix _L).
https://wwwn.cdc.gov/nchs/nhanes/search/DataPage.aspx

Note: this is UNWEIGHTED. NHANES sample weights (WTINT2YR, WTMEC2YR,
WTSAF2YR) are not applied, so nothing here should be read as a
population-representative prevalence estimate — it's respondent-level
outlier detection within the sample itself.
"""

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from tqdm import tqdm

# pandas 2.2.2's chained-assignment heuristic false-positives on every
# plain df["col"] = ... assignment under Python 3.14 (reproduces on a
# fresh, unrelated DataFrame — an environment quirk, not a real risk here;
# copy_on_write is off and nothing relies on mutating a view in place).
warnings.filterwarnings("ignore", category=FutureWarning, message=".*ChainedAssignmentError.*")

DATA_DIR = Path("data/raw")
EXPORT_DIR = Path("exports")
BASE_URL = "https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/2021/DataFiles"

# file stem -> {raw column: output column}. SEQN (respondent ID) is
# always kept and is the join key across every file.
FILES = {
    "DEMO_L": {
        "RIAGENDR": "gender_code",
        "RIDAGEYR": "age_years",
        "RIDRETH3": "race_code",
        "DMDEDUC2": "education_code",
        "INDFMPIR": "income_poverty_ratio",
    },
    "BMX_L": {
        "BMXWT": "weight_kg",
        "BMXHT": "height_cm",
        "BMXBMI": "bmi",
        "BMXWAIST": "waist_cm",
    },
    "BPXO_L": {
        "BPXOSY1": "systolic_1", "BPXOSY2": "systolic_2", "BPXOSY3": "systolic_3",
        "BPXODI1": "diastolic_1", "BPXODI2": "diastolic_2", "BPXODI3": "diastolic_3",
    },
    "TCHOL_L": {"LBXTC": "total_cholesterol"},
    "HDL_L": {"LBDHDD": "hdl_cholesterol"},
    "TRIGLY_L": {"LBXTLG": "triglycerides", "LBDLDL": "ldl_cholesterol"},
    "GLU_L": {"LBXGLU": "fasting_glucose"},
    "GHB_L": {"LBXGH": "hba1c"},
    "INS_L": {"LBXIN": "insulin"},
    "DIQ_L": {"DIQ010": "diabetes_code"},
    "BPQ_L": {"BPQ020": "high_bp_diagnosis_code", "BPQ080": "high_cholesterol_diagnosis_code"},
    "SMQ_L": {"SMQ020": "smoked_100_cigarettes_code", "SMQ040": "current_smoking_code"},
    "MCQ_L": {
        "MCQ160C": "coronary_heart_disease_code",
        "MCQ160E": "heart_attack_code",
        "MCQ160F": "stroke_code",
    },
}

# NHANES codes 7 (refused) and 9 (don't know) are dropped by simply not
# appearing in these maps -> .map() turns them into NaN.
GENDER_MAP = {1: "Male", 2: "Female"}
RACE_MAP = {
    1: "Mexican American", 2: "Other Hispanic", 3: "Non-Hispanic White",
    4: "Non-Hispanic Black", 6: "Non-Hispanic Asian", 7: "Other/Multi-Racial",
}
EDUCATION_MAP = {
    1: "Less than 9th grade", 2: "9-11th grade", 3: "High school grad/GED",
    4: "Some college or AA degree", 5: "College graduate or above",
}
YES_NO_MAP = {1: "Yes", 2: "No"}
DIABETES_MAP = {1: "Yes", 2: "No", 3: "Borderline"}
SMOKING_MAP = {1: "Every day", 2: "Some days", 3: "Not at all"}

# continuous fields checked for peer-group outliers
ANOMALY_FIELDS = [
    "bmi", "waist_cm", "mean_systolic", "mean_diastolic",
    "total_cholesterol", "hdl_cholesterol", "triglycerides", "ldl_cholesterol",
    "fasting_glucose", "hba1c", "insulin",
]

# field -> (human-readable label, mart category) for fact_anomalies
FIELD_METADATA = {
    "bmi": ("BMI", "Body Measures"),
    "waist_cm": ("Waist Circumference (cm)", "Body Measures"),
    "mean_systolic": ("Systolic BP (mmHg)", "Blood Pressure"),
    "mean_diastolic": ("Diastolic BP (mmHg)", "Blood Pressure"),
    "total_cholesterol": ("Total Cholesterol (mg/dL)", "Labs"),
    "hdl_cholesterol": ("HDL Cholesterol (mg/dL)", "Labs"),
    "triglycerides": ("Triglycerides (mg/dL)", "Labs"),
    "ldl_cholesterol": ("LDL Cholesterol (mg/dL)", "Labs"),
    "fasting_glucose": ("Fasting Glucose (mg/dL)", "Labs"),
    "hba1c": ("HbA1c (%)", "Labs"),
    "insulin": ("Insulin (uU/mL)", "Labs"),
}

AGE_BINS = [-1, 11, 17, 29, 44, 59, 74, 150]
AGE_LABELS = ["0-11", "12-17", "18-29", "30-44", "45-59", "60-74", "75+"]

MIN_PEER_GROUP_SIZE = 10
Z_THRESHOLD = 3


def download_file(stem: str, force: bool = False) -> Path:
    dest = DATA_DIR / f"{stem}.xpt"
    if dest.exists() and not force:
        return dest
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    url = f"{BASE_URL}/{stem}.xpt"
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    with open(dest, "wb") as f, tqdm(total=total, unit="B", unit_scale=True, desc=stem) as bar:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
            bar.update(len(chunk))
    return dest


def load_file(stem: str, columns: dict, force: bool = False) -> pd.DataFrame:
    path = download_file(stem, force=force)
    df = pd.read_sas(path, format="xport")
    keep = ["SEQN"] + list(columns.keys())
    missing = [c for c in keep if c not in df.columns]
    if missing:
        raise KeyError(f"{stem}.xpt has no column(s) {missing} — check FILES against the actual XPT columns")
    df = df[keep].rename(columns=columns).copy()
    df["SEQN"] = df["SEQN"].astype(int)
    return df


def build_merged_table(force: bool = False) -> pd.DataFrame:
    label = "re-downloading" if force else "downloading (cached where present)"
    print(f"NHANES 2021-2023 files — {label} + merging...")
    merged = None
    for stem, columns in FILES.items():
        df = load_file(stem, columns, force=force)
        print(f"  {stem:<10} {len(df):>6,} rows, {len(columns)} fields")
        merged = df if merged is None else merged.merge(df, on="SEQN", how="left")
    return merged


def add_derived_fields(df: pd.DataFrame) -> pd.DataFrame:
    df["gender"] = df["gender_code"].map(GENDER_MAP)
    df["race_ethnicity"] = df["race_code"].map(RACE_MAP)
    df["education"] = df["education_code"].map(EDUCATION_MAP)
    df["diabetes_diagnosis"] = df["diabetes_code"].map(DIABETES_MAP)
    df["high_bp_diagnosis"] = df["high_bp_diagnosis_code"].map(YES_NO_MAP)
    df["high_cholesterol_diagnosis"] = df["high_cholesterol_diagnosis_code"].map(YES_NO_MAP)
    df["smoked_100_cigarettes"] = df["smoked_100_cigarettes_code"].map(YES_NO_MAP)
    df["current_smoking"] = df["current_smoking_code"].map(SMOKING_MAP)
    df["coronary_heart_disease"] = df["coronary_heart_disease_code"].map(YES_NO_MAP)
    df["heart_attack"] = df["heart_attack_code"].map(YES_NO_MAP)
    df["stroke"] = df["stroke_code"].map(YES_NO_MAP)

    # SMQ040 (current_smoking) only gets asked of people who said yes to
    # SMQ020 (ever smoked 100+ cigarettes) — collapsing both into one
    # three-level factor gives a cleaner category for regression/grouping
    # than either raw field alone (never-smokers are NaN on SMQ040).
    df["smoking_status"] = np.select(
        [
            df["smoked_100_cigarettes"] == "No",
            df["current_smoking"].isin(["Every day", "Some days"]),
            df["current_smoking"] == "Not at all",
        ],
        ["Never smoker", "Current smoker", "Former smoker"],
        default=np.nan,
    )

    df["mean_systolic"] = df[["systolic_1", "systolic_2", "systolic_3"]].mean(axis=1, skipna=True)
    df["mean_diastolic"] = df[["diastolic_1", "diastolic_2", "diastolic_3"]].mean(axis=1, skipna=True)

    df["age_band"] = pd.cut(df["age_years"], bins=AGE_BINS, labels=AGE_LABELS)
    return df


def flag_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    """Z-score each continuous field against its age_band + gender peer
    group. Peer groups smaller than MIN_PEER_GROUP_SIZE are left
    unscored (NaN) rather than flagged on a noisy small sample."""
    group_cols = ["age_band", "gender"]
    anomaly_flag_cols = []

    for field in ANOMALY_FIELDS:
        group = df.groupby(group_cols, observed=True)[field]
        mean = group.transform("mean")
        std = group.transform("std")
        count = group.transform("count")

        z = (df[field] - mean) / std
        z[(count < MIN_PEER_GROUP_SIZE) | std.isna() | (std == 0)] = np.nan

        z_col, flag_col = f"{field}_zscore", f"{field}_is_outlier"
        df[z_col] = z.round(2)
        df[flag_col] = z.abs() >= Z_THRESHOLD
        anomaly_flag_cols.append(flag_col)

    df["anomaly_count"] = df[anomaly_flag_cols].sum(axis=1)
    df["is_any_anomaly"] = df["anomaly_count"] > 0
    return df


def build_peer_group_summary(df: pd.DataFrame) -> pd.DataFrame:
    agg = {f: "mean" for f in ANOMALY_FIELDS}
    agg["SEQN"] = "count"
    agg["is_any_anomaly"] = "mean"

    summary = (
        df.groupby(["age_band", "gender"], observed=True)
        .agg(agg)
        .rename(columns={"SEQN": "respondent_count", "is_any_anomaly": "anomaly_rate"})
        .round(2)
        .reset_index()
    )
    return summary


def build_dim_respondents(df: pd.DataFrame) -> pd.DataFrame:
    """Star-schema dimension: one row per respondent, demographics + coverage
    flags. The flags mark which measurement domains a respondent actually has —
    NHANES doesn't measure everything on everyone (exam vs. interview, the
    morning fasting-labs subsample, adult-only questionnaires), so most rows are
    blank in some domain. Filtering on a flag in Power BI drops those blanks in
    one click without misrepresenting the data as missing/erroneous."""
    out = df[[
        "SEQN", "age_years", "age_band", "gender", "race_ethnicity",
        "education", "income_poverty_ratio",
    ]].copy()
    out["is_adult"] = df["age_years"] >= 18
    out["has_body_measures"] = df["bmi"].notna()
    out["has_blood_pressure"] = df["mean_systolic"].notna()
    out["has_labs"] = df[["total_cholesterol", "hba1c"]].notna().any(axis=1)
    out["has_fasting_labs"] = df["fasting_glucose"].notna()  # morning subsample
    out["has_diagnosis_data"] = df["high_bp_diagnosis"].notna()  # adult questionnaire
    return out


def build_fact_body_measures(df: pd.DataFrame) -> pd.DataFrame:
    return df[[
        "SEQN", "weight_kg", "height_cm",
        "bmi", "bmi_zscore", "bmi_is_outlier",
        "waist_cm", "waist_cm_zscore", "waist_cm_is_outlier",
    ]].copy()


def build_fact_blood_pressure(df: pd.DataFrame) -> pd.DataFrame:
    return df[[
        "SEQN",
        "mean_systolic", "mean_systolic_zscore", "mean_systolic_is_outlier",
        "mean_diastolic", "mean_diastolic_zscore", "mean_diastolic_is_outlier",
    ]].copy()


def build_fact_labs(df: pd.DataFrame) -> pd.DataFrame:
    lab_fields = [
        "total_cholesterol", "hdl_cholesterol", "triglycerides",
        "ldl_cholesterol", "fasting_glucose", "hba1c", "insulin",
    ]
    cols = ["SEQN"]
    for f in lab_fields:
        cols += [f, f"{f}_zscore", f"{f}_is_outlier"]
    return df[cols].copy()


def build_fact_diagnoses(df: pd.DataFrame) -> pd.DataFrame:
    return df[[
        "SEQN", "diabetes_diagnosis", "high_bp_diagnosis", "high_cholesterol_diagnosis",
        "smoked_100_cigarettes", "current_smoking", "smoking_status",
        "coronary_heart_disease", "heart_attack", "stroke",
    ]].copy()


def build_fact_care_gaps(df: pd.DataFrame) -> pd.DataFrame:
    """Per-respondent care-gap flags (adults 18+): whether a respondent is
    condition-positive by biomarker, and whether that condition is undiagnosed.
    Precomputed here (0/1 ints) so the Power BI care-gap rates are trivial
    single-table measures — DIVIDE(SUM(undiagnosed), SUM(condition)) — instead
    of fragile cross-table DAX that could block the whole model from loading.
    NaN comparisons are False, so non-measured respondents contribute 0 to both
    numerator and denominator and don't distort the rate."""
    adult = df["age_years"] >= 18
    out = pd.DataFrame({"SEQN": df["SEQN"]})

    diabetic = adult & (df["hba1c"] >= 6.5)
    out["diabetic_range"] = diabetic.astype(int)
    out["undiagnosed_diabetes"] = (diabetic & (df["diabetes_diagnosis"] == "No")).astype(int)

    hypertensive = adult & ((df["mean_systolic"] >= 130) | (df["mean_diastolic"] >= 80))
    out["hypertensive"] = hypertensive.astype(int)
    out["undiagnosed_hypertension"] = (hypertensive & (df["high_bp_diagnosis"] == "No")).astype(int)

    high_ldl = adult & (df["ldl_cholesterol"] >= 160)
    out["high_ldl"] = high_ldl.astype(int)
    out["undiagnosed_high_cholesterol"] = (high_ldl & (df["high_cholesterol_diagnosis"] == "No")).astype(int)

    return out


def build_fact_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    """Long format: one row per (respondent, field) that's flagged as an
    outlier, rather than wide boolean columns — much easier to slice by
    field/category/demographic in Power BI. Joins back to dim_respondents
    on SEQN for demographic slicing (1-respondent-to-many-anomalies)."""
    rows = []
    for field, (label, category) in FIELD_METADATA.items():
        flagged = df[df[f"{field}_is_outlier"]]
        if flagged.empty:
            continue
        rows.append(pd.DataFrame({
            "SEQN": flagged["SEQN"].values,
            "field": field,
            "field_label": label,
            "field_category": category,
            "value": flagged[field].values,
            "zscore": flagged[f"{field}_zscore"].values,
        }))
    if not rows:
        return pd.DataFrame(columns=["SEQN", "field", "field_label", "field_category", "value", "zscore"])
    return pd.concat(rows, ignore_index=True)


def build_metrics_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Pre-aggregated headline KPIs for Power BI cards — one row per metric,
    long format (metric / category / value_pct / numerator / denominator /
    description). NOT respondent-grain and does NOT join on SEQN; it's a
    standalone summary table like nhanes_peer_group_summary. Every clinical
    prevalence is computed on adults (18+) and on respondents who actually
    have the relevant measurement, so denominators vary per metric."""
    adults = df[df["age_years"] >= 18]
    rows = []

    def add(metric, category, mask, denom_mask, description):
        denom = int(denom_mask.sum())
        num = int((mask & denom_mask).sum())
        pct = round(100 * num / denom, 1) if denom else None
        rows.append({
            "metric": metric, "category": category, "value_pct": pct,
            "numerator": num, "denominator": denom, "description": description,
        })

    a = adults
    male = a["gender"] == "Male"

    # body composition
    has_bmi = a["bmi"].notna()
    add("Overweight or obese (BMI >= 25)", "Body", a["bmi"] >= 25, has_bmi,
        "Adults 18+ with a measured BMI at or above 25.")
    add("Obese (BMI >= 30)", "Body", a["bmi"] >= 30, has_bmi,
        "Adults 18+ with a measured BMI at or above 30.")

    # glycemic
    has_a1c = a["hba1c"].notna()
    add("Prediabetic range (HbA1c 5.7-6.4%)", "Glycemic",
        (a["hba1c"] >= 5.7) & (a["hba1c"] < 6.5), has_a1c,
        "Adults 18+ whose HbA1c falls in the prediabetic range.")
    add("Diabetic range (HbA1c >= 6.5%)", "Glycemic", a["hba1c"] >= 6.5, has_a1c,
        "Adults 18+ whose HbA1c is in the diabetic range (regardless of diagnosis).")
    add("Undiagnosed diabetes", "Care gap",
        (a["hba1c"] >= 6.5) & (a["diabetes_diagnosis"] == "No"),
        has_a1c & (a["hba1c"] >= 6.5),
        "Of adults with diabetic-range HbA1c, the share who report no diabetes diagnosis.")

    # blood pressure
    has_bp = a["mean_systolic"].notna() & a["mean_diastolic"].notna()
    htn = (a["mean_systolic"] >= 130) | (a["mean_diastolic"] >= 80)
    add("Hypertensive BP (>= 130/80)", "Cardiovascular", htn, has_bp,
        "Adults 18+ whose measured BP meets the hypertension threshold.")
    add("Undiagnosed hypertension", "Care gap",
        htn & (a["high_bp_diagnosis"] == "No"), has_bp & htn,
        "Of adults with hypertensive-range BP, the share who report no hypertension diagnosis.")

    # lipids
    has_ldl = a["ldl_cholesterol"].notna()
    add("High LDL (>= 160 mg/dL)", "Lipids", a["ldl_cholesterol"] >= 160, has_ldl,
        "Adults 18+ with measured LDL at or above 160 mg/dL.")
    add("Undiagnosed high cholesterol", "Care gap",
        (a["ldl_cholesterol"] >= 160) & (a["high_cholesterol_diagnosis"] == "No"),
        has_ldl & (a["ldl_cholesterol"] >= 160),
        "Of adults with high measured LDL, the share who report no high-cholesterol diagnosis.")

    # metabolic syndrome: >= 3 of 5 criteria, adults with all 5 measured
    ms_fields = ["waist_cm", "triglycerides", "hdl_cholesterol", "mean_systolic",
                 "mean_diastolic", "fasting_glucose"]
    ms_full = a[ms_fields].notna().all(axis=1)
    ms_count = (
        ((male & (a["waist_cm"] > 102)) | (~male & (a["waist_cm"] > 88))).astype(int)
        + (a["triglycerides"] >= 150).astype(int)
        + ((male & (a["hdl_cholesterol"] < 40)) | (~male & (a["hdl_cholesterol"] < 50))).astype(int)
        + ((a["mean_systolic"] >= 130) | (a["mean_diastolic"] >= 85)).astype(int)
        + (a["fasting_glucose"] >= 100).astype(int)
    )
    add("Metabolic syndrome (>= 3 of 5)", "Cardiovascular", ms_count >= 3, ms_full,
        "Adults 18+ with all five criteria measured who meet at least three "
        "(waist, triglycerides, HDL, BP, fasting glucose).")

    # smoking
    has_smk = a["smoking_status"].notna()
    add("Current smoker", "Lifestyle", a["smoking_status"] == "Current smoker", has_smk,
        "Adults 18+ who currently smoke (every day or some days).")
    add("Former smoker", "Lifestyle", a["smoking_status"] == "Former smoker", has_smk,
        "Adults 18+ who smoked 100+ cigarettes but no longer smoke.")

    return pd.DataFrame(rows)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Build the NHANES cardiometabolic marts.")
    parser.add_argument(
        "--force-download", action="store_true",
        help="Re-download every source .xpt from CDC even if a cached copy exists "
             "in data/raw/ (used by the manual-refresh workflow to guarantee the "
             "latest published data).",
    )
    args = parser.parse_args()

    df = build_merged_table(force=args.force_download)
    df = add_derived_fields(df)
    df = flag_anomalies(df)
    summary = build_peer_group_summary(df)
    metrics = build_metrics_summary(df)

    EXPORT_DIR.mkdir(exist_ok=True)

    # kept for backward compatibility with the existing .pbix built on
    # the single wide table — the star-schema marts below are additive.
    df.to_csv(EXPORT_DIR / "nhanes_respondents.csv", index=False)
    summary.to_csv(EXPORT_DIR / "nhanes_peer_group_summary.csv", index=False)
    metrics.to_csv(EXPORT_DIR / "metrics_summary.csv", index=False)

    marts = {
        "dim_respondents": build_dim_respondents(df),
        "fact_body_measures": build_fact_body_measures(df),
        "fact_blood_pressure": build_fact_blood_pressure(df),
        "fact_labs": build_fact_labs(df),
        "fact_diagnoses": build_fact_diagnoses(df),
        "fact_anomalies": build_fact_anomalies(df),
        "fact_care_gaps": build_fact_care_gaps(df),
    }
    for name, mart_df in marts.items():
        mart_df.to_csv(EXPORT_DIR / f"{name}.csv", index=False)

    print(f"\n{len(df):,} respondents")
    print(f"{df['is_any_anomaly'].sum():,} flagged with at least one outlier field "
          f"({df['is_any_anomaly'].mean():.1%})")
    print(f"\nMarts written to {EXPORT_DIR}/:")
    for name, mart_df in marts.items():
        print(f"  {name:<22} {len(mart_df):>6,} rows")
    print(f"  {'nhanes_respondents':<22} {len(df):>6,} rows  (legacy wide table)")
    print(f"  {'nhanes_peer_group_summary':<22} {len(summary):>6,} rows  (legacy)")
    print(f"  {'metrics_summary':<22} {len(metrics):>6,} rows  (headline KPIs)")
    print("\nDone. Get Data -> Text/CSV in Power BI, pointed at the exports/ folder.")


if __name__ == "__main__":
    main()

"""
Single-script pipeline: download NHANES 2021-2023 cardiometabolic data,
merge into one respondent-level table, flag statistical outliers, and
export CSVs for Power BI.

No database, no Codespaces — everything runs locally.

Usage:
  python scripts/build_dataset.py

Outputs (in exports/):
  nhanes_respondents.csv      one row per respondent, all fields + z-scores
  nhanes_peer_group_summary.csv   aggregated by age band + gender

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

AGE_BINS = [-1, 11, 17, 29, 44, 59, 74, 150]
AGE_LABELS = ["0-11", "12-17", "18-29", "30-44", "45-59", "60-74", "75+"]

MIN_PEER_GROUP_SIZE = 10
Z_THRESHOLD = 3


def download_file(stem: str) -> Path:
    dest = DATA_DIR / f"{stem}.xpt"
    if dest.exists():
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


def load_file(stem: str, columns: dict) -> pd.DataFrame:
    path = download_file(stem)
    df = pd.read_sas(path, format="xport")
    keep = ["SEQN"] + list(columns.keys())
    missing = [c for c in keep if c not in df.columns]
    if missing:
        raise KeyError(f"{stem}.xpt has no column(s) {missing} — check FILES against the actual XPT columns")
    df = df[keep].rename(columns=columns).copy()
    df["SEQN"] = df["SEQN"].astype(int)
    return df


def build_merged_table() -> pd.DataFrame:
    print("Downloading + merging NHANES 2021-2023 files...")
    merged = None
    for stem, columns in FILES.items():
        df = load_file(stem, columns)
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


def main():
    df = build_merged_table()
    df = add_derived_fields(df)
    df = flag_anomalies(df)
    summary = build_peer_group_summary(df)

    EXPORT_DIR.mkdir(exist_ok=True)
    respondents_out = EXPORT_DIR / "nhanes_respondents.csv"
    summary_out = EXPORT_DIR / "nhanes_peer_group_summary.csv"
    df.to_csv(respondents_out, index=False)
    summary.to_csv(summary_out, index=False)

    print(f"\n{len(df):,} respondents -> {respondents_out}")
    print(f"{df['is_any_anomaly'].sum():,} flagged with at least one outlier field "
          f"({df['is_any_anomaly'].mean():.1%})")
    print(f"{len(summary)} peer groups -> {summary_out}")
    print("\nDone. Get Data -> Text/CSV in Power BI, pointed at the exports/ folder.")


if __name__ == "__main__":
    main()

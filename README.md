# NHANES Cardiometabolic Warehouse

Real (not synthetic) health survey data from the CDC's [NHANES](https://wwwn.cdc.gov/nchs/nhanes/) August 2021-August 2023 cycle, piped straight into a single respondent-level dataset for Power BI. No database, no Codespaces — one Python script does everything locally.

## Stack

| Layer | Tool |
|---|---|
| Data source | CDC NHANES 2021-2023 cycle (file suffix `_L`) |
| Pipeline | Python (pandas, requests) — runs locally |
| Visualization | Power BI |

## What's in the data

Demographics, body measurements, blood pressure, lipid panel, glucose/insulin, and self-reported diagnosis history (diabetes, hypertension, high cholesterol, smoking, coronary heart disease, heart attack, stroke) — merged on the NHANES respondent ID (`SEQN`) from 13 source files:

| Component | Files |
|---|---|
| Demographics | `DEMO_L` |
| Examination | `BMX_L` (body measures), `BPXO_L` (blood pressure) |
| Laboratory | `TCHOL_L`, `HDL_L`, `TRIGLY_L`, `GLU_L`, `GHB_L`, `INS_L` |
| Questionnaire | `DIQ_L`, `BPQ_L`, `SMQ_L`, `MCQ_L` |

**This is unweighted.** NHANES sample weights (`WTINT2YR`, `WTMEC2YR`, `WTSAF2YR`) exist to make estimates population-representative, but they're not applied here. Nothing in these exports should be read as a national prevalence estimate — this is respondent-level outlier detection *within the sample*, not epidemiology.

## Running it

```bash
pip install -r requirements.txt
python scripts/build_dataset.py
```

This downloads the 13 NHANES `.xpt` files (cached in `data/raw/`, skipped on repeat runs), merges them on `SEQN`, decodes NHANES's numeric codes into readable labels, and computes z-scores for each respondent against their age-band + gender peer group across 11 continuous fields (BMI, waist circumference, blood pressure, cholesterol, triglycerides, glucose, HbA1c, insulin). Anything ≥3 standard deviations from its peer group mean is flagged `_is_outlier`; peer groups smaller than 10 people are left unscored rather than flagged off a noisy sample.

Outputs land in `exports/` as a star schema:

| Mart | Grain | Contents |
|---|---|---|
| **`dim_respondents.csv`** | 1 row/respondent | Demographics: age, age_band, gender, race/ethnicity, education, income-to-poverty ratio |
| **`fact_body_measures.csv`** | 1 row/respondent | Weight, height, BMI, waist circumference + z-score/outlier flag per field |
| **`fact_blood_pressure.csv`** | 1 row/respondent | Mean systolic/diastolic (averaged across up to 3 readings) + z-score/outlier flag |
| **`fact_labs.csv`** | 1 row/respondent | Total/HDL/LDL cholesterol, triglycerides, fasting glucose, HbA1c, insulin + z-score/outlier flag per field |
| **`fact_diagnoses.csv`** | 1 row/respondent | Self-reported diabetes, hypertension, high cholesterol, smoking, CHD, heart attack, stroke |
| **`fact_anomalies.csv`** | 1 row per flagged (respondent, field) | Long format: `SEQN`, `field`, `field_label`, `field_category`, `value`, `zscore` — every outlier a respondent has, one per row |

All keyed on `SEQN` (the NHANES respondent ID). `fact_anomalies` is the one genuinely one-to-many relationship — a respondent can appear multiple times if several fields are flagged; every other fact table is one-to-one with `dim_respondents`.

Two legacy files are also written for backward compatibility with an existing `.pbix` built before the marts existed — **`nhanes_respondents.csv`** (the original wide table, everything in one place) and **`nhanes_peer_group_summary.csv`** (aggregated by age band + gender). New reports should use the star schema above instead.

## Connecting Power BI

1. Run `python scripts/build_dataset.py` (above).
2. Power BI Desktop → **Get Data → Text/CSV** → import each `dim_*`/`fact_*` CSV from `exports/`.
3. In **Model view**, create relationships: `dim_respondents[SEQN]` (one) → each `fact_*[SEQN]` (many, even though body_measures/blood_pressure/labs/diagnoses happen to be 1:1 in practice — Power BI still models it as one-to-many from the dimension side). `fact_anomalies[SEQN]` is a genuine many relationship.
4. To refresh: re-run the script, then hit **Refresh** in Power BI. No Power BI Service/Pro license needed since this is a plain local CSV connection — refresh is manual (open the file, click Refresh), same limitation as the healthcare-data-warehouse project's Power BI setup.

Suggested visuals:
- Table on `fact_anomalies` joined to `dim_respondents`, sliced by `field_category`, showing `field_label`, `value`, `zscore`, `age_band`, `gender` — drill-down into exactly who's flagged and why.
- Bar chart of anomaly count by `field_category` (Body Measures / Blood Pressure / Labs) from `fact_anomalies`.
- Scatter plot of `bmi` (from `fact_body_measures`) vs. `fasting_glucose` (from `fact_labs`), colored by `diabetes_diagnosis` (from `fact_diagnoses`), to see where undiagnosed outliers cluster.
- Bar chart of `anomaly_rate` by `age_band` + `gender` from `nhanes_peer_group_summary.csv`.

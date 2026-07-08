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

Outputs land in `exports/`:

- **`nhanes_respondents.csv`** — one row per respondent: demographics, raw values, per-field z-scores/outlier flags, `anomaly_count`, `is_any_anomaly`.
- **`nhanes_peer_group_summary.csv`** — aggregated by age band + gender: mean values, respondent count, and anomaly rate per group. Good for a quick bar/matrix chart.

## Connecting Power BI

1. Run `python scripts/build_dataset.py` (above).
2. Power BI Desktop → **Get Data → Text/CSV** → select `exports/nhanes_respondents.csv` and `exports/nhanes_peer_group_summary.csv`.
3. To refresh: re-run the script, then hit **Refresh** in Power BI. No Power BI Service/Pro license needed since this is a plain local CSV connection — refresh is manual (open the file, click Refresh), same limitation as the healthcare-data-warehouse project's Power BI setup.

Suggested visuals:
- Table filtered to `is_any_anomaly = TRUE`, sorted by `anomaly_count` descending, for drill-down into specific flagged respondents.
- Scatter plot of `bmi` vs. `fasting_glucose`, colored by `diabetes_diagnosis`, to see where undiagnosed outliers cluster.
- Bar chart of `anomaly_rate` by `age_band` + `gender` from the peer-group summary.

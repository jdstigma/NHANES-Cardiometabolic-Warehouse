# NHANES Cardiometabolic Warehouse

[![Pipeline CI](https://github.com/jdstigma/NHANES-Cardiometabolic-Warehouse/actions/workflows/ci.yml/badge.svg)](https://github.com/jdstigma/NHANES-Cardiometabolic-Warehouse/actions/workflows/ci.yml)

Real (not synthetic) health survey data from the CDC's [NHANES](https://wwwn.cdc.gov/nchs/nhanes/) August 2021-August 2023 cycle, piped into a star-schema dataset for Power BI. No database, no Codespaces — one Python script builds everything locally; a notebook and CI workflow build on top of it for exploration and pipeline validation.

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

**Expect blanks — they're structural, not errors.** NHANES doesn't measure everything on everyone, so most respondents are blank in *some* domain: the fasting labs (`fasting_glucose`, `insulin`, `triglycerides`, `LDL`) come only from the ~30% morning fasting subsample; exam measures (BMI ~71%, blood pressure ~63%) exist only for mobile-exam attendees; and the adult questionnaires (diagnoses, smoking) are blank for the ~32% who are children. A blank means "not measured for this person." The `dim_respondents` coverage flags (`has_labs`, `has_fasting_labs`, `has_blood_pressure`, `is_adult`, …) let you filter to the measured population in one click, and every DAX measure below already counts only respondents who have the relevant field, so the percentages stay correct regardless of blanks.

## Running it

```bash
pip install -r requirements.txt
python scripts/build_dataset.py
```

This downloads the 13 NHANES `.xpt` files (cached in `data/raw/`, skipped on repeat runs), merges them on `SEQN`, decodes NHANES's numeric codes into readable labels, and computes z-scores for each respondent against their age-band + gender peer group across 11 continuous fields (BMI, waist circumference, blood pressure, cholesterol, triglycerides, glucose, HbA1c, insulin). Anything ≥3 standard deviations from its peer group mean is flagged `_is_outlier`; peer groups smaller than 10 people are left unscored rather than flagged off a noisy sample.

Outputs land in `exports/` as a star schema:

| Mart | Grain | Contents |
|---|---|---|
| **`dim_respondents.csv`** | 1 row/respondent | Demographics (age, age_band, gender, race/ethnicity, education, income-to-poverty ratio) + coverage flags: `is_adult`, `has_body_measures`, `has_blood_pressure`, `has_labs`, `has_fasting_labs`, `has_diagnosis_data` |
| **`fact_body_measures.csv`** | 1 row/respondent | Weight, height, BMI, waist circumference + z-score/outlier flag per field |
| **`fact_blood_pressure.csv`** | 1 row/respondent | Mean systolic/diastolic (averaged across up to 3 readings) + z-score/outlier flag |
| **`fact_labs.csv`** | 1 row/respondent | Total/HDL/LDL cholesterol, triglycerides, fasting glucose, HbA1c, insulin + z-score/outlier flag per field |
| **`fact_diagnoses.csv`** | 1 row/respondent | Self-reported diabetes, hypertension, high cholesterol, smoking (raw fields + derived `smoking_status`: Never/Former/Current), CHD, heart attack, stroke |
| **`fact_anomalies.csv`** | 1 row per flagged (respondent, field) | Long format: `SEQN`, `field`, `field_label`, `field_category`, `value`, `zscore` — every outlier a respondent has, one per row |

All keyed on `SEQN` (the NHANES respondent ID). `fact_anomalies` is the one genuinely one-to-many relationship — a respondent can appear multiple times if several fields are flagged; every other fact table is one-to-one with `dim_respondents`.

A pre-aggregated KPI table is also written — **`metrics_summary.csv`** — one row per headline metric (`metric`, `category`, `value_pct`, `numerator`, `denominator`, `description`). It's a standalone summary (not respondent-grain, doesn't join on `SEQN`), meant for KPI cards. Current values (adults 18+, denominators vary per metric since each counts only respondents with the relevant measurement):

| Metric | Value | Reading |
|---|---|---|
| Overweight or obese (BMI ≥ 25) | 71.9% | of adults with measured BMI |
| Obese (BMI ≥ 30) | 40.5% | " |
| Prediabetic HbA1c (5.7–6.4%) | 26.5% | of adults with measured HbA1c |
| Diabetic-range HbA1c (≥ 6.5%) | 12.0% | " |
| **Undiagnosed diabetes** | **16.1%** | of diabetic-range adults reporting no diagnosis |
| Hypertensive BP (≥ 130/80) | 40.3% | of adults with measured BP |
| **Undiagnosed hypertension** | **46.9%** | of hypertensive-BP adults reporting no diagnosis |
| High LDL (≥ 160 mg/dL) | 8.4% | of adults with measured LDL |
| **Undiagnosed high cholesterol** | **44.4%** | of high-LDL adults reporting no diagnosis |
| Metabolic syndrome (≥ 3 of 5) | 32.2% | of adults with all 5 criteria measured |
| Current smoker | 14.6% | of adults 18+ |
| Former smoker | 25.2% | " |

The **care-gap** metrics (bold) are the standout finding: nearly half of adults with hypertensive-range blood pressure or high LDL, and one in six with diabetic-range HbA1c, report no corresponding diagnosis. (These are exploratory, unweighted sample figures — see the note above.)

Two legacy files are also written for backward compatibility with an existing `.pbix` built before the marts existed — **`nhanes_respondents.csv`** (the original wide table, everything in one place) and **`nhanes_peer_group_summary.csv`** (aggregated by age band + gender). New reports should use the star schema above instead.

## Connecting Power BI

### Fast path — generate a ready-made Power BI project (2 steps)

`scripts/build_powerbi_project.py` generates a **PBIP** (Power BI Project) with all seven star-schema tables, the `SEQN` relationships, the headline DAX measures, and an **Overview report page with 5 starter visuals** (KPI cards for Respondents / Obesity Rate / Diabetic-Range Rate, a bar chart of every headline metric, and a bar chart of flagged anomalies by category) — so you don't have to import each CSV or build the model and report by hand:

```bash
python scripts/build_dataset.py          # 1. build exports/*.csv
python scripts/build_powerbi_project.py  # 2. generate powerbi/NHANES.pbip
# then open powerbi/NHANES.pbip in Power BI Desktop
```

Before opening, enable two Power BI Desktop **Preview features** (File → Options and settings → Options → Preview features): *"Store semantic model using TMDL format"* and *"Store reports using enhanced metadata format (PBIR)"*. The model reads the CSVs by absolute path baked in at generation time — if you move the repo, re-run step 2 (or edit the `ExportsFolder` parameter in Power Query).

> ⚠️ **Untested end-to-end.** The `.pbip` format is version-sensitive and this generator was built from Microsoft's docs without a Power BI Desktop available to confirm it loads. If Desktop reports a load error it will name the offending file — treat that as the thing to fix. The manual path below always works as a fallback.

The generated project is git-ignored (it bakes in a machine-specific path); regenerate it locally rather than committing it. The complex cross-table care-gap measures are *not* baked in (a bad measure blocks the whole model from loading) — paste those from [`powerbi/measures.md`](powerbi/measures.md) after the project opens.

### Manual path (always works)

1. Run `python scripts/build_dataset.py` (above).
2. Power BI Desktop → **Get Data → Text/CSV** → import each `dim_*`/`fact_*` CSV from `exports/`, plus `metrics_summary.csv` for KPI cards.
3. In **Model view**, create relationships: `dim_respondents[SEQN]` (one) → each `fact_*[SEQN]` (many, even though body_measures/blood_pressure/labs/diagnoses happen to be 1:1 in practice — Power BI still models it as one-to-many from the dimension side). `fact_anomalies[SEQN]` is a genuine many relationship. `metrics_summary` stands alone (no relationship — it's already aggregated).
4. Add the measures in [`powerbi/measures.md`](powerbi/measures.md) — ready-to-paste DAX for the *dynamic* versions of these metrics that recompute under demographic slicers (age band, gender, race), which the static CSV can't.
5. To refresh: re-run the script, then hit **Refresh** in Power BI. No Power BI Service/Pro license needed since this is a plain local CSV connection — refresh is manual (open the file, click Refresh), same limitation as the healthcare-data-warehouse project's Power BI setup.

Suggested visuals:
- **KPI cards** straight off `metrics_summary.csv` (filter by `category`), leading with the three care-gap metrics.
- Table on `fact_anomalies` joined to `dim_respondents`, sliced by `field_category`, showing `field_label`, `value`, `zscore`, `age_band`, `gender` — drill-down into exactly who's flagged and why.
- Bar chart of anomaly count by `field_category` (Body Measures / Blood Pressure / Labs) from `fact_anomalies`.
- Scatter plot of `bmi` (from `fact_body_measures`) vs. `fasting_glucose` (from `fact_labs`), colored by `diabetes_diagnosis` (from `fact_diagnoses`), to see where undiagnosed outliers cluster.
- Care-gap bars using the DAX measures, split by `age_band` + `gender` — this is where the dynamic measures beat the static CSV.

## Exploration notebook

`notebooks/exploration.ipynb` reads the marts in `exports/` (it doesn't re-run the pipeline) and goes beyond the Power BI report with several additional analyses:

- **Log-scale distributions** — triglycerides, insulin, and fasting glucose are heavily right-skewed, so the notebook shows each on both a linear and a **log(x) axis** (with log-spaced histogram bins), plus a log-y version of the BMI-vs-glucose scatter. The log views make the bulk of the distribution readable and roughly symmetric, and explain why the peer-group z-scores and the glucose regression have such long-tailed residuals — these biomarkers are closer to log-normal than normal.
- **Regression: glucose from body measures** — OLS of fasting glucose on BMI, waist circumference, systolic BP, and age (`statsmodels`). The residuals are a second, independent outlier lens: respondents whose glucose is unusual *given* their other measurements, not just unusual relative to their peer group.
- **Regression: blood pressure by diagnosed condition** — same idea, but diabetes/high-cholesterol diagnosis and smoking status (derived from ever-smoked + current-smoking into Never/Former/Current) go in as categorical **factors**, comparing condition groups after controlling for BMI/waist/age. First real run found current smokers run +2.4 mmHg systolic higher than never-smokers (p < 0.001) even after controlling for body measures and age; diabetes/high-cholesterol diagnosis showed no significant independent BP effect once those covariates are in the model.
- **Regression: predicting diabetes diagnosis from biomarkers** — flips the previous one around: diagnosis becomes the target (logistic regression) instead of a factor, predicted from BMI, waist, BP, age, total cholesterol, and fasting glucose, reported as odds ratios plus an ROC/AUC. First run: pseudo-R² of 0.44, fasting glucose by far the strongest predictor (unsurprising — it's diagnostic-adjacent), total cholesterol *negatively* associated (plausibly a statin-treatment effect on already-diagnosed diabetics, not a real protective effect — a good example of why these are exploratory associations, not causal claims).
- **Regression: condition burden across all three diagnosis types** — combines diabetes, hypertension, and high-cholesterol diagnosis into one 4-level "condition burden" target (0/1/2/3 conditions) and predicts it with a multinomial logistic regression, so all three diagnosis types are used together instead of picking one. First run: pseudo-R² of 0.21, with a clean dose-response pattern — age, systolic BP, and fasting glucose all become stronger predictors going from 1 → 2 → 3 conditions.
- **Clustering** — K-means (`scikit-learn`) on the fasting-subsample lab panel, with no diagnosis labels used as input. On the first real run this cleanly separated a ~790-person high-risk cluster (BMI 35.8, HbA1c 6.5%, 35% diabetes diagnosis rate) from two lower-risk clusters (~4% diagnosis rate each) — the diagnosis-rate gap is the after-the-fact sanity check that the clusters mean something clinically.
- **Lean Six Sigma process capability** — Cp, Cpk, % out of spec, DPMO, and sigma level for BMI, blood pressure, glucose, HbA1c, and cholesterol, treating each clinical reference range as a spec limit (LSL/USL). Cpk is uniformly low/negative across the board, which is expected and itself the finding: a general-population health survey isn't a controlled process centered on "healthy," unlike what Cpk normally measures in manufacturing.

Run `python scripts/build_dataset.py` first if `exports/` doesn't exist yet, then open the notebook normally in Jupyter/VS Code. All of the above was verified executing end-to-end (locally and in CI) before being committed.

**`notebooks/exploration.ipynb` is committed with its outputs already run** (not stripped), so GitHub's own notebook viewer renders every chart and table inline — no need to clone the repo or run anything to see the results. **`notebooks/exploration.html`** is a self-contained static export of the same executed notebook (via `nbconvert`) for anyone who wants a single file to open in a browser or share directly, no GitHub or Jupyter required.

Both are point-in-time snapshots, not auto-refreshed — after editing the notebook, regenerate them with:

```bash
python -m papermill notebooks/exploration.ipynb notebooks/exploration.ipynb   # re-run in place
python -m nbconvert --to html notebooks/exploration.ipynb --output exploration.html
```

## CI

`.github/workflows/ci.yml` runs the full pipeline (script + notebook + HTML export) on every push/PR and once a month via schedule, then validates the mart outputs (row counts, SEQN referential integrity) and uploads the CSVs + executed notebook + HTML as a build artifact. This is separate from the committed `exploration.ipynb`/`exploration.html` above — CI's copies are freshly regenerated on every run but only kept as a 30-day artifact, not written back to the repo. The monthly schedule isn't a data-refresh mechanism — NHANES only releases a new cycle every ~2 years — it exists to catch the CDC changing a URL or variable name independent of any code change here (this has already happened twice during development: a wrong subdomain, and a variable name case mismatch).

## Manual refresh

`.github/workflows/refresh.yml` is a manually-triggered full refresh — run it from the repo's **Actions → Manual Refresh → Run workflow** button (or `gh workflow run refresh.yml`). It differs from CI in three ways:

1. **Force-fresh source data** — runs `python scripts/build_dataset.py --force-download`, which re-pulls every NHANES `.xpt` from CDC even if a cached copy exists, so you're guaranteed to be on the latest published data (not that NHANES changes often — this is mostly a "prove it's current" button).
2. **Commits rendered outputs back** — re-runs the notebook in place and regenerates the HTML, then commits the refreshed `exploration.ipynb` + `exploration.html` back to `main` (only if they actually changed; the commit is tagged `[skip ci]` so it doesn't re-trigger the CI workflow). This keeps what GitHub renders in sync with the latest data.
3. **Uploads the CSV marts as an artifact** — since the marts are git-ignored (Power BI reads them locally), the refreshed CSVs are attached to the workflow run as a downloadable `nhanes-marts-refreshed` artifact you can pull and drop straight into Power BI.

You can also run the same refresh locally — `python scripts/build_dataset.py --force-download`, then the two regenerate commands under [Exploration notebook](#exploration-notebook) above.

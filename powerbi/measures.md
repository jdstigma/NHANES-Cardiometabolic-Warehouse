# Power BI DAX measures

> **Note:** the generated PBIP (`scripts/build_powerbi_project.py`) already bakes
> in the headline measures **and** the undiagnosed care-gap rates (the latter as
> simple single-table measures over the `fact_care_gaps` mart). This file is a
> reference for the DAX, and for the *alternative* cross-table (`TREATAS`)
> formulation if you'd rather compute the care gaps in DAX than from the
> precomputed flags. You don't need to paste anything for the default project.

Ready-to-paste DAX for the star-schema marts. Create these as **measures** (not
calculated columns) on the table noted in each heading — in Power BI Desktop:
right-click the table in the Data pane → **New measure** → paste.

These are the *dynamic* counterparts to `metrics_summary.csv`: because they're
measures over the respondent-grain fact tables, they **recompute under any
slicer** you add (age band, gender, race/ethnicity from `dim_respondents`),
whereas the CSV is a fixed adults-18+ snapshot. Use the CSV for quick headline
cards; use these when you want the number to respond to filters.

**Model setup first:** in Model view, relate `dim_respondents[SEQN]` → each
`fact_*[SEQN]` as one-to-many (single-direction, from the dimension). Every
denominator below is written to count only respondents who actually have the
relevant measurement, so blanks don't distort the rate.

---

## Base counts — table: `dim_respondents`

```dax
Respondents = COUNTROWS ( dim_respondents )
```

```dax
Adults 18+ =
CALCULATE ( [Respondents], dim_respondents[age_years] >= 18 )
```

## Body composition — table: `fact_body_measures`

```dax
BMI Measured = COUNTROWS ( FILTER ( fact_body_measures, NOT ( ISBLANK ( fact_body_measures[bmi] ) ) ) )
```

```dax
Obesity Rate =
DIVIDE (
    COUNTROWS ( FILTER ( fact_body_measures, fact_body_measures[bmi] >= 30 ) ),
    [BMI Measured]
)
```

```dax
Overweight+ Rate =
DIVIDE (
    COUNTROWS ( FILTER ( fact_body_measures, fact_body_measures[bmi] >= 25 ) ),
    [BMI Measured]
)
```

## Glycemic — table: `fact_labs`

```dax
HbA1c Measured = COUNTROWS ( FILTER ( fact_labs, NOT ( ISBLANK ( fact_labs[hba1c] ) ) ) )
```

```dax
Prediabetes Rate =
DIVIDE (
    COUNTROWS ( FILTER ( fact_labs, fact_labs[hba1c] >= 5.7 && fact_labs[hba1c] < 6.5 ) ),
    [HbA1c Measured]
)
```

```dax
Diabetic-Range Rate =
DIVIDE (
    COUNTROWS ( FILTER ( fact_labs, fact_labs[hba1c] >= 6.5 ) ),
    [HbA1c Measured]
)
```

## Care gaps (undiagnosed) — needs the diagnosis + lab/BP tables related via `dim_respondents`

`Undiagnosed diabetes` counts respondents whose HbA1c is in the diabetic range
but whose `fact_diagnoses[diabetes_diagnosis]` is "No". Because the biomarker
and the diagnosis live in different fact tables, use `TREATAS`/relationship
context — the simplest robust form filters `dim_respondents` down to the
matching SEQNs:

```dax
Diabetic-Range Respondents =
CALCULATE (
    DISTINCTCOUNT ( fact_labs[SEQN] ),
    fact_labs[hba1c] >= 6.5
)
```

```dax
Undiagnosed Diabetes =
VAR DiabeticSeqns =
    CALCULATETABLE ( VALUES ( fact_labs[SEQN] ), fact_labs[hba1c] >= 6.5 )
VAR Undiagnosed =
    CALCULATE (
        DISTINCTCOUNT ( fact_diagnoses[SEQN] ),
        fact_diagnoses[diabetes_diagnosis] = "No",
        TREATAS ( DiabeticSeqns, fact_diagnoses[SEQN] )
    )
RETURN Undiagnosed
```

```dax
Undiagnosed Diabetes Rate =
DIVIDE ( [Undiagnosed Diabetes], [Diabetic-Range Respondents] )
```

The same pattern gives undiagnosed hypertension (swap `fact_labs[hba1c] >= 6.5`
for `fact_blood_pressure[mean_systolic] >= 130 || fact_blood_pressure[mean_diastolic] >= 80`
and `diabetes_diagnosis` for `high_bp_diagnosis`) and undiagnosed high
cholesterol (`fact_labs[ldl_cholesterol] >= 160`, `high_cholesterol_diagnosis`).

## Anomalies — table: `fact_anomalies`

```dax
Flagged Anomalies = COUNTROWS ( fact_anomalies )
```

```dax
Respondents With Any Anomaly = DISTINCTCOUNT ( fact_anomalies[SEQN] )
```

```dax
Anomaly Rate =
DIVIDE ( [Respondents With Any Anomaly], [Respondents] )
```

---

## Suggested report pages

1. **Headline KPIs** — cards straight off `metrics_summary.csv` (no DAX needed):
   Obesity 40.5%, Prediabetes 26.5%, Undiagnosed hypertension 46.9%, Metabolic
   syndrome 32.2%. Add a `category` slicer.
2. **Care gaps** — the three "Undiagnosed *" measures as cards, plus a clustered
   bar of undiagnosed rate by `dim_respondents[age_band]` and `gender` (this is
   where the dynamic DAX earns its keep — the CSV can't slice like this).
3. **Anomaly drill-down** — table on `fact_anomalies` (field_label, value,
   zscore) joined to `dim_respondents`, sliced by `field_category`.
4. **Distributions** — histograms of BMI / systolic / fasting glucose from the
   fact tables; consider a log axis for the right-skewed labs (triglycerides,
   insulin, glucose), mirroring the notebook.

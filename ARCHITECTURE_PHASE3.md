# Phase 3 Architecture — Football + Basketball Prediction Brain

## Authoritative Pipeline Architecture

The Phase 3 prediction brain establishes a single authoritative prediction pipeline for both **Football** and **Basketball**:

```
DATA
  │
  ▼
VALIDATION
  │
  ▼
POINT-IN-TIME FEATURES
  │
  ▼
MODEL
  │
  ▼
RAW PROBABILITIES
  │
  ▼
PROBABILITY VALIDATION
  │
  ▼
CALIBRATION
  │
  ▼
MARKET / ODDS / EDGE / EV
  │
  ▼
UNCERTAINTY
  │
  ▼
QUALITY GATE
  │
  ▼
SIGNAL / PASS
  │
  ▼
STORAGE / PERSISTENCE
```

---

## Key Pipeline Components

### 1. Point-in-Time Feature Contract
- **Football**: Recent form, goals for, goals against, home/away splits, scoring & defensive rates, league scoring environment, Elo, H2H, and statistical enrichment.
- **Basketball**: Points scored, points allowed, home/away splits, team strength, recent form, pace/efficiency proxies.
- **Strict Leakage Prevention**: For any prediction generated at cutoff timestamp $T$, ONLY data with timestamp strictly before $T$ is used. Future fixtures, future statistics, target results, and future odds are strictly rejected.

### 2. Probability Validation Layer (`probability_validation.py`)
- Reusable validator checking every probability for:
  - Missing or `None` values
  - Booleans (rejected)
  - Non-numeric or non-finite values (`NaN`, `Inf`)
  - Bounds $[0.0, 1.0]$
  - Distribution integrity (sums close to 1.0)
  - Impossible market combinations (e.g. Totals non-monotonicity or Double Chance mismatch)
- Malformed model outputs fail closed without silent repairs.

### 3. Calibration Layer (`calibration.py`)
- Supported methods:
  - Platt / Logistic scaling
  - Multiclass Vector Platt scaling for 1X2 distributions
  - Isotonic regression (PAV algorithm)
- Walk-forward chronological boundary: Calibration is fitted strictly using outcomes prior to cutoff $T$.
- Safe fallback: If historical calibration data is insufficient (< 20 samples), raw probabilities are preserved and calibration status is set to `UNAVAILABLE`.

### 4. Market Analysis (`market_analysis.py`)
- **Bookmaker Implied Probability**: $\text{implied\_prob} = \frac{1}{\text{decimal\_odds}}$
- **Edge**: $\text{edge} = \text{calibrated\_probability} - \text{implied\_probability}$
- **Expected Value**: $\text{EV} = (\text{calibrated\_probability} \times \text{decimal\_odds}) - 1.0$
- **Missing / Stale Odds Behavior**:
  - Prediction continues to exist.
  - `edge` = `None`, `EV` = `None`, `odds_status` = `MISSING` or `STALE`.
  - Odds are an external market reference, not the model ground truth.

### 5. Uncertainty & Quality Gate (`uncertainty.py` & `quality_gate.py`)
- **Uncertainty Classification**: Deterministically assigns state:
  - `sufficient_data`
  - `limited_data`
  - `high_uncertainty`
  - `insufficient_data`
- **Authoritative Quality Gate**: Outputs `SIGNAL` or `PASS`.
- **Machine-Readable Reason Codes for PASS**:
  - `insufficient_history`
  - `insufficient_data`
  - `missing_odds`
  - `invalid_probability`
  - `high_uncertainty`
  - `insufficient_edge`
  - `insufficient_ev`
  - `stale_data`
  - `model_error`
  - `calibration_unavailable`

### 6. Versioning (`config.py`)
- `MODEL_VERSION = "v3.0.0"`
- `FEATURE_VERSION = "v3.0.0"`
- `CALIBRATION_VERSION = "v3.0.0"`
- Every generated prediction and backtest run records these version identifiers.

---

## Sport-Specific Differences

| Feature / Behavior | Football | Basketball |
|---|---|---|
| Core Model | Poisson + Dixon-Coles + Elo blend | Score averages + Normal/Erf distribution |
| Primary Markets | 1X2, Double Chance, BTTS, Over/Under (1.5, 2.5, 3.5, 4.5), Team Goals, Scoreline | Moneyline (Home/Away), Total Points (Over/Under line) |
| Multiclass Calibration | Multiclass Vector Platt scaling | Binary Platt / Isotonic scaling |
| League Parameter | `league_id` (Allow-list: 39, 140, 135, 78, 61, etc.) | `league_id` (NBA: 12) |

---

## Missing Data & Odds Behavior

1. **Missing Team Statistics**: The prediction engine generates a capability-honest result with `insufficient_data = True` and Quality Gate `PASS` (reason: `insufficient_data`). No league averages are fabricated for missing basketball teams.
2. **Missing Odds**: Prediction generation proceeds normally; `edge` and `EV` remain `None`, `odds_status` is `MISSING`. If the Quality Gate is configured to require odds, a `PASS` is issued with reason `missing_odds`.
3. **Database-First Backtests**: Backtests fetch historical fixtures strictly from persistent database storage and make ZERO live external API calls.

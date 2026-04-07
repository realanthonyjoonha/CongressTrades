# 04 — Feedback Loop: Continuous Parameter Optimization

All tunable constants (`k_XX`) ship with defaults based on reasoning, not data. The system learns from its own results.

## Step 1 — Log Everything

Every trade entering the pipeline gets a full snapshot in `trade_diagnostics`:
- All raw Dimension A/B values (actual numbers, not just pass/fail)
- Computed thresholds for that specific stock at evaluation time
- Which checks fired, which didn't
- Final verdict (open / narrowing / closed)
- **Recommended trades**: paper-trading outcome (P&L at 30, 60, 90 days)
- **Filtered trades**: retroactive hypothetical outcome computed using same entry timing and options structure

Retroactive computation on filtered trades is critical — without it the system never discovers it's filtering out winners.

## Step 2 — Weekly Parameter Review

Runs inside Agent 3 every Sunday. Computes:

1. **False negative rate**: Of trades filtered as `opportunity-expired` or `window-narrowing` that were skipped/downsized, what % would have been profitable at 60 days? If > `k_fn_threshold` (0.30) → thresholds too tight.
2. **False positive rate**: Of trades passed as `window-open`, what % lost money at 60 days where the loss was attributable to late entry? If > `k_fp_threshold` (0.40) → thresholds too loose.
3. **Per-check correlation**: For each check (A1–A4, B1–B4), correlation between that check firing and the trade being unprofitable. Weak/negative correlation → candidate for weight reduction or threshold adjustment.
4. **Sensitivity analysis**: For each `k_XX`, compute FN/FP rates at `k_XX ± 10%` and `± 20%`. Shows if parameter is in a stable zone or on a cliff edge.

## Step 3 — Propose Adjustments

Weekly report "Parameter Health" section:
- Flags constants where sensitivity analysis shows a better operating point
- Proposes specific adjustment with rationale (e.g., "k_A1: 0.60 → 0.55, FN rate drops from 35% → 24%")
- Reports FN/FP rates with trend arrows (improving / degrading / stable)
- Confidence qualifier: "low (N<20)" / "moderate (20–50)" / "high (50+)"

## Step 4 — Human Approval Gate

Parameter changes are **never auto-applied**. User approves or rejects. Approved changes logged to `parameter_changelog` with timestamp, old value, new value, rationale, and FN/FP rates at time of change. Audit trail prevents overfit drift.

## Step 5 — Quarterly Deep Recalibration

Runs inside Agent 5 (Backtest Agent) on-demand. Replays trailing 6 months through diagnostic with **grid search across all `k_XX` values simultaneously**. Catches interaction effects weekly single-parameter analysis misses (e.g., loosening k_A1 only works if k_A2 tightens). Output: "Quarterly Recalibration Report" with recommended parameter set for human approval.

## Database Tables

- `trade_diagnostics` — one row per evaluated trade. All raw values, thresholds, results, verdict, outcome (actual or retroactive). Append-only.
- `parameter_changelog` — one row per change. Timestamp, constant, old/new values, rationale, approval status, FN/FP rates. Append-only.
- `tunable_parameters` — current values of all `k_XX` constants, read by pipeline at evaluation time.

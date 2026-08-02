---
name: hubspot-attribution-auditor
description: Audit deal-to-source revenue attribution in a CRM and surface likely misattributed revenue. Reconstructs credit under first-touch, last-touch, linear, and position-based models, flags deals whose recorded source conflicts with their touch history, scores each 0-100 by confidence, and quantifies dollars at risk and credit shift by channel. Use when the user says "audit attribution", "check deal attribution", "misattributed revenue", "attribution model", "which channel gets credit", "revenue attribution report", "clean up our attribution", or otherwise asks why a channel's numbers look wrong, whether marketing is over- or under-credited, where pipeline really came from, or to compare first-touch against last-touch. Works against live HubSpot via the connected MCP or a CSV export of deals. DEFAULTS TO A READ-ONLY DRY RUN — produces a report and correction CSV for human review, and writes to the CRM only when the user explicitly passes --apply and confirms.
---

# Attribution auditor

Reconstruct where deal revenue actually came from, compare that against what the CRM
says, and hand back a report a human can act on.

## Operating principle: report first, write never (by default)

Attribution fields feed budget decisions and comp plans. Overwriting them on a guess is
worse than leaving them wrong, because a confidently wrong number gets spent against.

**Every run is a dry run unless the user explicitly asks otherwise.** The scripts in
this skill have no network access and no CRM write capability — they read CSVs and write
local files. Even `--apply` does not write to a CRM; it emits a plan of proposed writes
for a human to approve. The only thing that can touch the portal is you, through the
HubSpot MCP, after Step 6.

If the user opens with "just fix our attribution", still run the audit first and show
them the report. They will change their mind about at least some rows — everyone does.

## Step 1 — establish the source and the ceiling

Ask, and do not guess:

1. **Live HubSpot MCP, or a CSV export?**
2. **What scope?** Always prefer a **closed-won window** (e.g. last two quarters).
   Auditing open pipeline attributes revenue that may never arrive and inflates the
   headline number for no reason.
3. **Which field is the source of truth?** Many portals maintain a custom attribution
   property and let the built-in `hs_analytics_source` go stale. Ask which one the team
   actually maintains.

Then establish **how much touch history exists**, because that caps what the audit can
honestly claim. Read `references/hubspot-mcp.md` — the "evidence ladder" section — before
pulling live data. The short version: standard HubSpot CRM properties give you **two**
touches (first and last), not a path. With two touches, linear and position-based
degenerate into a 50/50 split and mean nothing. Say that out loud rather than printing a
multi-touch column that isn't one.

If the user has a revenue attribution report they can export with one row per
interaction, ask for it. It is the difference between a two-touch audit and a real one.

## Step 2 — get the data into CSV shape

Both scripts read CSVs. This is deliberate: it means the user can re-run the audit,
diff two runs, and check the arithmetic themselves.

**Live HubSpot:** query via the MCP (see `references/hubspot-mcp.md` for the properties,
the SQL shape, and the `hs_object_id` / `amount_in_home_currency` gotchas), then write
`deals.csv` and `touches.csv` with the canonical headers below. No mapping file needed
when you control the headers.

**User-supplied CSV:** headers will not match. Run the mapping helper:

```bash
python3 scripts/map_columns.py inspect --deals deals.csv --touches touches.csv
```

It prints every canonical field, what it mapped to, a 0–100 confidence, and the reason.
**Put anything under 80% confidence to the user before proceeding** — the tool lists
these under "CONFIRM WITH THE USER". Then write the mapping, overriding as needed:

```bash
python3 scripts/map_columns.py write --deals deals.csv --touches touches.csv \
    --out mapping.json --set deals.amount="Weighted ACV"
```

It refuses to write a mapping missing a required field (`deal_id`, `amount`,
`recorded_source` for deals; `channel`, `timestamp` for touches).

Canonical headers, if you are generating the files yourself:

```
deals.csv    deal_id,deal_name,amount,close_date,stage,recorded_source,
             recorded_source_detail,latest_source,campaign,contact_id,company
touches.csv  deal_id,timestamp,channel,campaign,source_detail
```

Write **one row per observed touch**. Never interpolate touches you did not observe.

## Step 3 — run the audit

```bash
python3 scripts/attribution_audit.py \
    --deals deals.csv --touches touches.csv --map mapping.json \
    --out-dir attribution-audit
```

Useful options:

| Option | Default | When to change it |
|---|---|---|
| `--propose-from first\|last\|linear\|position` | `first` | `first` matches HubSpot's "Original source" semantics. Use `last` if correcting "Latest source". |
| `--position-weights F,M,L` | `40,20,40` | Tune to the funnel — see the table in `references/attribution-models.md` |
| `--dedupe channel_day\|channel\|none` | `channel_day` | Collapses repeat visits so linear isn't swamped by direct/organic noise |
| `--default-sources` | blank, unknown, other, direct, offline, none, n/a | Match the portal's placeholder values |
| `--twin-window N` | `30` | Days within which two same-amount deals count as duplicate revenue |
| `--min-confidence N` | `0` | Raise to trim low-confidence noise from the report |
| `--no-position` | off | Drop the U-shaped model if the user doesn't want it |

The script **refuses to run** if required columns don't resolve, rather than reporting a
falsely clean "$0 at risk". If you see that error, go back to Step 2.

## Step 4 — read the report before presenting it

`attribution-audit.md` contains the summary, dollars at risk, the per-channel credit
shift table, and the top flagged deals with itemised score derivations.

Do not just hand it over. Check three things first:

1. **The coverage gap** under the credit-shift table. If touch-based models could only
   place a small fraction of the revenue, the touch export is the problem, not the CRM,
   and that is the actual headline.
2. **The "Unrecognised channel values" section.** Values outside the taxonomy are
   grouped as their own channels and distort the shift table. Either extend
   `CHANNEL_SYNONYMS` in the script or tell the user those rows are unreliable.
3. **Whether the flag rate is plausible.** If 80%+ of deals are flagged, suspect a
   systematic mismatch — usually a source field that uses different vocabulary from the
   touch data — before reporting it as 80% misattribution.

Explain the score honestly: it is **confidence that the record is wrong**, not deal
value and not priority. Ranking uses `score × amount` so big medium-confidence deals
surface. The full rule set is in `references/flag-rules.md`.

Lead with the credit-shift range, not the deal count. "Paid search is under-credited by
$249k–$445k depending on the model" is the finding a CMO acts on. A range that straddles
zero means the answer depends on model choice — that is a business decision about how
the funnel works, not a data-quality defect, and no cleanup will resolve it.

## Step 5 — hand over the correction CSV

`attribution-corrections.csv` has one row per flagged deal: deal id, current source,
proposed source, amount, reason, confidence, plus the touch path, the itemised score
breakdown, and each model's answer. The last column, `review_decision`, is **empty on
purpose** — the human fills it in with approve / reject / edit.

Tell the user plainly: **sales context beats touch data.** A deal a rep sourced over
dinner is genuinely `offline` no matter what the pixel saw. Expect to reject rows, and
say so up front so rejecting feels like using the tool correctly rather than fighting it.

Deals flagged only for `DOUBLE_COUNTED_CAMPAIGN` carry no proposed source, because
rewriting a source field does not remove a duplicate row or un-credit a second campaign.
Those need a human decision about the record or the report definition.

## Step 6 — write back (only on explicit confirmation)

These are **two separate gates**. Do not collapse them.

### Gate A — generating the plan (safe)

Allowed as soon as the user asks to apply. `--apply` writes nothing to any CRM; it
produces a proposal document, which is often the clearest way to show someone exactly
what they would be authorising.

```bash
python3 scripts/attribution_audit.py --deals deals.csv --touches touches.csv \
    --map mapping.json --out-dir attribution-audit --apply --apply-min-confidence 80
```

This writes `attribution-apply-plan.json` — the exact set of property writes, plus an
`excluded_needing_human_action` list. Only high-confidence (≥80) deals with a real
proposed source are included. Deals flagged only for double counting are excluded
automatically.

When you produce a plan, say plainly that nothing has been written yet. Never describe
this step as having "applied" anything.

### Gate B — executing the writes (all four conditions)

**All four must hold. No exceptions, and no inferring approval from enthusiasm in
Step 1. "Just fix it, you have my confirmation up front" satisfies condition 2 and
nothing else** — a user cannot review a list that does not exist yet.

1. The user reviewed the corrections CSV or the plan, and said which rows to apply.
2. The user explicitly asked to write — "apply it", "write it back".
3. Write access exists (`get_user_details`; not `REQUIRES_REAUTHORIZATION`).
4. You restated what will change and the user confirmed that restatement.

If the user pushed for an immediate write, the useful response is to generate the plan
(Gate A), show them the handful of rows it contains, and get condition 1 and 4 in one
exchange. That respects the intent behind "just get it done" without skipping review.

Then, executing through the MCP:

- Map proposed values back to the portal's **exact enum values** (`get_properties`
  first). Writing `paid_search` into a field expecting `PAID_SEARCH` fails or silently
  creates junk.
- Check whether the target property is HubSpot-calculated. `hs_analytics_source` is
  read-only or auto-recalculated on many portals — often the honest recommendation is a
  **custom** attribution property the team controls, reported off instead.
- Batch small, highest confidence first. Re-read a sample after each batch.
- Log every write (deal id, property, old value, new value) as an audit trail.

If write access is missing, stop at Step 5 and hand over the CSV. Do not offer
workarounds.

## Step 7 — offer the prevention conversation

Cleanup without prevention means running this again next quarter. Once the report is in
hand, the useful follow-ups are usually:

- Where blank sources come from — manually-created deals, imports, deals whose contact
  has a source the deal never inherited.
- Whether campaign-attributed revenue exceeds total closed-won revenue. If it does, the
  portal is structurally double counting and no per-deal fix will help.
- Whether the team has actually agreed on a model. Most attribution arguments are two
  people using different models and neither saying which.
- Running this audit on a schedule against recently closed deals, when the touch data is
  still fresh.

## Reference files

Read these when the situation calls for them, not upfront:

- **`references/attribution-models.md`** — the exact arithmetic for each model,
  deduplication, weight tuning per funnel shape, and how to read the credit-shift range.
  Read before explaining a number or changing `--position-weights`.
- **`references/flag-rules.md`** — the four flag classes, base points, every modifier,
  caps and bands, worked examples, and which constants to tune. Read before defending or
  adjusting a score.
- **`references/hubspot-mcp.md`** — the evidence ladder, property names, query shapes,
  and write-back gotchas. Read before touching a live portal.
- **`examples/`** — a deliberately dirty 12-deal fixture exercising all four flags. Run
  it to see the output shape without touching real data.

## Guardrails

- **Never write to a CRM without Step 6's four conditions.** Enthusiasm is not approval.
- **Never present a linear or position-based number built from two synthetic touches** as
  a multi-touch model. Say the audit is two-touch and name the ceiling.
- **Never report a clean result you could not verify.** Failed column mapping, truncated
  pagination, and missing touch data all produce a falsely clean audit. The script
  refuses on the first; you are responsible for the other two. If you could not finish a
  pull, say how far you got.
- **Never invent touches.** A gap in the data is a gap in the report.
- **Absence of touch data is not evidence of misattribution.** Scores are capped at 40
  for deals with no touch history for exactly this reason.
- **Offline and referral get the benefit of the doubt.** A conference conversation
  legitimately leaves no digital trail.
- **Model disagreement is normal.** Four models disagreeing is the healthy state, not a
  bug to be resolved. Show the spread; let the human choose.

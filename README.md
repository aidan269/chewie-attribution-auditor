# hubspot-attribution-auditor

A **dry-run-by-default** Claude Code skill that audits deal-to-source revenue
attribution, reconstructs credit under four different models, and tells you how much
revenue is credited to a channel the evidence doesn't support.

Works against live HubSpot via the connected MCP, or against any CSV export of deals.
It produces a report and a correction CSV. A human decides what gets written.

## The problem this exists for

Someone asks how much revenue paid search drove last quarter. You pull the number. It's
$135k. Paid search's own platform says it influenced closer to half a million.

Both numbers are "right". The CRM is reporting a single `Original source` field that was
set once, sometimes by a form, sometimes by an import, sometimes by nothing at all — and
then never revisited. The ad platform is reporting every deal it touched. Nobody is
lying, and nobody can reconcile it, so the meeting ends with "let's look into the data"
and the budget gets set on vibes.

The underlying failure is that **one field is being asked to hold an opinion it can't
express**. A deal that started with a paid click, ran through six weeks of nurture, and
closed after a demo request has one `Original source` value and three defensible answers.

This skill doesn't resolve that argument. It makes the argument legible: here is what
each model says, here is where they disagree, here is the revenue whose recorded source
no model supports at all.

## What it flags

Four classes, scored 0–100 by confidence, each with a one-line reason:

- **`UNSUPPORTED_CREDIT`** *(base 60)* — revenue credited to a channel with **no
  supporting touchpoint at all**. The recorded source appears nowhere in the history.
  The strongest flag: no judgement about ordering is required, the channel simply isn't
  there.
- **`DOUBLE_COUNTED_CAMPAIGN`** *(base 50)* — the same revenue counted twice. Either one
  deal split across multiple campaign rows each carrying the full amount, or two deal
  records for the same company, same amount, days apart, tagged to different campaigns.
- **`SOURCE_CONFLICT`** *(base 45)* — the recorded source *is* in the touch history, but
  is neither first nor last. Something mid-path got credit for the whole deal.
- **`BLANK_OR_DEFAULT_SOURCE`** *(base 35)* — source is blank, `unknown`, `other`,
  `direct`, or `offline` while real attributable touches exist. The most common finding
  in most portals, and scored lowest because a placeholder is often honest.

**Events and outbound are first-class channels, not placeholders.** "Webinar", "Trade
Show", "Cold Call", and "SDR" normalize to `events` and `outbound_sales` — specific
answers someone deliberately chose, not "we don't know." They're exempt from
`UNSUPPORTED_CREDIT` (a conference conversation leaves no digital trail) without being
treated as a missing answer. Folding them into `offline` would flag most legitimate
revenue at any org that runs webinars or staffs an SDR team.

## Four models, all transparent

First-touch, last-touch, linear, and position-based (U-shaped, default 40/20/40 and
tunable). Every model is a few lines of arithmetic over an ordered touch list — no
fitted coefficients, nothing you can't reproduce in a spreadsheet. The exact formulas
are in [`references/attribution-models.md`](references/attribution-models.md).

**An attribution number nobody can re-derive is an attribution number nobody will act
on.** That's why the report itemises every score:

```
Cyberdyne Platform — $250,000 → final 100

- +60  flag UNSUPPORTED_CREDIT
- +18  flag BLANK_OR_DEFAULT_SOURCE
- +15  rich touch history (n>=3)
- +10  first and last touch agree on a different channel
- +10  blank/default source with campaign-tagged touch present

  Touch path: paid_search/enterprise-2026 → email_marketing → paid_search
              → social_media → paid_search/enterprise-2026
```

The score answers exactly one question: **how confident are we that the recorded source
is wrong?** Deal size is deliberately excluded — a $2M deal is no more likely to be
misattributed than a $2k one. Materiality re-enters at the ranking layer, where rows are
ordered by `score × amount`.

## Example run

The repo ships a deliberately dirty 12-deal fixture that exercises all four flags. It
has messy headers on purpose, so it also demonstrates the column mapper.

```bash
# 1. See what the columns are and how they'd map
python3 scripts/map_columns.py inspect --deals examples/deals.csv --touches examples/touches.csv
```

```
CANONICAL FIELD          MAPPED TO                         CONF  WHY
----------------------------------------------------------------------------
*deal_id                 Record ID                           95  known synonym for deal_id
*amount                  Amount in company currency          95  known synonym for amount
*recorded_source         Original Source                     95  known synonym for recorded_source
 close_date              Close Date                         100  matches the canonical field name
 campaign                Campaign Name                       95  known synonym for campaign
 company                 Associated Company                  95  known synonym for company
```

Anything under 80% confidence is listed separately as **CONFIRM WITH THE USER**, and the
tool refuses to write a mapping that's missing a required field.

```bash
# 2. Save the mapping (override anything it got wrong with --set)
python3 scripts/map_columns.py write --deals examples/deals.csv \
    --touches examples/touches.csv --out mapping.json

# 3. Audit
python3 scripts/attribution_audit.py --deals examples/deals.csv \
    --touches examples/touches.csv --map mapping.json --out-dir out
```

```
DRY RUN — no CRM was contacted and nothing was written to any CRM.
  deals audited     : 12
  deals flagged     : 10
  revenue at risk   : $981,500
  report            : out/attribution-audit.md
  corrections ( 10) : out/attribution-corrections.csv
  credit shift      : out/channel-credit-shift.csv
```

The credit-shift table is the part that changes minds:

| Channel | Recorded (today) | First touch | Last touch | Linear | Position (U) | Swing vs recorded |
|---|---|---|---|---|---|---|
| direct_traffic | $48,000 | $22,500 | $310,000 | $114,583 | $135,250 | -$25,500 … +$262,000 |
| other_campaigns | $350,000 | $0 | $0 | $0 | $0 | -$350,000 |
| paid_search | $135,000 | $505,000 | $580,000 | $384,333 | $480,267 | +$249,333 … +$445,000 |
| unknown | $250,000 | $0 | $0 | $0 | $0 | -$250,000 |

Swing is a **range**, not a point estimate. Read it as:

- **Entirely negative** (`other_campaigns`, `unknown`) — over-credited today under
  *every* model. The strongest signal in the report.
- **Entirely positive** (`paid_search`) — under-credited under every model. Here paid
  search is carrying between $249k and $445k more than it's getting credit for.
- **Straddling zero** (`direct_traffic`) — the answer depends on which model you pick.
  That's a business decision about how your funnel works, not a data-quality defect, and
  no amount of cleanup will resolve it.

*(The fixture is intentionally awful — an 83% flag rate is not what a real portal looks
like. If a real run flags 80%+, suspect a vocabulary mismatch between your source field
and your touch data before believing it.)*

## Dry run is the default, and the scripts can't write anyway

The scripts have **no network access and no CRM write capability**. They read CSVs and
write local files. That's structural, not a policy.

Even `--apply` doesn't write to a CRM. It emits `attribution-apply-plan.json` — the
exact set of proposed property writes, plus an `excluded_needing_human_action` list —
for a human to approve. Writes happen only afterwards, through the HubSpot MCP, and only
when all four conditions in Step 6 of `SKILL.md` hold: the CSV was reviewed, the user
explicitly asked, write access exists, and the user confirmed a restatement of what
changes.

Deals flagged *only* for double counting are automatically excluded from the apply plan,
because rewriting a source field doesn't remove a duplicate row.

The correction CSV ships with an empty `review_decision` column. Filling it in is the
human's job, and **sales context beats touch data** — a deal a rep sourced over dinner
is genuinely `offline` no matter what the pixel saw. Expect to reject rows.

## The constraint nobody mentions: HubSpot doesn't give you a touch path

There's no CRM object API that returns "here are the eleven touches on this deal, in
order." Standard properties give you **two** points — `hs_analytics_source` and
`hs_latest_source`.

With two touches, linear and position-based **degenerate into a 50/50 split** and tell
you nothing that first/last didn't. A live-MCP-only audit is really a *two-touch audit*,
and this skill says so in the report rather than printing a multi-touch column that
isn't one.

Full multi-touch needs a revenue attribution report exported with one row per
interaction. [`references/hubspot-mcp.md`](references/hubspot-mcp.md) has the full
evidence ladder, the property names, the query shapes, and the `hs_object_id` /
`amount_in_home_currency` gotchas.

## Install

```bash
git clone https://github.com/aidan269/hubspot-attribution-auditor \
    ~/.claude/skills/hubspot-attribution-auditor
```

Claude Code picks it up on the next session. Python 3.8+, stdlib only — no pip install,
no pandas.

## Trigger phrases

- *"audit attribution"*
- *"check deal attribution"*
- *"we have misattributed revenue"*
- *"which attribution model should we use?"*
- *"which channel gets credit for this?"*
- *"build me a revenue attribution report"*
- *"clean up our attribution"*
- *"why does paid search look so weak in HubSpot?"*
- *"is marketing under-credited?"*

Or just describe the problem — "our source data is a mess and I don't trust the numbers"
routes here fine.

## Tuning it to your funnel

Every threshold is a named constant, and the report prints an **assumptions &
limitations** section reflecting the settings that run used.

| What | How |
|---|---|
| Position weights | `--position-weights 50,20,30` — long enterprise cycles want more on first touch, PLG wants more on last |
| Touch noise | `--dedupe channel_day` (default) collapses repeat visits so linear isn't swamped by direct/organic |
| Placeholder values | `--default-sources` to match your portal's vocabulary |
| Which model proposes fixes | `--propose-from first` (default, matches "Original source") |
| Duplicate-revenue window | `--twin-window 30` days |
| Flag base scores, untrackable channels, channel synonyms | Named constants at the top of `scripts/attribution_audit.py` |

Change the constants, not the report. If a rule consistently misfires for your funnel,
retune it and say so in the assumptions section — don't quietly filter the output.

## What it can't do

Stated plainly, because an audit tool that oversells itself is worse than none:

- **Attribute what was never tracked.** Dark social, word of mouth, a conversation at a
  conference. Those deals will look misattributed and aren't.
- **Distinguish influence from correlation.** Every model here counts touches. None
  measures whether a touch changed anyone's mind.
- **Apply a lookback window.** Filter your touch export first if you work a 90-day one.
- **Handle multi-buyer committees well.** A seven-person committee where only the
  champion is associated shows a thin, misleading path.
- **Settle the model argument.** Four models disagreeing is the normal, healthy state.
  The report shows the spread. A human picks.

## License

MIT — see `LICENSE`.

## Contributing

Generalisable improvements are welcome as PRs: new flag classes, new channel synonyms,
new CRM export shapes for the column mapper, corrections to the model arithmetic. Keep
scoring transparent — anything that can't be printed as an itemised breakdown doesn't
belong here.

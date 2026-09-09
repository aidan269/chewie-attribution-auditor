# Meet Chewie... 

A **dry-run-by-default** Claude Code skill that audits deal-to-source revenue
attribution, reconstructs credit under four different models, and tells you how much
revenue is credited to a channel the evidence doesn't support.

Works against live HubSpot via the connected MCP, or against any CSV export of deals.
It produces a report and a correction CSV. A human decides what gets written.

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

## Install

```bash
git clone https://github.com/aidan269/hubspot-attribution-auditor \
    ~/.claude/skills/hubspot-attribution-auditor
```

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

## Dry run is the default, and the scripts can't write anyway

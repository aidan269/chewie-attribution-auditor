# Pulling attribution data from the HubSpot MCP

How to get deals and touch history out of a live HubSpot portal, and — more importantly
— what you **cannot** get, because that determines which models the audit can honestly
run.

## Contents

- [The honest constraint: HubSpot does not hand you a touch path](#the-honest-constraint-hubspot-does-not-hand-you-a-touch-path)
- [The evidence ladder](#the-evidence-ladder)
- [Step 0 — check access](#step-0--check-access)
- [Step 1 — resolve property names before querying](#step-1--resolve-property-names-before-querying)
- [Step 2 — pull deals](#step-2--pull-deals)
- [Step 3 — pull touch evidence](#step-3--pull-touch-evidence)
- [Deal properties worth pulling](#deal-properties-worth-pulling)
- [Contact properties worth pulling](#contact-properties-worth-pulling)
- [Converting MCP results into the auditor's CSV shape](#converting-mcp-results-into-the-auditors-csv-shape)
- [Write-back](#write-back)

## The honest constraint: HubSpot does not hand you a touch path

There is no CRM object API that returns "here are the eleven marketing touches on this
deal, in order." The standard CRM properties give you **two** points — first and last —
and that is it:

- `hs_analytics_source` (+ `_data_1`, `_data_2`) — original source
- `hs_latest_source` (+ `_data_1`, `_data_2`) — latest source

**This matters more than anything else in this file.** With only first and last:

- First-touch and last-touch models work correctly.
- Linear degenerates to a 50/50 split of those two touches.
- Position-based degenerates to the same 50/50 (the `n == 2` case).
- `SOURCE_CONFLICT` can barely fire — with two touches, the recorded source is almost
  always one of them.

So a live-MCP-only audit is really a **two-touch audit**. Say so in the report. Do not
present a linear column derived from two synthetic touches as a genuine multi-touch
model — that is the exact black-box behaviour this skill exists to avoid.

Full multi-touch requires the attribution report path or a CSV export (below).

## The evidence ladder

Work down until you find something the portal actually has. Tell the user which rung you
landed on — it sets the ceiling on what the audit can claim.

| Rung | Source | Touches per deal | Models that mean anything |
|---|---|---|---|
| 1 | CSV export of a **revenue attribution report** with one row per interaction | full path | all four |
| 2 | `get_campaign_attribution_reports` | campaign-level credit, not an ordered path | double-count detection; channel shift |
| 3 | Contact timeline — form submissions, email engagement, page views | partial, uneven | first/last reliably; linear with caveats |
| 4 | `hs_analytics_source` + `hs_latest_source` only | 2 | first/last only |

Rung 4 is the default for most portals. That is fine — a two-touch audit still finds
unsupported credit, blank sources, and double counting. It just cannot rank channels
under a multi-touch model, and the report must not pretend otherwise.

## Step 0 — check access

Call `get_user_details` with tool information. Read access to DEAL and CONTACT is enough
for the entire audit. Write access is needed **only** for the optional apply step; if it
is missing or shows `REQUIRES_REAUTHORIZATION`, say so at the start of the run so nobody
expects a write-back at the end.

## Step 1 — resolve property names before querying

Portals rename things and add custom properties. Do not guess.

1. `search_properties` — find the internal names for source, campaign, and amount fields
2. `get_properties` — get the enum values for any enumeration property (source fields
   are enums, and their raw values are what you must map)

Custom attribution properties are common and often more trustworthy than the built-ins:
look for anything matching `*source*`, `*channel*`, `*attribution*`, `*campaign*`,
`*lead_source*`. Ask the user which one their team actually maintains — the built-in
`hs_analytics_source` is frequently stale in portals that adopted a custom field.

## Step 2 — pull deals

`query_crm_data` accepts SQL-like syntax. Two rules that will bite you:

- **Use `hs_object_id`, never `id`**, for record identity.
- **Use `amount_in_home_currency`**, not `amount`, for any aggregation across a
  multi-currency portal — otherwise you are summing mixed currencies.

```sql
SELECT hs_object_id, dealname, amount_in_home_currency, closedate, dealstage,
       hs_analytics_source, hs_analytics_source_data_1, hs_analytics_source_data_2,
       hs_latest_source, hs_latest_source_data_1,
       hubspot_owner_id, pipeline
FROM DEAL
WHERE dealstage = 'closedwon'
  AND closedate BETWEEN '2026-01-01' AND '2026-06-30'
```

Scope every audit to a **closed-won window**. Auditing open pipeline attributes revenue
that may never arrive, and it inflates the "dollars at risk" headline for no reason.

Join company and contact for the double-count and fallback-join logic:

```sql
SELECT hs_object_id, dealname, amount_in_home_currency, closedate,
       hs_analytics_source, COMPANY.name, COMPANY.domain
FROM DEAL
WHERE closedate BETWEEN '2026-01-01' AND '2026-06-30'
  AND associations.COMPANY IS NOT NULL
```

**Paginate to the end.** A truncated pull produces an audit that under-reports dollars
at risk while looking complete. If you cannot finish the pull, say how far you got.

## Step 3 — pull touch evidence

**Rung 2 —** `get_campaign_attribution_reports` returns campaign-level credit. Its real
value here is `DOUBLE_COUNTED_CAMPAIGN`: if the sum of campaign-attributed revenue
exceeds total closed-won revenue for the same window, the portal is double counting and
you can quantify it directly.

**Rung 3 —** pull the associated contacts and their engagement history:

```sql
SELECT hs_object_id, email,
       hs_analytics_source, hs_analytics_source_data_1,
       hs_latest_source, hs_latest_source_data_1,
       hs_analytics_first_timestamp, hs_analytics_last_timestamp,
       hs_analytics_num_visits, hs_analytics_num_page_views,
       first_conversion_event_name, first_conversion_date,
       recent_conversion_event_name, recent_conversion_date
FROM CONTACT
WHERE associations.DEAL IS NOT NULL
```

The conversion-event fields are the most useful thing here: they give two *dated*,
*named* touches per contact, and on a multi-contact deal several contacts produce
several distinct touches — which is how you climb from a two-touch audit toward a real
path without an export.

**Rung 1 —** ask the user to export a revenue attribution report with one row per
interaction (Reports → Attribution → export). This is the only path to a genuine
multi-touch audit, and it is worth asking for.

## Deal properties worth pulling

| Property | Maps to | Notes |
|---|---|---|
| `hs_object_id` | `deal_id` | Never use `id` |
| `dealname` | `deal_name` | |
| `amount_in_home_currency` | `amount` | Prefer over `amount` for multi-currency |
| `closedate` | `close_date` | |
| `dealstage` | `stage` | Scope to closed-won |
| `hs_analytics_source` | `recorded_source` | The field being audited |
| `hs_analytics_source_data_1` | `recorded_source_detail` | Campaign / keyword detail |
| `hs_latest_source` | `latest_source` | |

## Contact properties worth pulling

| Property | Use |
|---|---|
| `hs_analytics_source` / `_data_1` | First touch, when the deal's own field is blank |
| `hs_latest_source` / `_data_1` | Last touch |
| `hs_analytics_first_timestamp` | Dates the first touch so ordering works |
| `hs_analytics_last_timestamp` | Dates the last touch |
| `first_conversion_event_name` / `first_conversion_date` | A named, dated touch |
| `recent_conversion_event_name` / `recent_conversion_date` | A named, dated touch |

A deal's source is often blank while its contact's is populated — that pattern alone
generates a large share of `BLANK_OR_DEFAULT_SOURCE` findings, and it is usually the
cheapest thing for the user to fix at the workflow level.

## Converting MCP results into the auditor's CSV shape

The scripts read CSVs, so write what you pulled to `deals.csv` and `touches.csv` using
the canonical headers, then run the auditor directly — no mapping file needed when you
control the headers:

```
deals.csv    deal_id,deal_name,amount,close_date,stage,recorded_source,
             recorded_source_detail,latest_source,campaign,contact_id,company
touches.csv  deal_id,timestamp,channel,campaign,source_detail
```

Emitting real CSVs rather than modelling in your head is not busywork: it means the
user can re-run the audit themselves, diff two runs, and check the arithmetic. An
attribution number nobody can reproduce will not survive its first meeting.

When building `touches.csv` from rung 3/4 data, write **one row per known touch** and
leave the rest out. Do not interpolate touches you did not observe.

## Write-back

Only after the human has reviewed the corrections CSV, and only with explicit approval.

- Target property: `hs_analytics_source` by default (`--write-property` to change).
- Proposed values are canonical lowercase channels; HubSpot source fields are **enums**,
  so map back to the portal's exact enum values from `get_properties` before writing. A
  write of `paid_search` into a field expecting `PAID_SEARCH` will fail or, worse,
  silently create a junk value.
- `hs_analytics_source` is HubSpot-calculated on many portals and may be read-only or
  liable to be overwritten by HubSpot's own recalculation. Check before promising a
  durable fix — often the honest recommendation is to write a **custom** attribution
  property the team controls and report off that instead.
- Batch small, re-read a sample after each batch, and keep a log of every write.

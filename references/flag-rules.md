# Flag rules and confidence scoring

Four flag classes, one additive score, every number visible. Implemented as the
`flag_*` and `score_deal` functions in `scripts/attribution_audit.py`.

## Contents

- [What the score means](#what-the-score-means)
- [The four flags](#the-four-flags)
- [Base points](#base-points)
- [Modifiers](#modifiers)
- [Caps and bands](#caps-and-bands)
- [Worked examples](#worked-examples)
- [Tuning the rules](#tuning-the-rules)

## What the score means

> **Confidence that the recorded source is wrong.** Nothing else.

Specifically it is **not** a priority score, and **not** a measure of how much money is
involved. A $2M deal is no more likely to be misattributed than a $2k one, so deal size
is deliberately excluded from the arithmetic.

Materiality re-enters at the reporting layer: the top-flagged table and the corrections
CSV are ranked by `score × amount`, so a large medium-confidence deal outranks a tiny
certain one. Keeping the two separate means a big, correctly-attributed deal never gets
mistaken for a data-quality problem.

## The four flags

### `UNSUPPORTED_CREDIT` — base 60

Revenue credited to a channel with **no supporting touchpoint at all**. The recorded
source appears nowhere in the deal's touch history.

The strongest flag, because it needs no judgement about ordering — the channel simply
isn't there.

Does **not** fire when:
- there is no touch history (nothing to contradict), or
- the recorded source is `offline`, `referrals`, `events`, or `outbound_sales` — all of
  which legitimately leave no digital trail (see `UNTRACKABLE_CHANNELS`).

### `DOUBLE_COUNTED_CAMPAIGN` — base 50

The same revenue counted more than once. Two distinct shapes share the code:

**(a) Intra-deal.** The deals file has multiple rows for one deal id, each naming a
different campaign and each carrying the *full* amount. The classic campaign-attribution
export that sums to N × real revenue. Detected from the file structure alone.

**(b) Cross-deal.** Two different deal ids on the same company, same amount, close dates
within `--twin-window` days (default 30), tagged to different campaigns. Usually one
real deal entered twice so two campaigns can each claim it.

Same-campaign repeats are ignored — a customer genuinely buying the same thing twice is
a repeat sale, not double counting. Shape (b) needs a company or contact column; without
one only shape (a) is detectable.

**This flag never produces an automatic correction.** Rewriting a source property does
not remove a duplicate row or un-credit a second campaign — that is a deletion or a
report-definition change, and a human makes it. Deals flagged *only* for double counting
are excluded from the apply plan and listed under `excluded_needing_human_action`.

### `SOURCE_CONFLICT` — base 45

The recorded source **is** in the touch history, but is neither the first touch nor the
last. Something in the middle of the path got credited for the whole deal.

Deliberately narrower and lower-scored than `UNSUPPORTED_CREDIT`: the channel really did
touch this deal, so this is a disagreement about *which* touch deserves credit rather
than evidence of a broken record. Under a linear or position-based model, a
mid-path channel legitimately earns some credit — which is exactly why this scores 45
and not 60.

### `BLANK_OR_DEFAULT_SOURCE` — base 35

Original source is blank, or one of the placeholder values (`unknown`, `other`,
`direct_traffic`, `offline`, `none`, `n/a`, `null`, `-`), while real attributable
touches exist on the record.

**`events` and `outbound_sales` are deliberately NOT placeholders.** "Webinar",
"Trade Show", "Conference", "Cold Call", "SDR" are specific, intentional answers to
"where did this come from" — someone chose them. Folding them into `offline` (which
*is* a placeholder) would flag every event-sourced and outbound-sourced deal as
"origin unknown", which for any org running webinars or staffing an SDR team means
a large share of legitimate revenue flagged as broken. They still get the
`UNTRACKABLE_CHANNELS` exemption above, because a conference conversation genuinely
leaves no digital trail — they are simply untracked, not unknown.

The most common flag in most portals and the lowest-scoring, because a placeholder is
often *honest* — plenty of revenue genuinely has no trackable origin. It fires only when
the touch history contains at least one non-placeholder channel, i.e. when a better
answer demonstrably exists.

Configure the placeholder list with `--default-sources`.

## Base points

| Flag | Base | Rationale |
|---|---:|---|
| `UNSUPPORTED_CREDIT` | 60 | No judgement needed — the channel is absent |
| `DOUBLE_COUNTED_CAMPAIGN` | 50 | Structural, but sometimes a legitimate export shape |
| `SOURCE_CONFLICT` | 45 | Real disagreement, but the channel did participate |
| `BLANK_OR_DEFAULT_SOURCE` | 35 | Very common, and often honest |

**Stacking:** the highest-value flag scores in full; each additional flag adds **half**
its base. Stacked evidence should raise confidence without pinning every multi-flag deal
at 100 — otherwise the top of the report becomes a flat block of hundreds and the
ranking stops meaning anything.

## Modifiers

Applied once each, after flag points.

| Condition | Δ | Why |
|---|---:|---|
| ≥3 touches | **+15** | A rich path is strong evidence against a single wrong label |
| First and last touch agree, and differ from recorded | **+10** | One unambiguous alternative answer |
| Recorded is a placeholder and a campaign-tagged touch exists | **+10** | A demonstrably better answer is sitting right there |
| Exactly 1 touch | **−20** | One touch is thin evidence for overturning a record |
| 0 touches | **−15** | Nothing to corroborate against |
| All dated touches post-date the close | **−10** | Post-sale activity can't have sourced the deal |
| Recorded is `offline`/`referrals` and all touches are direct/organic | **−10** | Plausibly a genuine offline deal with incidental web activity |
| Any undated touches | **−5** | Ordering is unreliable, so first/last claims weaken |
| Recorded value outside the known taxonomy | **−5** | May be a valid channel this script doesn't recognise |

The last modifier is skipped for known placeholders, which are already scored by
`BLANK_OR_DEFAULT_SOURCE` and shouldn't be discounted twice.

## Caps and bands

Final score clamps to `0–100`.

**Hard cap at 40 when the deal has no touch history.** Absence of evidence is not
evidence of misattribution. Without touches only `DOUBLE_COUNTED_CAMPAIGN` can fire, and
a structural duplicate alone should never present as a high-confidence source error.

| Band | Range | Meaning |
|---|---|---|
| **high** | 80–100 | Evidence clearly contradicts the record |
| **medium** | 55–79 | Probably wrong; a human should look |
| **low** | < 55 | Worth surfacing, not worth acting on unreviewed |

Only **high** deals enter the apply plan by default (`--apply-min-confidence 80`).

## Worked examples

**Cyberdyne Platform — $250,000, recorded `unknown`, 5 touches**

```
+60  UNSUPPORTED_CREDIT     'unknown' appears in no touch
+18  BLANK_OR_DEFAULT       second flag, half of 35, rounded
+15  rich touch history     n = 5
+10  first and last agree   both paid_search, neither is 'unknown'
+10  placeholder + campaign paid_search/enterprise-2026 present
───
 113 → clamped to 100 (high)
```

**Initech Pilot — $22,500, recorded `Offline Sources`, 2 touches (direct, organic)**

```
+35  BLANK_OR_DEFAULT       'offline' is a placeholder, organic touch exists
−10  untrackable + weak     offline source, only direct/organic touches
───
  25 (low)
```

Correctly quiet. A conference-sourced deal whose contact later browsed the site is
exactly what this looks like, and the report should not push a "correction".

**Hooli Starter — $12,000, recorded `Social Media`, 1 touch (organic)**

```
+60  UNSUPPORTED_CREDIT     no social touch anywhere
−20  single touch only      thin evidence
───
  40 (low)
```

Probably wrong, but one organic pageview is not grounds for overwriting a rep's entry.

## Tuning the rules

Everything is a named constant near the top of `scripts/attribution_audit.py`:

- `FLAG_BASE_POINTS` — the four base scores
- `UNTRACKABLE_CHANNELS` — sources exempt from `UNSUPPORTED_CREDIT`. Add
  `other_campaigns` if your team logs field marketing there.
- `TRACKABLE_CHANNELS` — the always-observable set
- `DEFAULT_NULLISH_SOURCES` — placeholder values (also settable via `--default-sources`)
- `CHANNEL_SYNONYMS` — normalization map; extend it whenever the report's
  "Unrecognised channel values" section names something real

Modifier deltas live inline in `score_deal` with a comment each.

Change the constants, not the report. If a rule consistently misfires for your funnel,
the honest fix is to retune it and say so in the report's assumptions section — not to
quietly filter the output.

# Attribution models — the exact arithmetic

Every model here is a few lines of arithmetic over an ordered list of touches. There is
no machine learning, no fitted coefficient, and nothing that can't be reproduced by hand
in a spreadsheet. That is the point: an attribution number nobody can re-derive is an
attribution number nobody will act on.

Implemented in `scripts/attribution_audit.py` as `credit_*` functions.

## Contents

- [Notation](#notation)
- [Touch deduplication (happens first)](#touch-deduplication-happens-first)
- [Recorded](#recorded)
- [First touch](#first-touch)
- [Last touch](#last-touch)
- [Linear](#linear)
- [Position-based (U-shaped)](#position-based-u-shaped)
- [Which model to propose corrections from](#which-model-to-propose-corrections-from)
- [Reading the credit-shift table](#reading-the-credit-shift-table)
- [What none of these models can do](#what-none-of-these-models-can-do)

## Notation

For one deal:

- `A` — deal amount
- `t₁ … tₙ` — the deal's touches, sorted ascending by timestamp
- `channel(tᵢ)` — the normalized channel of touch i

Each model returns a map of `{channel: dollars}` summing to `A` (or to `0` when the
model has no touches to work with). Channel totals in the report are the sum of these
maps across every deal.

Undated touches sort last and are counted, but they weaken first/last ordering, so the
score carries a `-5` penalty per deal that has any.

## Touch deduplication (happens first)

Raw touch logs are noisy. Someone who visits your pricing page nine times in a week
generated nine `direct_traffic` rows, and under a naive linear model that one person
drowns out the paid click that started everything.

Default mode `channel_day` collapses touches sharing `(channel, campaign, calendar day)`
into one. `channel` collapses to one touch per `(channel, campaign)` across the entire
history. `none` uses the log as-is.

This choice materially changes the linear and position-based numbers and does **not**
change first-touch or last-touch. Say which mode you used whenever you quote a linear
figure.

## Recorded

The baseline — what the CRM believes today.

```
credit = { recorded_source: A }
```

100% to the source field, whatever it says, including blank. This is not an attribution
model; it is the thing being audited. Every other column is compared against it.

## First touch

```
credit = { channel(t₁): A }
```

100% to the channel that opened the relationship. Matches the semantics of HubSpot's
**Original source** property, which is why it is the default for `--propose-from`.

Answers: *what created this opportunity?* Biased toward top-of-funnel — brand, content,
and paid acquisition look strong; nurture and sales-assist look worthless.

## Last touch

```
credit = { channel(tₙ): A }
```

100% to the final touch before close. Matches **Latest source**.

Answers: *what closed this?* Biased toward bottom-of-funnel — branded search, direct,
and email all look strong because those are what people do once they've already decided.
Last touch is the model most likely to flatter channels that did the least work.

## Linear

```
share = A / n
credit[channel(tᵢ)] += share    for every i
```

Every touch counts equally. Answers: *what participated?* Its weakness is that it can't
distinguish a decisive first click from an incidental fourth pageview, so a channel wins
by volume rather than by influence — which is exactly why deduplication runs first.

## Position-based (U-shaped)

Default weights `40 / 20 / 40` (first / middle / last), configurable via
`--position-weights`. Weights are normalized, so `30,40,30` and `3,4,3` behave
identically.

```
n == 1:  credit[channel(t₁)] = A

n == 2:  credit[channel(t₁)] += A × first_w / (first_w + last_w)
         credit[channel(t₂)] += A × last_w  / (first_w + last_w)
         (with defaults this is a 50/50 split, not 40/40)

n >= 3:  credit[channel(t₁)] += A × first_w
         credit[channel(tₙ)] += A × last_w
         credit[channel(tᵢ)] += A × mid_w / (n − 2)   for 1 < i < n
```

The compromise position: the touch that created the opportunity and the touch that
closed it did most of the work, and everything in between kept it alive.

**Tuning to your funnel** — this is the one number worth arguing about internally:

| Funnel shape | Suggested weights | Why |
|---|---|---|
| Long enterprise, SDR-opened | `50,20,30` | The open is the hard part |
| PLG / self-serve | `30,20,50` | The final trigger is the hard part |
| Heavy nurture, long consideration | `35,30,35` | The middle genuinely does work |
| Short transactional | `40,20,40` (default) | No strong reason to skew |

## Which model to propose corrections from

`--propose-from` selects the source of proposed corrections. Default `first`.

| Value | Proposes | Use when |
|---|---|---|
| `first` | first-touch channel | Correcting **Original source** — the usual case |
| `last` | last-touch channel | Correcting **Latest source** |
| `linear` | channel with the largest linear share | You want the channel that participated most |
| `position` | channel with the largest U-shaped share | You have a tuned position model you trust |

Two guards apply regardless of model:

1. A proposal identical to the current value is dropped — no churn for no change.
2. A model whose winner is itself a placeholder (`direct_traffic`, `offline`, `unknown`)
   falls back to the best *real* channel in the history, and proposes nothing if there
   isn't one. Swapping `offline` for `direct_traffic` trades one non-answer for another.

## Reading the credit-shift table

The swing column is a **range** — the least and the most a channel gains or loses across
the non-recorded models.

- **Range entirely negative** — over-credited today under every model. The strongest
  signal in the report.
- **Range entirely positive** — under-credited today under every model.
- **Range straddling zero** — the answer depends on which model you pick. That is a
  business decision about how your funnel works, not a data-quality defect, and no
  amount of cleanup will resolve it.

Watch the coverage-gap note beneath the table. Deals with no usable touch history appear
in the recorded column and nowhere else, so the model columns legitimately sum to less
than total revenue. A large gap means the touch export, not the CRM, is the problem.

## What none of these models can do

- **Attribute what was never tracked.** Dark social, word of mouth, a conversation at a
  conference, a podcast mention. These deals look misattributed and are not.
- **Distinguish influence from correlation.** Every model here counts touches. None
  measures whether a touch changed anyone's mind.
- **Apply a lookback window.** Every touch on the record is used, however old. If your
  team works a 90-day window, filter the touch export before running.
- **Handle multi-buyer committees.** Touches are joined by deal, falling back to a
  single contact. A seven-person buying committee where only the champion is associated
  will show a thin, misleading path.
- **Settle the model argument.** Four models disagreeing is the normal, healthy state.
  The report shows the spread so a human can choose; it does not choose.

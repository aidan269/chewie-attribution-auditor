#!/usr/bin/env python3
"""
attribution_audit.py — reconstruct deal revenue attribution under multiple models,
flag likely misattribution, and emit a review-first audit report.

READ-ONLY BY CONSTRUCTION. This script never contacts a CRM and has no network or
write capability. It reads CSVs and writes local files. The `--apply` flag does NOT
write to any CRM; it only emits an explicit apply-plan JSON that a human reviews and
an agent may then execute through the HubSpot MCP. See SKILL.md.

Stdlib only. Python 3.8+.

Usage
-----
  attribution_audit.py --deals deals.csv [--touches touches.csv] [options]

Inputs
------
deals.csv    one row per deal (or one row per deal-campaign pair; see
             DOUBLE_COUNTED_CAMPAIGN). Canonical fields:
               deal_id, deal_name, amount, close_date, stage,
               recorded_source, recorded_source_detail, latest_source,
               campaign, contact_id, company
touches.csv  optional, one row per marketing touch. Canonical fields:
               deal_id (or contact_id), timestamp, channel, campaign, source_detail

Column names rarely match. Run scripts/map_columns.py first to produce a mapping
JSON, then pass it with --map.

Outputs (written to --out-dir, default ./attribution-audit)
-----------------------------------------------------------
  attribution-audit.md          the human report
  attribution-corrections.csv   deal-level proposed corrections for human review
  channel-credit-shift.csv      per-channel dollars under every model
  attribution-apply-plan.json   ONLY when --apply is passed
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta

# --------------------------------------------------------------------------------------
# Canonical channels
# --------------------------------------------------------------------------------------
# HubSpot's own original-source taxonomy, lowercased and snake_cased. Everything is
# normalized into this set so that a deal recorded as "Paid Search" and a touch logged
# as "cpc" are recognised as the same channel. Unknown values pass through normalized
# but unmapped, and are reported verbatim so nothing is silently swallowed.

CANONICAL_CHANNELS = [
    "organic_search",
    "paid_search",
    "paid_social",
    "social_media",
    "email_marketing",
    "referrals",
    "other_campaigns",
    "events",
    "outbound_sales",
    "direct_traffic",
    "offline",
]

CHANNEL_SYNONYMS = {
    "organic_search": [
        "organic", "organic search", "seo", "natural search", "google organic",
    ],
    "paid_search": [
        "paid search", "cpc", "ppc", "sem", "adwords", "google ads", "bing ads",
        "search ads", "paid_search_ads",
    ],
    "paid_social": [
        "paid social", "social ads", "linkedin ads", "facebook ads", "meta ads",
        "twitter ads", "x ads", "social paid",
    ],
    "social_media": [
        "social", "social media", "organic social", "linkedin", "twitter", "facebook",
        "instagram", "youtube",
    ],
    "email_marketing": [
        "email", "email marketing", "newsletter", "nurture", "drip", "marketing email",
        "eloqua", "marketo email",
    ],
    "referrals": [
        "referral", "referrals", "referral traffic", "partner", "partner referral",
        "word of mouth", "customer referral",
    ],
    "other_campaigns": [
        "other campaigns", "other campaign", "campaign", "display", "banner",
        "affiliate", "content syndication", "review site", "g2", "capterra",
    ],
    "direct_traffic": [
        "direct", "direct traffic", "web direct", "direct web", "none", "(none)",
        "typed/bookmarked", "typed or bookmarked", "bookmark",
    ],
    # Events and outbound are REAL, deliberately-chosen channels — not placeholders.
    # They were originally folded into `offline`, which is in DEFAULT_NULLISH_SOURCES,
    # so every event-sourced deal was flagged as "we don't know where this came from".
    # For any org that runs webinars or has an SDR team that is a false-positive
    # factory. They keep the UNTRACKABLE exemption (a conference conversation really
    # does leave no digital trail) without being treated as a missing answer.
    "events": [
        "event", "events", "webinar", "conference", "trade show", "tradeshow",
        "field marketing", "summit", "expo", "roadshow", "meetup", "seminar",
        "field event", "sponsorship",
    ],
    "outbound_sales": [
        "outbound", "cold call", "cold calling", "cold email", "sales outreach",
        "outbound email", "prospecting", "bdr", "sdr", "sales prospecting",
        "outbound sales", "sales generated",
    ],
    # Reserved for genuinely opaque origins — the record exists but nobody knows why.
    "offline": [
        "offline", "offline sources", "offline source", "list import", "import",
        "manual", "manual entry", "data import", "csv import", "bulk import",
    ],
}

# Values that mean "we do not actually know where this came from". A deal carrying one
# of these while real touches exist is the BLANK_OR_DEFAULT_SOURCE flag.
DEFAULT_NULLISH_SOURCES = {
    "", "unknown", "other", "n/a", "na", "none", "null", "-",
    "direct_traffic", "offline",
}

# Channels that plausibly leave no digital touch trail. Used only to REDUCE confidence:
# an offline-sourced deal with only direct/organic touches may be correctly recorded.
# A conference conversation, a partner intro, and an SDR cold call are all real origins
# that the tracking stack genuinely cannot see.
UNTRACKABLE_CHANNELS = {"offline", "referrals", "events", "outbound_sales"}

# Channels that are always digitally observable. Credit to one of these with zero
# supporting touches is a strong misattribution signal.
TRACKABLE_CHANNELS = {
    "organic_search", "paid_search", "paid_social", "social_media",
    "email_marketing", "other_campaigns",
}

MODELS = ["recorded", "first_touch", "last_touch", "linear", "position_based"]


# --------------------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------------------

def norm_channel(raw):
    """Normalize an arbitrary source/channel string to a canonical channel.

    Returns (canonical, was_mapped). Unmapped values are returned snake_cased so they
    still group consistently, with was_mapped=False so the report can surface them.
    """
    if raw is None:
        return "", False
    s = str(raw).strip().lower()
    if not s:
        return "", False
    s = re.sub(r"[\s\-/]+", "_", s)
    s = re.sub(r"[^a-z0-9_()]+", "", s)
    if s in CANONICAL_CHANNELS:
        return s, True
    spaced = s.replace("_", " ")
    for canonical, variants in CHANNEL_SYNONYMS.items():
        if spaced in variants or s in [v.replace(" ", "_") for v in variants]:
            return canonical, True
    return s, False


def parse_amount(raw):
    """Parse a currency-ish string into a float. Returns None if unparseable.

    Handles: $1,234.56  |  1234.56  |  (1,234.56) negative  |  USD 1234  |  1 234,56 is
    NOT handled (see 'Assumptions & limitations' — European decimal commas need
    pre-conversion).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    s = re.sub(r"[^0-9.\-]", "", s)
    if s in ("", "-", "."):
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if negative else val


DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
    "%m/%d/%Y %H:%M", "%m/%d/%y", "%b %d, %Y", "%d %b %Y",
]


def parse_date(raw):
    """Parse a date/timestamp into a datetime. Returns None if unparseable.

    Ambiguity note: %m/%d/%Y is tried before %d/%m/%Y, so 03/04/2026 reads as March 4.
    Pass ISO dates to avoid this.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if re.match(r"^\d{13}$", s):          # epoch millis (HubSpot exports)
        try:
            return datetime.utcfromtimestamp(int(s) / 1000.0)
        except (ValueError, OSError):
            return None
    if re.match(r"^\d{10}$", s):          # epoch seconds
        try:
            return datetime.utcfromtimestamp(int(s))
        except (ValueError, OSError):
            return None
    s2 = s.replace("+0000", "").replace("Z", "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s2, fmt)
        except ValueError:
            continue
    # Last resort: leading ISO date inside a longer string
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s2)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            return None
    return None


def money(x):
    if x is None:
        return "—"
    sign = "-" if x < 0 else ""
    return "{}${:,.0f}".format(sign, abs(x))


def signed_money(x):
    return "{}{}".format("+" if x > 0 else "", money(x))


def plural(n, one, many):
    return "{} {}".format(n, one if n == 1 else many)


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------

DEAL_FIELDS = [
    "deal_id", "deal_name", "amount", "close_date", "stage",
    "recorded_source", "recorded_source_detail", "latest_source",
    "campaign", "contact_id", "company",
]
TOUCH_FIELDS = ["deal_id", "contact_id", "timestamp", "channel", "campaign", "source_detail"]


def load_mapping(path):
    if not path:
        return {}
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    # Accept either {"deals": {...}, "touches": {...}} or a flat {canonical: header}
    if "deals" in data or "touches" in data:
        return data
    return {"deals": data, "touches": {}}


def read_csv_rows(path, mapping, fields):
    """Read a CSV, remapping headers to canonical field names.

    mapping is {canonical_field: source_header}. Unmapped canonical fields resolve by
    exact (case/space-insensitive) header match, then are left empty.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        lookup = {}
        norm_headers = {re.sub(r"[^a-z0-9]", "", (h or "").lower()): h for h in headers}
        for field in fields:
            src = mapping.get(field)
            if src and src in headers:
                lookup[field] = src
            else:
                key = re.sub(r"[^a-z0-9]", "", field.lower())
                if key in norm_headers:
                    lookup[field] = norm_headers[key]
        rows = []
        for raw in reader:
            row = {f: (raw.get(lookup[f]) if f in lookup else None) for f in fields}
            row["_raw"] = raw
            rows.append(row)
        return rows, headers


def check_resolution(rows, required, label, path, headers):
    """Refuse to audit a file whose key columns did not resolve.

    The failure this prevents: headers that don't match anything resolve to empty for
    every row, the audit finds nothing to contradict, and the report cheerfully says
    "0 deals flagged, $0 at risk". A clean bill of health produced by a parsing failure
    is worse than a crash, because someone will believe it.
    """
    problems = []
    for field in required:
        filled = sum(1 for r in rows if (r.get(field) or "").strip())
        if filled == 0:
            problems.append("  {:<18} resolved for 0 of {} rows".format(field, len(rows)))
        elif filled < len(rows) * 0.5:
            problems.append("  {:<18} resolved for only {} of {} rows".format(
                field, filled, len(rows)))
    if problems:
        raise SystemExit(
            "\nERROR: required columns in {} ({}) could not be read.\n{}\n\n"
            "Columns found in the file:\n  {}\n\n"
            "Fix this by building a mapping first:\n"
            "  map_columns.py write --deals {} --out mapping.json\n"
            "then re-run with --map mapping.json\n\n"
            "Refusing to continue: an audit that cannot read its key columns reports "
            "'nothing wrong' for the wrong reason.".format(
                label, path, "\n".join(problems),
                "\n  ".join(headers) if headers else "(none)", path))


def load_deals(path, mapping):
    rows, headers = read_csv_rows(path, mapping.get("deals", {}), DEAL_FIELDS)
    check_resolution(rows, ["deal_id", "amount", "recorded_source"], "deals", path, headers)
    deals = []
    for r in rows:
        chan, mapped = norm_channel(r.get("recorded_source"))
        latest, _ = norm_channel(r.get("latest_source"))
        deals.append({
            "deal_id": (r.get("deal_id") or "").strip(),
            "deal_name": (r.get("deal_name") or "").strip(),
            "amount": parse_amount(r.get("amount")),
            "amount_raw": r.get("amount"),
            "close_date": parse_date(r.get("close_date")),
            "stage": (r.get("stage") or "").strip(),
            "recorded_source_raw": (r.get("recorded_source") or "").strip(),
            "recorded_source": chan,
            "recorded_source_mapped": mapped,
            "recorded_source_detail": (r.get("recorded_source_detail") or "").strip(),
            "latest_source": latest,
            "campaign": (r.get("campaign") or "").strip(),
            "contact_id": (r.get("contact_id") or "").strip(),
            "company": (r.get("company") or "").strip(),
        })
    return deals, headers


def load_touches(path, mapping):
    if not path:
        return [], []
    rows, headers = read_csv_rows(path, mapping.get("touches", {}), TOUCH_FIELDS)
    check_resolution(rows, ["channel", "timestamp"], "touches", path, headers)
    touches = []
    for r in rows:
        chan, mapped = norm_channel(r.get("channel"))
        touches.append({
            "deal_id": (r.get("deal_id") or "").strip(),
            "contact_id": (r.get("contact_id") or "").strip(),
            "timestamp": parse_date(r.get("timestamp")),
            "channel_raw": (r.get("channel") or "").strip(),
            "channel": chan,
            "channel_mapped": mapped,
            "campaign": (r.get("campaign") or "").strip(),
            "source_detail": (r.get("source_detail") or "").strip(),
        })
    return touches, headers


# --------------------------------------------------------------------------------------
# Deal / touch assembly
# --------------------------------------------------------------------------------------

def collapse_deal_rows(deals):
    """Collapse multiple rows sharing a deal_id into one deal, remembering the rows.

    Multi-row deals are how campaign-attribution exports represent one deal credited to
    several campaigns. Whether that constitutes double counting is decided later, in
    flag_double_counted_campaign.
    """
    grouped = OrderedDict()
    for d in deals:
        key = d["deal_id"] or "(missing-id)#{}".format(len(grouped))
        if key not in grouped:
            merged = dict(d)
            merged["rows"] = [d]
            grouped[key] = merged
        else:
            grouped[key]["rows"].append(d)
            # Keep the first non-empty value for each descriptive field.
            for f in ("deal_name", "recorded_source", "recorded_source_raw", "stage",
                      "contact_id", "company", "latest_source"):
                if not grouped[key].get(f) and d.get(f):
                    grouped[key][f] = d[f]
            if grouped[key].get("amount") is None:
                grouped[key]["amount"] = d.get("amount")
            if grouped[key].get("close_date") is None:
                grouped[key]["close_date"] = d.get("close_date")
    return list(grouped.values())


def attach_touches(deals, touches, dedupe="channel_day"):
    """Attach touches to deals by deal_id, falling back to contact_id."""
    by_deal = defaultdict(list)
    by_contact = defaultdict(list)
    for t in touches:
        if t["deal_id"]:
            by_deal[t["deal_id"]].append(t)
        elif t["contact_id"]:
            by_contact[t["contact_id"]].append(t)

    for d in deals:
        ts = list(by_deal.get(d["deal_id"], []))
        if not ts and d["contact_id"]:
            ts = list(by_contact.get(d["contact_id"], []))
        ts = [t for t in ts if t["channel"]]
        # Undated touches cannot be ordered; keep them but sort them last, and record
        # the fact so the report can caveat first/last-touch for that deal.
        d["undated_touches"] = sum(1 for t in ts if t["timestamp"] is None)
        ts.sort(key=lambda t: (t["timestamp"] is None, t["timestamp"] or datetime.max))
        d["touches"] = dedupe_touches(ts, dedupe)
        d["touches_raw_count"] = len(ts)
    return deals


def dedupe_touches(touches, mode):
    """Collapse repeated touches so the linear model is not swamped by noise.

    channel_day (default): one touch per (channel, campaign, calendar day).
    none:                  keep every touch as logged.
    channel:               one touch per (channel, campaign) for the whole history.
    """
    if mode == "none" or not touches:
        return touches
    seen = set()
    out = []
    for t in touches:
        if mode == "channel":
            key = (t["channel"], t["campaign"])
        else:
            day = t["timestamp"].date().isoformat() if t["timestamp"] else "undated"
            key = (t["channel"], t["campaign"], day)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


# --------------------------------------------------------------------------------------
# Attribution models — all arithmetic, all inspectable
# --------------------------------------------------------------------------------------

def credit_recorded(deal):
    """Whatever the CRM currently says: 100% of the amount to the recorded source."""
    amt = deal["amount"] or 0.0
    src = deal["recorded_source"] or "(blank)"
    return {src: amt}


def credit_first_touch(deal):
    ts = deal["touches"]
    if not ts:
        return {}
    return {ts[0]["channel"]: (deal["amount"] or 0.0)}


def credit_last_touch(deal):
    ts = deal["touches"]
    if not ts:
        return {}
    return {ts[-1]["channel"]: (deal["amount"] or 0.0)}


def credit_linear(deal):
    """Equal split across every (deduped) touch."""
    ts = deal["touches"]
    if not ts:
        return {}
    amt = deal["amount"] or 0.0
    share = amt / float(len(ts))
    out = defaultdict(float)
    for t in ts:
        out[t["channel"]] += share
    return dict(out)


def credit_position_based(deal, weights=(0.40, 0.20, 0.40)):
    """U-shaped: first_w to the first touch, last_w to the last, middle_w spread evenly.

    n == 1 -> 100% to the single touch.
    n == 2 -> first_w and last_w renormalized to sum to 1 (40/40 becomes 50/50).
    n >= 3 -> first_w / middle_w / last_w exactly as configured.
    """
    ts = deal["touches"]
    if not ts:
        return {}
    amt = deal["amount"] or 0.0
    first_w, mid_w, last_w = weights
    out = defaultdict(float)
    n = len(ts)
    if n == 1:
        out[ts[0]["channel"]] += amt
    elif n == 2:
        total = first_w + last_w
        out[ts[0]["channel"]] += amt * (first_w / total)
        out[ts[-1]["channel"]] += amt * (last_w / total)
    else:
        out[ts[0]["channel"]] += amt * first_w
        out[ts[-1]["channel"]] += amt * last_w
        per_mid = (amt * mid_w) / float(n - 2)
        for t in ts[1:-1]:
            out[t["channel"]] += per_mid
    return dict(out)


def all_model_credits(deal, position_weights, enable_position=True):
    credits = {
        "recorded": credit_recorded(deal),
        "first_touch": credit_first_touch(deal),
        "last_touch": credit_last_touch(deal),
        "linear": credit_linear(deal),
    }
    credits["position_based"] = (
        credit_position_based(deal, position_weights) if enable_position else {}
    )
    return credits


def dominant_channel(credit_map):
    """The channel holding the largest share under a model. Ties broken alphabetically
    so results are deterministic across runs."""
    if not credit_map:
        return ""
    return sorted(credit_map.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


# --------------------------------------------------------------------------------------
# Flag rules
# --------------------------------------------------------------------------------------
# Each rule returns None or a dict: {"code", "points", "detail"}.
# Points are the rule's BASE contribution to the confidence score. Modifiers are applied
# afterwards in score_deal(). Every number here is documented in references/flag-rules.md.

FLAG_BASE_POINTS = {
    "UNSUPPORTED_CREDIT": 60,
    "DOUBLE_COUNTED_CAMPAIGN": 50,
    "SOURCE_CONFLICT": 45,
    "BLANK_OR_DEFAULT_SOURCE": 35,
}


def flag_unsupported_credit(deal):
    """Amount credited to a channel that appears nowhere in the touch history."""
    ts = deal["touches"]
    if not ts:
        return None
    rec = deal["recorded_source"]
    if not rec:
        return None
    if rec in UNTRACKABLE_CHANNELS:
        return None  # offline/referral legitimately leaves no digital trail
    if rec in {t["channel"] for t in ts}:
        return None
    return {
        "code": "UNSUPPORTED_CREDIT",
        "points": FLAG_BASE_POINTS["UNSUPPORTED_CREDIT"],
        "detail": "recorded '{}' has zero supporting touchpoints across {} touch(es)".format(
            rec, len(ts)),
    }


def flag_source_conflict(deal):
    """Recorded source exists in the history but contradicts both bookends.

    Deliberately narrower than UNSUPPORTED_CREDIT: the channel did touch the deal, it
    just is not the one either first-touch or last-touch would credit.
    """
    ts = deal["touches"]
    if not ts or not deal["recorded_source"]:
        return None
    rec = deal["recorded_source"]
    first, last = ts[0]["channel"], ts[-1]["channel"]
    if rec in (first, last):
        return None
    if rec not in {t["channel"] for t in ts}:
        return None  # that is UNSUPPORTED_CREDIT, not a conflict
    return {
        "code": "SOURCE_CONFLICT",
        "points": FLAG_BASE_POINTS["SOURCE_CONFLICT"],
        "detail": "recorded '{}' is neither first-touch '{}' nor last-touch '{}'".format(
            rec, first, last),
    }


def flag_blank_or_default_source(deal, default_sources):
    """Original source is blank / direct / offline / unknown while real touches exist."""
    ts = deal["touches"]
    rec = deal["recorded_source"]
    if rec not in default_sources:
        return None
    real = [t for t in ts if t["channel"] not in default_sources]
    if not real:
        return None
    return {
        "code": "BLANK_OR_DEFAULT_SOURCE",
        "points": FLAG_BASE_POINTS["BLANK_OR_DEFAULT_SOURCE"],
        "detail": "recorded '{}' but {} attributable touch(es) exist ({})".format(
            rec or "(blank)", len(real),
            ", ".join(sorted({t["channel"] for t in real}))),
    }


def flag_double_counted_campaign(deal, revenue_twins):
    """The same revenue credited more than once.

    Two distinct shapes, both reported under this code:

    (a) Intra-deal: the deals file contains multiple rows for one deal_id, each naming a
        different campaign and each carrying the FULL deal amount. That is the classic
        campaign-attribution export that sums to N x the real revenue.

    (b) Cross-deal: two different deal_ids on the same company with the same amount and
        close dates within --twin-window days, tagged to different campaigns. Usually one
        real deal recorded twice so two campaigns can each claim it.
    """
    rows = deal.get("rows", [])
    if len(rows) > 1:
        campaigns = {r["campaign"] for r in rows if r["campaign"]}
        amounts = [r["amount"] for r in rows if r["amount"] is not None]
        full = deal["amount"]
        if len(campaigns) > 1 and full and amounts and all(
                abs(a - full) < 0.01 for a in amounts):
            return {
                "code": "DOUBLE_COUNTED_CAMPAIGN",
                "points": FLAG_BASE_POINTS["DOUBLE_COUNTED_CAMPAIGN"],
                "detail": "{} rows credit the full {} to {} different campaigns ({})".format(
                    len(rows), money(full), len(campaigns),
                    ", ".join(sorted(campaigns))),
            }
    twin = revenue_twins.get(deal["deal_id"])
    if twin:
        return {
            "code": "DOUBLE_COUNTED_CAMPAIGN",
            "points": FLAG_BASE_POINTS["DOUBLE_COUNTED_CAMPAIGN"],
            "detail": "same {} on '{}' also recorded as deal {} under campaign '{}'".format(
                money(deal["amount"]), deal["company"] or "(no company)",
                twin["deal_id"], twin["campaign"] or "(none)"),
        }
    return None


def find_revenue_twins(deals, window_days=30):
    """Cross-deal duplicate revenue detection. Requires a company (or contact) column."""
    twins = {}
    buckets = defaultdict(list)
    for d in deals:
        key_owner = (d["company"] or d["contact_id"] or "").strip().lower()
        if not key_owner or d["amount"] is None or d["amount"] == 0:
            continue
        buckets[(key_owner, round(d["amount"], 2))].append(d)
    for _, group in buckets.items():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a["deal_id"] == b["deal_id"]:
                    continue
                if (a["campaign"] or "") == (b["campaign"] or ""):
                    continue  # same campaign: a genuine repeat sale, not double counting
                if a["close_date"] and b["close_date"]:
                    if abs((a["close_date"] - b["close_date"]).days) > window_days:
                        continue
                twins.setdefault(a["deal_id"], b)
                twins.setdefault(b["deal_id"], a)
    return twins


# --------------------------------------------------------------------------------------
# Scoring — additive, capped, and fully itemised
# --------------------------------------------------------------------------------------
# The score answers exactly one question: "how confident are we that the recorded source
# is wrong?" It deliberately does NOT include deal size. Materiality is reported
# separately as dollars at risk, and the report ranks by score x amount so a big
# medium-confidence deal still surfaces. Mixing the two into one number would make a
# large correctly-attributed deal look like a data-quality problem.

def score_deal(deal, flags, default_sources):
    """Return (score, breakdown) where breakdown is a list of (label, delta)."""
    breakdown = []
    total = 0

    for f in sorted(flags, key=lambda x: -x["points"]):
        # Highest-value flag scores in full; each additional flag adds half its base,
        # so stacked evidence raises confidence without instantly pinning every
        # multi-flag deal at 100.
        delta = f["points"] if not breakdown else int(round(f["points"] * 0.5))
        breakdown.append(("flag {}".format(f["code"]), delta))
        total += delta

    ts = deal["touches"]
    n = len(ts)

    if n >= 3:
        breakdown.append(("rich touch history (n>=3)", 15))
        total += 15
    elif n == 1:
        breakdown.append(("single touch only — thin evidence", -20))
        total -= 20
    elif n == 0:
        breakdown.append(("no touch data — cannot corroborate", -15))
        total -= 15

    if n >= 2 and ts[0]["channel"] == ts[-1]["channel"] \
            and ts[0]["channel"] != deal["recorded_source"]:
        breakdown.append(("first and last touch agree on a different channel", 10))
        total += 10

    if deal["recorded_source"] in default_sources:
        paid = [t for t in ts if t["channel"] in {"paid_search", "paid_social"} or t["campaign"]]
        if paid:
            breakdown.append(("blank/default source with campaign-tagged touch present", 10))
            total += 10

    if n and deal["close_date"]:
        dated = [t for t in ts if t["timestamp"]]
        if dated and all(t["timestamp"] > deal["close_date"] for t in dated):
            breakdown.append(("all touches post-date the close — cannot have sourced it", -10))
            total -= 10

    if deal["recorded_source"] in UNTRACKABLE_CHANNELS:
        if ts and all(t["channel"] in {"direct_traffic", "organic_search"} for t in ts):
            breakdown.append(("untrackable recorded source, only direct/organic touches", -10))
            total -= 10

    if deal["undated_touches"] and n:
        breakdown.append(("{} undated touch(es) weaken first/last ordering".format(
            deal["undated_touches"]), -5))
        total -= 5

    if not deal["recorded_source_mapped"] and deal["recorded_source"] \
            and deal["recorded_source"] not in default_sources:
        # An unrecognised value might be a valid channel this script doesn't know about,
        # so soften. Known placeholders like 'unknown' are exempt — those are already
        # scored by BLANK_OR_DEFAULT_SOURCE and shouldn't be discounted twice.
        breakdown.append(("recorded source '{}' is outside the known taxonomy".format(
            deal["recorded_source_raw"]), -5))
        total -= 5

    score = max(0, min(100, total))

    # Hard ceiling: with no touch history there is nothing to contradict the record.
    if n == 0 and score > 40:
        breakdown.append(("capped at 40 — no touch history to contradict the record", 40 - score))
        score = 40

    return score, breakdown


def band(score):
    if score >= 80:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def build_reason(deal, flags, proposed):
    """One line a RevOps human can act on without opening the script."""
    ts = deal["touches"]
    rec = deal["recorded_source_raw"] or "(blank)"
    if not flags:
        return "No misattribution signal."
    lead = flags[0]
    bits = {
        "UNSUPPORTED_CREDIT":
            "Credited to '{}' but none of the {} came from that channel".format(
                rec, plural(len(ts), "logged touch", "logged touches")),
        "SOURCE_CONFLICT":
            "Recorded '{}' contradicts the touch history (first: '{}', last: '{}')".format(
                rec, ts[0]["channel"] if ts else "—", ts[-1]["channel"] if ts else "—"),
        "BLANK_OR_DEFAULT_SOURCE":
            "Source is '{}' but the record shows {}".format(rec, plural(len(
                [t for t in ts if t["channel"] not in DEFAULT_NULLISH_SOURCES]),
                "real touch", "real touches")),
        "DOUBLE_COUNTED_CAMPAIGN":
            "Revenue counted more than once — {}".format(lead["detail"]),
    }
    line = bits.get(lead["code"], lead["detail"])
    extra = [f["code"] for f in flags[1:]]
    if extra:
        line += " (also: {})".format(", ".join(extra))
    if proposed and lead["code"] != "DOUBLE_COUNTED_CAMPAIGN":
        line += ". Proposed: '{}'".format(proposed)
    return line + "."


# --------------------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------------------

def audit(deals, args):
    default_sources = set(args.default_sources)
    weights = args.position_weights
    revenue_twins = find_revenue_twins(deals, args.twin_window)

    results = []
    channel_totals = {m: defaultdict(float) for m in MODELS}
    unmapped_values = defaultdict(int)

    for deal in deals:
        credits = all_model_credits(deal, weights, enable_position=not args.no_position)
        for model, cmap in credits.items():
            for ch, amt in cmap.items():
                channel_totals[model][ch] += amt

        if not deal["recorded_source_mapped"] and deal["recorded_source_raw"]:
            unmapped_values[deal["recorded_source_raw"]] += 1
        for t in deal["touches"]:
            if not t["channel_mapped"] and t["channel_raw"]:
                unmapped_values[t["channel_raw"]] += 1

        flags = [f for f in (
            flag_unsupported_credit(deal),
            flag_source_conflict(deal),
            flag_blank_or_default_source(deal, default_sources),
            flag_double_counted_campaign(deal, revenue_twins),
        ) if f]
        flags.sort(key=lambda f: -f["points"])

        score, breakdown = score_deal(deal, flags, default_sources) if flags else (0, [])
        proposed = propose_source(deal, credits, args.propose_from, default_sources)

        results.append({
            "deal": deal,
            "credits": credits,
            "flags": flags,
            "score": score,
            "breakdown": breakdown,
            "proposed": proposed,
            "reason": build_reason(deal, flags, proposed),
        })

    return {
        "results": results,
        "channel_totals": channel_totals,
        "unmapped_values": dict(unmapped_values),
    }


def propose_source(deal, credits, mode, default_sources):
    """The source we would put in the field, under the chosen model.

    Returns "" when the model has nothing useful to say. Two cases produce "":
    no touch history at all, and a model whose answer is itself a placeholder value.
    Replacing 'offline' with 'direct_traffic' is not a correction — it trades one
    non-answer for another — so a nullish winner falls back to the best real channel
    in the history, and proposes nothing if there isn't one.
    """
    if mode == "first":
        cmap = credits["first_touch"]
    elif mode == "last":
        cmap = credits["last_touch"]
    elif mode == "linear":
        cmap = credits["linear"]
    else:
        cmap = credits["position_based"] or credits["first_touch"]
    proposed = dominant_channel(cmap)
    if proposed in default_sources:
        real = {ch: amt for ch, amt in credits["linear"].items()
                if ch not in default_sources}
        proposed = dominant_channel(real)
    if not proposed or proposed == deal["recorded_source"]:
        return ""
    return proposed


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

def render_report(audit_out, args, meta):
    results = audit_out["results"]
    totals = audit_out["channel_totals"]
    flagged = [r for r in results if r["flags"] and r["score"] >= args.min_confidence]
    flagged.sort(key=lambda r: -(r["score"] * (abs(r["deal"]["amount"] or 0) + 1)))

    total_revenue = sum(d["deal"]["amount"] or 0 for d in results)
    at_risk = sum(r["deal"]["amount"] or 0 for r in flagged)
    high = [r for r in flagged if band(r["score"]) == "high"]
    med = [r for r in flagged if band(r["score"]) == "medium"]
    low = [r for r in flagged if band(r["score"]) == "low"]

    L = []
    A = L.append
    A("# Attribution audit")
    A("")
    A("**Mode: DRY RUN — nothing was written to any CRM.**"
      if not args.apply else
      "**Mode: APPLY PLAN GENERATED — still nothing written. A human must approve the plan.**")
    A("")
    A("| | |")
    A("|---|---|")
    A("| Deals audited | {} |".format(len(results)))
    A("| Total revenue in scope | {} |".format(money(total_revenue)))
    A("| Deals flagged | {} ({:.0f}%) |".format(
        len(flagged), 100.0 * len(flagged) / len(results) if results else 0))
    A("| **Revenue at risk of misattribution** | **{}** ({:.0f}% of total) |".format(
        money(at_risk), 100.0 * at_risk / total_revenue if total_revenue else 0))
    A("| High confidence (80–100) | {} deals, {} |".format(
        len(high), money(sum(r["deal"]["amount"] or 0 for r in high))))
    A("| Medium confidence (55–79) | {} deals, {} |".format(
        len(med), money(sum(r["deal"]["amount"] or 0 for r in med))))
    A("| Low confidence (<55) | {} deals, {} |".format(
        len(low), money(sum(r["deal"]["amount"] or 0 for r in low))))
    A("| Touch data | {} |".format(meta["touch_status"]))
    A("| Proposed-source model | {} |".format(args.propose_from))
    A("")
    if meta.get("join_warning"):
        A("> **⚠ Possible join-key mismatch.** {}".format(meta["join_warning"]))
        A("")

    A("## Dollars at risk")
    A("")
    A('"At risk" means the recorded source is contradicted by the evidence — not that '
      "the revenue is wrong. The money is real; the channel credited for it may not be.")
    A("")
    by_flag = defaultdict(lambda: [0, 0.0])
    for r in flagged:
        for f in r["flags"]:
            by_flag[f["code"]][0] += 1
            by_flag[f["code"]][1] += (r["deal"]["amount"] or 0)
    A("| Flag | Deals | Revenue implicated |")
    A("|---|---:|---:|")
    for code, (cnt, amt) in sorted(by_flag.items(), key=lambda kv: -kv[1][1]):
        A("| `{}` | {} | {} |".format(code, cnt, money(amt)))
    if not by_flag:
        A("| — | 0 | $0 |")
    A("")
    A("A deal carrying two flags is counted once in the headline "
      "*revenue at risk* and once per flag in this table, so this table sums high.")
    A("")

    A("## How credit shifts between channels")
    A("")
    models = [m for m in MODELS if not (m == "position_based" and args.no_position)]
    channels = sorted({c for m in models for c in totals[m]})
    A("| Channel | " + " | ".join(pretty_model(m) for m in models) +
      " | Swing vs recorded |")
    A("|---" * (len(models) + 2) + "|")
    for ch in channels:
        rec = totals["recorded"].get(ch, 0.0)
        others = [totals[m].get(ch, 0.0) for m in models if m != "recorded"]
        lo, hi = (min(others) - rec, max(others) - rec) if others else (0.0, 0.0)
        if not lo and not hi:
            swing = "—"
        elif abs(hi - lo) < 0.01:
            swing = signed_money(hi)
        else:
            swing = "{} … {}".format(signed_money(lo), signed_money(hi))
        A("| {} | {} | {} |".format(
            ch or "(blank)",
            " | ".join(money(totals[m].get(ch, 0.0)) for m in models),
            swing))
    A("")
    A("The swing column is a **range**, not a single number: the least and the most "
      "credit this channel gains or loses when you stop trusting the recorded source "
      "and use a touch-based model instead. A range that stays negative across the "
      "board means the channel is over-credited today under *every* model — that is "
      "the strongest signal in this table. A range that straddles zero means the "
      "answer depends on which model you pick, and picking one is a business "
      "decision, not a data-quality fix.")
    A("")
    modelled = sum(totals["first_touch"].values())
    if modelled < total_revenue:
        A("> **Coverage gap:** touch-based models could only place {} of the {} in "
          "scope. The remaining {} sits on deals with no usable touch history and is "
          "excluded from every non-recorded column above.".format(
              money(modelled), money(total_revenue), money(total_revenue - modelled)))
        A("")

    A("## Top flagged deals")
    A("")
    A("Ranked by confidence x deal size, so a large medium-confidence deal outranks a "
      "tiny certain one. Confidence is *confidence the record is wrong*, not deal value.")
    A("")
    A("| # | Deal | Amount | Current | Proposed | Conf. | Reason |")
    A("|---:|---|---:|---|---|---:|---|")
    for i, r in enumerate(flagged[:args.top], 1):
        d = r["deal"]
        A("| {} | {} | {} | {} | {} | **{}** ({}) | {} |".format(
            i,
            (d["deal_name"] or d["deal_id"] or "—")[:40],
            money(d["amount"]),
            d["recorded_source_raw"] or "(blank)",
            r["proposed"] or "—",
            r["score"], band(r["score"]),
            r["reason"]))
    if not flagged:
        A("| — | No deals flagged above the confidence threshold. | | | | | |")
    A("")

    if flagged:
        A("### Score derivation for the top {} deals".format(min(5, len(flagged))))
        A("")
        A("Every score is an itemised sum. Nothing is hidden.")
        A("")
        for r in flagged[:5]:
            d = r["deal"]
            A("**{}** — {} → final **{}**".format(
                d["deal_name"] or d["deal_id"], money(d["amount"]), r["score"]))
            A("")
            for label, delta in r["breakdown"]:
                A("- `{}{}` {}".format("+" if delta >= 0 else "", delta, label))
            A("")
            if d["touches"]:
                A("  Touch path: {}".format(" → ".join(
                    "{}{}".format(t["channel"], "/" + t["campaign"] if t["campaign"] else "")
                    for t in d["touches"])))
                A("")

    if audit_out["unmapped_values"]:
        A("## Unrecognised channel values")
        A("")
        A("These source/channel strings are not in the known taxonomy. They were kept "
          "verbatim and grouped as their own channels, which can distort the model "
          "comparison. Map them before trusting the swing table.")
        A("")
        for val, cnt in sorted(audit_out["unmapped_values"].items(), key=lambda kv: -kv[1]):
            A("- `{}` — {} occurrence(s)".format(val, cnt))
        A("")

    A("## Assumptions & limitations")
    A("")
    A(assumptions_text(args, meta))
    A("")
    A("## What happens next")
    A("")
    A("1. Review `attribution-corrections.csv`. It is a proposal, not a decision.")
    A("2. Delete or edit any row you disagree with. Sales context beats touch data — a "
      "deal a rep sourced at a dinner is genuinely `offline` no matter what the "
      "pixel saw.")
    A("3. Only after that review does a write-back step run, and only with an explicit "
      "`--apply` plus confirmation. This report changed nothing.")
    return "\n".join(L)


def pretty_model(m):
    return {
        "recorded": "Recorded (today)",
        "first_touch": "First touch",
        "last_touch": "Last touch",
        "linear": "Linear",
        "position_based": "Position (U)",
    }.get(m, m)


def assumptions_text(args, meta):
    fw, mw, lw = args.position_weights
    lines = [
        "- **Every model here is a lens, not a truth.** A deal has one real origin story; "
        "these are four different ways of guessing it from log data. Disagreement between "
        "models is expected and is not itself evidence of a bug.",
        "- **Position-based weights are {:.0f}/{:.0f}/{:.0f}** (first/middle/last). Change "
        "with `--position-weights`. A long enterprise cycle with an SDR-sourced open "
        "usually wants more weight on first touch; a short self-serve funnel wants more "
        "on last.".format(fw * 100, mw * 100, lw * 100),
        "- **Touches are deduplicated by `{}`** before modelling. Without this the linear "
        "model is dominated by whichever channel generates the most repeat visits "
        "(usually direct and organic), which understates paid and email. Use "
        "`--dedupe none` to see the raw effect.".format(args.dedupe),
        "- **Confidence excludes deal size on purpose.** A $2M deal is not more likely to "
        "be misattributed than a $2k one. Size drives the ranking, not the score.",
        "- **{}**".format(meta["touch_status_note"]),
        "- **Offline and referral sources are given the benefit of the doubt.** They are "
        "never flagged as `UNSUPPORTED_CREDIT`, because a conference conversation or a "
        "partner intro legitimately leaves no digital touch. Tune this by editing "
        "`UNTRACKABLE_CHANNELS` in the script.",
        "- **Values treated as 'no real answer'**: {}. Adjust with `--default-sources` if "
        "your portal uses different placeholders.".format(
            ", ".join("`{}`".format(s) for s in sorted(args.default_sources) if s)),
        "- **Dates**: `MM/DD/YYYY` is assumed over `DD/MM/YYYY` for ambiguous values. "
        "European decimal commas (`1.234,56`) are not parsed — convert first.",
        "- **Cross-deal double counting needs a company or contact column.** Without one, "
        "only the intra-deal (multi-row) form of double counting is detectable.",
        "- **Attribution windows are not applied.** Every touch on the record is "
        "considered, however old. If your team works a 90-day window, filter the touch "
        "export before running.",
        "- **This script cannot see**: sales-logged activity that never produced a "
        "tracked touch, dark social, self-reported attribution fields, and any channel "
        "your tracking does not instrument. Deals sourced that way will look "
        "misattributed and are not.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# CSV / JSON outputs
# --------------------------------------------------------------------------------------

CORRECTION_HEADERS = [
    "deal_id", "current_source", "proposed_source", "amount", "reason", "confidence",
    "confidence_band", "deal_name", "close_date", "stage", "flags", "touch_count",
    "touch_path", "score_breakdown", "first_touch", "last_touch", "linear_dominant",
    "position_dominant", "review_decision",
]


def write_corrections(path, audit_out, args):
    flagged = [r for r in audit_out["results"]
               if r["flags"] and r["score"] >= args.min_confidence]
    flagged.sort(key=lambda r: -(r["score"] * (abs(r["deal"]["amount"] or 0) + 1)))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CORRECTION_HEADERS)
        for r in flagged:
            d = r["deal"]
            w.writerow([
                d["deal_id"],
                d["recorded_source_raw"] or "",
                r["proposed"] or "",
                "{:.2f}".format(d["amount"]) if d["amount"] is not None else "",
                r["reason"],
                r["score"],
                band(r["score"]),
                d["deal_name"],
                d["close_date"].date().isoformat() if d["close_date"] else "",
                d["stage"],
                "|".join(f["code"] for f in r["flags"]),
                len(d["touches"]),
                " > ".join("{}{}".format(
                    t["channel"], "/" + t["campaign"] if t["campaign"] else "")
                    for t in d["touches"]),
                "; ".join("{}{}={}".format("+" if delta >= 0 else "", delta, label)
                          for label, delta in r["breakdown"]),
                dominant_channel(r["credits"]["first_touch"]),
                dominant_channel(r["credits"]["last_touch"]),
                dominant_channel(r["credits"]["linear"]),
                dominant_channel(r["credits"]["position_based"]),
                "",  # review_decision — a human fills this in: approve / reject / edit
            ])
    return len(flagged)


def write_shift_csv(path, audit_out, args):
    totals = audit_out["channel_totals"]
    models = [m for m in MODELS if not (m == "position_based" and args.no_position)]
    channels = sorted({c for m in models for c in totals[m]})
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["channel"] + models + ["swing_min", "swing_max"])
        for ch in channels:
            rec = totals["recorded"].get(ch, 0.0)
            others = [totals[m].get(ch, 0.0) for m in models if m != "recorded"]
            lo, hi = (min(others) - rec, max(others) - rec) if others else (0.0, 0.0)
            w.writerow([ch or "(blank)"] +
                       ["{:.2f}".format(totals[m].get(ch, 0.0)) for m in models] +
                       ["{:.2f}".format(lo), "{:.2f}".format(hi)])


def write_apply_plan(path, audit_out, args):
    """Emit the exact writes a human is authorising. This script does NOT execute them."""
    # Deals whose only problem is double counting are deliberately excluded. Rewriting
    # the source property does not remove a duplicate row or un-credit a second
    # campaign — that is a deletion or a report-definition change, and it is not
    # something this skill will ever propose automatically.
    eligible, deferred = [], []
    for r in audit_out["results"]:
        if not r["flags"] or r["score"] < args.apply_min_confidence:
            continue
        codes = {f["code"] for f in r["flags"]}
        if codes == {"DOUBLE_COUNTED_CAMPAIGN"} or not r["proposed"]:
            deferred.append(r)
        else:
            eligible.append(r)
    flagged = eligible
    plan = {
        "generated_by": "attribution_audit.py",
        "executed": False,
        "warning": ("This file is a PROPOSAL. attribution_audit.py has no CRM write "
                    "capability and has written nothing. Execution happens only through "
                    "the HubSpot MCP after a human approves this plan."),
        "property_to_write": args.write_property,
        "min_confidence_for_apply": args.apply_min_confidence,
        "proposed_model": args.propose_from,
        "write_count": len(flagged),
        "excluded_needing_human_action": [
            {
                "deal_id": r["deal"]["deal_id"],
                "deal_name": r["deal"]["deal_name"],
                "amount": r["deal"]["amount"],
                "confidence": r["score"],
                "why_excluded": (
                    "double-counted revenue cannot be fixed by writing a source "
                    "property; resolve the duplicate row or campaign credit manually"
                    if {f["code"] for f in r["flags"]} == {"DOUBLE_COUNTED_CAMPAIGN"}
                    else "no proposed source — the models had no better answer"),
                "reason": r["reason"],
            }
            for r in sorted(deferred, key=lambda r: -r["score"])
        ],
        "writes": [
            {
                "deal_id": r["deal"]["deal_id"],
                "deal_name": r["deal"]["deal_name"],
                "property": args.write_property,
                "current_value": r["deal"]["recorded_source_raw"],
                "new_value": r["proposed"],
                "amount": r["deal"]["amount"],
                "confidence": r["score"],
                "reason": r["reason"],
            }
            for r in sorted(flagged, key=lambda r: -r["score"])
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)
    return len(flagged)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def parse_weights(s):
    parts = [float(p) for p in str(s).split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("expected three comma-separated weights, e.g. 40,20,40")
    total = sum(parts)
    if total <= 0:
        raise argparse.ArgumentTypeError("weights must sum to more than zero")
    return tuple(p / total for p in parts)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Audit deal-to-source revenue attribution. Read-only by default.")
    p.add_argument("--deals", required=True, help="CSV of deals")
    p.add_argument("--touches", help="CSV of marketing touches (optional but strongly advised)")
    p.add_argument("--map", dest="map_path", help="column mapping JSON from map_columns.py")
    p.add_argument("--out-dir", default="attribution-audit", help="output directory")
    p.add_argument("--propose-from", default="first",
                   choices=["first", "last", "linear", "position"],
                   help="which model supplies the proposed source (default: first, matching "
                        "HubSpot's 'Original source' semantics)")
    p.add_argument("--position-weights", type=parse_weights, default=(0.40, 0.20, 0.40),
                   help="first,middle,last weights for the U-shaped model (default 40,20,40)")
    p.add_argument("--no-position", action="store_true",
                   help="skip the position-based model entirely")
    p.add_argument("--dedupe", default="channel_day",
                   choices=["channel_day", "channel", "none"],
                   help="touch deduplication before modelling (default: channel_day)")
    p.add_argument("--default-sources", default=",".join(sorted(DEFAULT_NULLISH_SOURCES)),
                   help="comma-separated values treated as 'no real source'")
    p.add_argument("--twin-window", type=int, default=30,
                   help="days within which two same-amount deals count as duplicate revenue")
    p.add_argument("--min-confidence", type=int, default=0,
                   help="omit flagged deals scoring below this from report and CSV")
    p.add_argument("--top", type=int, default=25, help="rows in the top-flagged table")
    p.add_argument("--apply", action="store_true",
                   help="ALSO emit attribution-apply-plan.json. Still writes nothing to any "
                        "CRM — the plan is for human approval before an agent executes it.")
    p.add_argument("--apply-min-confidence", type=int, default=80,
                   help="minimum confidence for a deal to enter the apply plan (default 80)")
    p.add_argument("--write-property", default="hs_analytics_source",
                   help="CRM property the apply plan targets (default hs_analytics_source)")
    p.add_argument("--json", action="store_true", help="also print a JSON summary to stdout")
    args = p.parse_args(argv)

    args.default_sources = {s.strip().lower() for s in args.default_sources.split(",")}

    mapping = load_mapping(args.map_path)
    deals_raw, deal_headers = load_deals(args.deals, mapping)
    if not deals_raw:
        print("No rows found in {}".format(args.deals), file=sys.stderr)
        return 2
    touches, _ = load_touches(args.touches, mapping)

    deals = collapse_deal_rows(deals_raw)
    deals = attach_touches(deals, touches, args.dedupe)

    with_touches = sum(1 for d in deals if d["touches"])

    # A touch file that parses cleanly but joins to nothing is a broken join key, not
    # sparse data — and it produces the same falsely-clean "$0 at risk" as an unmapped
    # column. Refuse, and show both ID namespaces so the mismatch is obvious.
    if touches and with_touches == 0:
        deal_ids = [d["deal_id"] for d in deals if d["deal_id"]][:3]
        touch_deal_ids = [t["deal_id"] for t in touches if t["deal_id"]][:3]
        touch_contact_ids = [t["contact_id"] for t in touches if t["contact_id"]][:3]
        raise SystemExit(
            "\nERROR: {} touch rows loaded but none of them joined to any deal.\n\n"
            "  deal_id in deals file    : {}\n"
            "  deal_id in touches file  : {}\n"
            "  contact_id in touches    : {}\n\n"
            "The join key does not match. Usually this means the touches file needs a "
            "different column mapped to deal_id (or contact_id):\n"
            "  map_columns.py write --deals ... --touches ... --out mapping.json \\\n"
            "      --set touches.deal_id='<the column holding the deal identifier>'\n\n"
            "Refusing to continue: without the join every touch-based check is silently "
            "skipped and the audit would report '$0 at risk' for the wrong reason.".format(
                len(touches),
                ", ".join(deal_ids) or "(none)",
                ", ".join(touch_deal_ids) or "(none)",
                ", ".join(touch_contact_ids) or "(none)"))

    join_warning = None
    if touches and 0 < with_touches < len(deals) * 0.25:
        join_warning = (
            "Only {} of {} deals matched any touch row, though {} touch rows loaded. "
            "That is low enough to suspect a partial join-key mismatch rather than "
            "genuinely sparse history. Verify before trusting the model columns."
            .format(with_touches, len(deals), len(touches)))
        print("WARNING: {}".format(join_warning), file=sys.stderr)

    if not args.touches:
        touch_status = "none supplied"
        note = ("No touch file was supplied, so `UNSUPPORTED_CREDIT`, `SOURCE_CONFLICT` and "
                "`BLANK_OR_DEFAULT_SOURCE` could not be evaluated. Only campaign "
                "double-counting was checked. Supply a touch export for a real audit.")
    else:
        touch_status = "{}/{} deals have touches ({} touch rows)".format(
            with_touches, len(deals), len(touches))
        note = ("{} of {} deals have no matching touch history. Their scores are capped at "
                "40 because there is no evidence to contradict the record — absence of "
                "touch data is not evidence of misattribution.".format(
                    len(deals) - with_touches, len(deals)))

    out = audit(deals, args)

    os.makedirs(args.out_dir, exist_ok=True)
    report_path = os.path.join(args.out_dir, "attribution-audit.md")
    corr_path = os.path.join(args.out_dir, "attribution-corrections.csv")
    shift_path = os.path.join(args.out_dir, "channel-credit-shift.csv")

    report = render_report(out, args, {"touch_status": touch_status,
                                       "touch_status_note": note,
                                       "join_warning": join_warning})
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    n_corr = write_corrections(corr_path, out, args)
    write_shift_csv(shift_path, out, args)

    plan_path = None
    n_plan = 0
    if args.apply:
        plan_path = os.path.join(args.out_dir, "attribution-apply-plan.json")
        n_plan = write_apply_plan(plan_path, out, args)

    flagged = [r for r in out["results"] if r["flags"] and r["score"] >= args.min_confidence]
    at_risk = sum(r["deal"]["amount"] or 0 for r in flagged)

    print("DRY RUN — no CRM was contacted and nothing was written to any CRM."
          if not args.apply else
          "APPLY PLAN WRITTEN — still no CRM contact. A human must approve the plan.")
    print("  deals audited     : {}".format(len(deals)))
    print("  deals flagged     : {}".format(len(flagged)))
    print("  revenue at risk   : {}".format(money(at_risk)))
    print("  report            : {}".format(report_path))
    print("  corrections ({:>3}) : {}".format(n_corr, corr_path))
    print("  credit shift      : {}".format(shift_path))
    if plan_path:
        print("  apply plan  ({:>3}) : {}".format(n_plan, plan_path))

    if args.json:
        print(json.dumps({
            "dry_run": not args.apply,
            "deals_audited": len(deals),
            "deals_flagged": len(flagged),
            "revenue_at_risk": round(at_risk, 2),
            "by_band": {
                b: sum(1 for r in flagged if band(r["score"]) == b)
                for b in ("high", "medium", "low")
            },
            "outputs": {
                "report": report_path,
                "corrections": corr_path,
                "credit_shift": shift_path,
                "apply_plan": plan_path,
            },
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
map_columns.py — map arbitrary CSV headers onto the canonical fields that
attribution_audit.py expects.

CRM exports name the same column a dozen ways: "Record ID", "Deal ID", "hs_object_id",
"Associated Deal". This proposes a mapping, shows its evidence, and writes a JSON file
the auditor consumes. It never guesses silently: every proposal carries a confidence and
the reason it was made, so the agent can put low-confidence guesses to the user before
anything runs.

Stdlib only. Python 3.8+.

Usage
-----
  # 1. Look at what is in the file and what the mapping would be
  map_columns.py inspect --deals deals.csv --touches touches.csv

  # 2. Write the mapping, overriding anything the proposal got wrong
  map_columns.py write --deals deals.csv --touches touches.csv \
      --out mapping.json \
      --set deals.amount="Weighted ACV" --set touches.channel="utm_medium"

  # 3. Feed it to the auditor
  attribution_audit.py --deals deals.csv --touches touches.csv --map mapping.json
"""

import argparse
import csv
import difflib
import json
import re
import sys
from collections import OrderedDict

# Canonical fields, in the order they matter. `required` drives the "can this audit even
# run" check; `why` is shown to the user so an unmapped field is a real decision.
DEAL_SCHEMA = OrderedDict([
    ("deal_id", {"required": True,
                 "why": "joins deals to touches and identifies rows for write-back"}),
    ("amount", {"required": True,
                "why": "the revenue being attributed; without it nothing can be quantified"}),
    ("recorded_source", {"required": True,
                         "why": "what the CRM credits today — the thing being audited"}),
    ("deal_name", {"required": False, "why": "human-readable label in the report"}),
    ("close_date", {"required": False,
                    "why": "detects touches that post-date the close"}),
    ("stage", {"required": False, "why": "lets you scope the audit to closed-won"}),
    ("recorded_source_detail", {"required": False,
                                "why": "campaign/keyword detail behind the source"}),
    ("latest_source", {"required": False, "why": "reported alongside original source"}),
    ("campaign", {"required": False,
                  "why": "required for campaign double-count detection"}),
    ("contact_id", {"required": False,
                    "why": "fallback join to touches when touches lack a deal id"}),
    ("company", {"required": False,
                 "why": "required for cross-deal duplicate-revenue detection"}),
])

TOUCH_SCHEMA = OrderedDict([
    ("channel", {"required": True, "why": "the channel of the touch; the core evidence"}),
    ("timestamp", {"required": True,
                   "why": "orders the path; without it first/last touch is meaningless"}),
    ("deal_id", {"required": False, "why": "preferred join key to the deal"}),
    ("contact_id", {"required": False, "why": "fallback join key when deal_id is absent"}),
    ("campaign", {"required": False, "why": "campaign-level credit and double counting"}),
    ("source_detail", {"required": False, "why": "extra context in the report"}),
])

# Header synonyms seen in real HubSpot, Salesforce, and BI exports.
SYNONYMS = {
    "deal_id": [
        "deal id", "dealid", "record id", "recordid", "hs_object_id", "object id",
        "opportunity id", "id", "deal record id", "associated deal id", "deal",
    ],
    "deal_name": [
        "deal name", "dealname", "name", "opportunity name", "deal title", "title",
    ],
    "amount": [
        "amount", "deal amount", "value", "deal value", "revenue", "acv", "arr", "mrr",
        "closed won amount", "amount in company currency", "hs_acv", "tcv",
        "weighted amount", "total contract value", "annual contract value",
    ],
    "close_date": [
        "close date", "closedate", "closed date", "date closed", "won date",
        "close_date", "closed won date", "hs_closed_won_date",
    ],
    "stage": [
        "stage", "deal stage", "dealstage", "pipeline stage", "status", "deal status",
    ],
    "recorded_source": [
        "original source", "source", "hs_analytics_source", "original source type",
        "lead source", "leadsource", "deal source", "first conversion source",
        "original traffic source", "channel", "acquisition channel", "utm_source",
        "attributed source", "original_source",
    ],
    "recorded_source_detail": [
        "original source drill-down 1", "hs_analytics_source_data_1",
        "original source data 1", "source detail", "original source detail",
        "lead source detail", "utm_campaign", "drill down 1", "source drill down",
    ],
    "latest_source": [
        "latest source", "hs_latest_source", "latest traffic source",
        "most recent source", "last source", "latest source type",
    ],
    "campaign": [
        "campaign", "campaign name", "hs_campaign", "marketing campaign",
        "utm_campaign", "primary campaign", "campaign id", "attributed campaign",
    ],
    "contact_id": [
        "contact id", "contactid", "associated contact id", "associated contact",
        "primary contact id", "vid", "person id", "lead id",
    ],
    "company": [
        "company", "company name", "associated company", "account", "account name",
        "associated company id", "domain", "organisation", "organization",
    ],
    "channel": [
        "channel", "source", "touch channel", "medium", "utm_medium", "utm_source",
        "traffic source", "interaction source", "touchpoint channel", "source type",
        "activity type", "event type",
    ],
    "timestamp": [
        "timestamp", "date", "datetime", "touch date", "interaction date",
        "activity date", "occurred at", "created at", "event date", "time",
        "engagement date", "hs_timestamp",
    ],
    "source_detail": [
        "source detail", "detail", "drill down", "utm_content", "utm_term",
        "referrer", "page url", "landing page", "keyword",
    ],
}


def normalize(h):
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).strip()


def score_header(field, header):
    """Return (confidence 0-100, reason). Deterministic — no ML, no hidden weights.

    100  exact match on the canonical field name
     95  exact match on a known synonym
     80  a synonym appears as a whole-word substring of the header
     60+ fuzzy string similarity above 0.72, scaled
      0  no signal
    """
    h = normalize(header)
    f = normalize(field)
    if not h:
        return 0, ""
    if h == f:
        return 100, "header matches the canonical field name exactly"
    syns = SYNONYMS.get(field, [])
    if h in [normalize(s) for s in syns]:
        return 95, "header is a known synonym for {}".format(field)
    for s in syns:
        ns = normalize(s)
        if ns and re.search(r"\b{}\b".format(re.escape(ns)), h):
            return 80, "header contains the known synonym '{}'".format(s)
    best = 0.0
    best_s = ""
    for s in [f] + syns:
        r = difflib.SequenceMatcher(None, h, normalize(s)).ratio()
        if r > best:
            best, best_s = r, s
    if best >= 0.72:
        return int(round(55 + (best - 0.72) * 100)), \
            "fuzzy match to '{}' ({:.0%} similar)".format(best_s, best)
    return 0, ""


def propose(headers, schema):
    """Greedy best-match assignment: strongest (field, header) pair wins, then that
    header is consumed so two fields never claim the same column."""
    pairs = []
    for field in schema:
        for h in headers:
            conf, reason = score_header(field, h)
            if conf:
                pairs.append((conf, field, h, reason))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    mapping = OrderedDict()
    used_headers = set()
    for conf, field, h, reason in pairs:
        if field in mapping or h in used_headers:
            continue
        mapping[field] = {"header": h, "confidence": conf, "reason": reason}
        used_headers.add(h)
    return mapping


def read_headers_and_samples(path, n=3):
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        samples = []
        for i, row in enumerate(reader):
            if i >= n:
                break
            samples.append(row)
    return headers, samples


def report_side(label, path, schema, n_samples):
    headers, samples = read_headers_and_samples(path, n_samples)
    mapping = propose(headers, schema)
    print("\n=== {} : {} ===".format(label.upper(), path))
    print("{} columns found".format(len(headers)))
    print("\n{:<24} {:<32} {:>5}  {}".format("CANONICAL FIELD", "MAPPED TO", "CONF", "WHY"))
    print("-" * 100)
    missing_required = []
    low_conf = []
    for field, spec in schema.items():
        m = mapping.get(field)
        req = "*" if spec["required"] else " "
        if not m:
            print("{}{:<23} {:<32} {:>5}  {}".format(
                req, field, "-- UNMAPPED --", "", spec["why"]))
            if spec["required"]:
                missing_required.append(field)
            continue
        print("{}{:<23} {:<32} {:>5}  {}".format(
            req, field, m["header"][:32], m["confidence"], m["reason"]))
        if m["confidence"] < 80:
            low_conf.append((field, m))
    unused = [h for h in headers if h not in {m["header"] for m in mapping.values()}]
    if unused:
        print("\nUnmapped columns in the file (ignored): {}".format(
            ", ".join(repr(u) for u in unused[:20]) + (" ..." if len(unused) > 20 else "")))
    if samples:
        print("\nSample values for mapped columns:")
        for field, m in mapping.items():
            vals = [str(s.get(m["header"], ""))[:28] for s in samples]
            print("  {:<24} {}".format(field, " | ".join(v or "(empty)" for v in vals)))
    return {
        "mapping": OrderedDict((f, m["header"]) for f, m in mapping.items()),
        "detail": mapping,
        "missing_required": missing_required,
        "low_confidence": low_conf,
        "headers": headers,
    }


def apply_overrides(result, overrides, side):
    """--set deals.amount="Weighted ACV" style overrides."""
    for key, value in overrides:
        if "." in key:
            which, field = key.split(".", 1)
        else:
            which, field = side, key
        if which != side:
            continue
        if value == "":
            result["mapping"].pop(field, None)
            continue
        if value not in result["headers"]:
            print("WARNING: --set {}.{}='{}' — no such column in the file. "
                  "Available: {}".format(side, field, value,
                                         ", ".join(repr(h) for h in result["headers"])),
                  file=sys.stderr)
        result["mapping"][field] = value
        result["missing_required"] = [
            f for f in result["missing_required"] if f != field]
    return result


def parse_set(s):
    if "=" not in s:
        raise argparse.ArgumentTypeError("--set expects field=header, e.g. deals.amount='ACV'")
    k, v = s.split("=", 1)
    return k.strip(), v.strip()


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Propose and write a column mapping for attribution_audit.py")
    p.add_argument("command", choices=["inspect", "write"])
    p.add_argument("--deals", required=True)
    p.add_argument("--touches")
    p.add_argument("--out", default="mapping.json", help="mapping JSON path (write only)")
    p.add_argument("--set", dest="sets", action="append", type=parse_set, default=[],
                   metavar="deals.field=Header",
                   help="override one mapping; repeatable. Use an empty value to unmap.")
    p.add_argument("--samples", type=int, default=3, help="sample rows to display")
    args = p.parse_args(argv)

    deals = report_side("deals", args.deals, DEAL_SCHEMA, args.samples)
    deals = apply_overrides(deals, args.sets, "deals")

    touches = None
    if args.touches:
        touches = report_side("touches", args.touches, TOUCH_SCHEMA, args.samples)
        touches = apply_overrides(touches, args.sets, "touches")

    print("\n" + "=" * 100)
    problems = []
    if deals["missing_required"]:
        problems.append("deals is missing required field(s): {}".format(
            ", ".join(deals["missing_required"])))
    if touches and touches["missing_required"]:
        problems.append("touches is missing required field(s): {}".format(
            ", ".join(touches["missing_required"])))
    low = list(deals["low_confidence"]) + (list(touches["low_confidence"]) if touches else [])
    if low:
        print("CONFIRM WITH THE USER — these mappings are guesses under 80% confidence:")
        for field, m in low:
            print("  {} -> '{}' ({}%, {})".format(field, m["header"], m["confidence"],
                                                  m["reason"]))
    if problems:
        for msg in problems:
            print("BLOCKER: {}".format(msg))
        print("Fix with --set, e.g. --set deals.amount='Your Column Name'")

    if args.command == "write":
        if problems:
            print("\nRefusing to write a mapping that is missing required fields.",
                  file=sys.stderr)
            return 2
        payload = {"deals": dict(deals["mapping"])}
        if touches:
            payload["touches"] = dict(touches["mapping"])
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print("\nWrote {}".format(args.out))
        print("Next: attribution_audit.py --deals {}{} --map {}".format(
            args.deals,
            " --touches {}".format(args.touches) if args.touches else "",
            args.out))
    else:
        print("\nDry inspect only. Re-run with `write` to save the mapping.")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())

"""Conversion-signal integrity simulator.

This module models the exact failure the ad-spam team owns: target-CPA bidding
learns from conversion signals, so poisoning those signals moves real budget
onto inventory that never produces revenue.

The model is deliberately small and inspectable:

  segment      : a traffic cohort with a true conversion rate and true value
  bidder       : target-CPA rule, bid = tCPA * p_hat(segment)
  poisoning    : fake conversions are injected at a given rate, optionally
                 removed by dedup/verification before they reach the model

Reported CPA is what the advertiser dashboard shows. True CPA divides the same
spend by conversions that produced revenue. The gap between them is the attack.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class Segment:
    name: str
    share: float
    true_p: float
    true_value: float
    fake_rate: float  # fake conversions produced per impression opportunity
    zero_value: bool


SEGMENTS = [
    Segment("genuine_search", share=0.62, true_p=0.0024, true_value=140.0, fake_rate=0.0, zero_value=False),
    Segment("genuine_retarget", share=0.18, true_p=0.0031, true_value=165.0, fake_rate=0.0, zero_value=False),
    Segment("low_value_app", share=0.12, true_p=0.0006, true_value=6.0, fake_rate=0.0, zero_value=False),
    Segment("bot_inventory", share=0.08, true_p=0.00005, true_value=0.0, fake_rate=0.35, zero_value=True),
]


TAMPER_VECTORS = [
    {
        "id": "fake_form_fill",
        "name": "Fake conversion injection",
        "mechanic": "Bot traffic auto-submits lead forms or fires the conversion pixel with junk data, "
                    "inflating one cheap conversion action.",
        "tell": "Conversion rate spikes on a single action; form payloads share field patterns; "
                "high conversion rate with zero downstream CRM progression.",
        "fix": "Delay the conversion until a verified downstream event (qualified lead, paid order) and "
               "score payload quality before it trains the model.",
    },
    {
        "id": "offline_upload_poison",
        "name": "Offline conversion upload poisoning",
        "mechanic": "Fabricated rows are pushed through the offline conversion import using guessed or "
                    "harvested identifiers, teaching the model that cheap inventory converts.",
        "tell": "Upload acceptance rate far above the account baseline; duplicate order IDs; "
                "conversion timestamps that precede the click.",
        "fix": "Require cryptographic provenance on uploaded rows and reconcile against billing or "
               "order systems before the row is eligible for training.",
    },
    {
        "id": "gclid_replay",
        "name": "Click-ID replay / double counting",
        "mechanic": "The same click identifier is reported repeatedly so one genuine sale is counted "
                    "many times, inflating apparent performance of that placement.",
        "tell": "Duplicate identifiers across batches; conversion-to-click ratio above 1; "
                "same identifier arriving from multiple sources.",
        "fix": "Enforce idempotent ingestion keyed on the click identifier plus event identity.",
    },
    {
        "id": "action_swap",
        "name": "Conversion action swap",
        "mechanic": "An advertiser switches bidding to a high-volume, low-value action so the reported "
                    "CPA looks healthy while real business CPA deteriorates.",
        "tell": "Reported CPA improves while revenue per conversion collapses; sudden change in "
                "action mix without a matching campaign change.",
        "fix": "Anchor optimisation on value, not counts, and alert when the value/count ratio moves "
               "beyond a tolerance band.",
    },
    {
        "id": "postback_spoof",
        "name": "Server-to-server postback spoofing",
        "mechanic": "A forged postback from a third party claims conversions for inventory that never "
                    "delivered them, capturing credit and budget.",
        "tell": "Postbacks from unexpected IP ranges; conversions with no matching impression; "
                "callback timing inconsistent with the product's funnel depth.",
        "fix": "Sign postbacks, pin expected source ranges, and drop events with no attributable impression.",
    },
    {
        "id": "fake_call",
        "name": "Call-conversion fraud",
        "mechanic": "Short, low-intent calls under the qualification threshold are generated to harvest "
                    "per-call payouts and inflate call conversions.",
        "tell": "Call duration distribution clusters just above the payable threshold; repeated caller "
                "numbers; identical call start times.",
        "fix": "Grade calls on outcome, not duration, and weight call conversions by downstream sale rate.",
    },
]


def _p_hat(observed_conv: float, impressions: int, prior: float = 0.01, strength: int = 400) -> float:
    """Beta-style smoothing so a thin poisoned cohort cannot dominate instantly."""
    return (observed_conv + prior * strength) / (impressions + strength)


def simulate(
    impressions: int = 200_000,
    tCPA: float = 45.0,
    budget: float = 20_000.0,
    injection_rate: float = 1.0,
    dedup_effectiveness: float = 0.0,
    value_based: bool = True,
    seed: int = 7,
) -> dict:
    """Two-round tCPA simulation.

    Round 1 explores with equal delivery and collects conversion data.
    Round 2 reallocates budget by bid, i.e. by the learned p_hat.
    """
    rng = random.Random(seed)
    injection_rate = max(0.0, min(3.0, injection_rate))
    dedup_effectiveness = max(0.0, min(1.0, dedup_effectiveness))

    seg_rows = []
    for seg in SEGMENTS:
        imp = int(impressions * seg.share)
        true_conv = sum(1 for _ in range(imp) if rng.random() < seg.true_p)
        true_value = true_conv * seg.true_value

        fake_generated = int(imp * seg.fake_rate * injection_rate) if seg.fake_rate else 0
        fake_surviving = int(round(fake_generated * (1.0 - dedup_effectiveness)))

        # Round 1: equal-ish delivery, so each segment gets its natural share.
        spend_r1 = budget * 0.4 * seg.share

        # What the model sees after round 1.
        reported_conv = true_conv + fake_surviving
        p_hat = _p_hat(reported_conv, imp)

        seg_rows.append({
            "segment": seg.name,
            "impressions": imp,
            "true_conversions": true_conv,
            "fake_conversions_generated": fake_generated,
            "fake_conversions_counted": fake_surviving,
            "reported_conversions": reported_conv,
            "true_value": round(true_value, 2),
            "zero_value": seg.zero_value,
            "spend_round1": round(spend_r1, 2),
            "p_hat": round(p_hat, 6),
            "bid": round(tCPA * p_hat, 4),
            "value_per_conversion": round(seg.true_value, 2),
        })

    # Round 2 allocation: budget proportional to expected value under the learned model.
    def weight(row: dict) -> float:
        if value_based:
            # value-based bidding uses an estimated value per conversion
            est_value = row["value_per_conversion"] if row["true_conversions"] else 0.0
            if row["fake_conversions_counted"] and not row["true_conversions"]:
                # poisoned cohort carries no value signal of its own, but a
                # count-based model still bids on it; value-based bidding does not
                est_value = 0.0
            return max(0.0, row["bid"] * (est_value if value_based else 1.0))
        return max(0.0, row["bid"])

    total_w = sum(weight(r) for r in seg_rows) or 1.0
    spend_r2_total = budget * 0.6
    for r in seg_rows:
        share = weight(r) / total_w
        r["budget_share_round2"] = round(share, 4)
        r["spend_round2"] = round(spend_r2_total * share, 2)
        r["spend_total"] = round(r["spend_round1"] + r["spend_round2"], 2)
        # Round-2 delivery scales with the budget it receives.
        # Delivery cannot scale without limit: cap at 4x the exploration volume.
        ratio = r["spend_round2"] / r["spend_round1"] if r["spend_round1"] else 0.0
        ratio = min(ratio, 4.0)
        r["true_conversions_round2"] = round(r["true_conversions"] * ratio, 1)
        r["reported_conversions_round2"] = round(r["reported_conversions"] * ratio, 1)
        r["true_value_round2"] = round(r["true_conversions_round2"] * r["value_per_conversion"], 2)

    spend = sum(r["spend_total"] for r in seg_rows)
    reported = sum(r["reported_conversions"] + r["reported_conversions_round2"] for r in seg_rows)
    true_conv = sum(r["true_conversions"] + r["true_conversions_round2"] for r in seg_rows)
    true_value = sum(r["true_value_round2"] for r in seg_rows)
    fake_counted = sum(r["fake_conversions_counted"] for r in seg_rows)

    reported_cpa = spend / reported if reported else 0
    true_cpa = spend / true_conv if true_conv else 0
    roas = true_value / spend if spend else 0

    bot = next((r for r in seg_rows if r["zero_value"]), None)
    bot_share_r1 = SEGMENTS[3].share if len(SEGMENTS) > 3 else 0
    bot_share_r2 = bot["budget_share_round2"] if bot else 0

    if true_cpa and reported_cpa:
        distortion = round(true_cpa / reported_cpa, 2)
    else:
        distortion = 0.0

    if distortion >= 2.2:
        verdict = ("Severe signal corruption: reported CPA understates the true cost by "
                   f"{distortion}x and budget concentrates on inventory with no revenue.")
    elif distortion >= 1.4:
        verdict = (f"Material corruption: true CPA is {distortion}x the reported figure. "
                   "The bidding model is rewarding inventory that does not pay back.")
    elif distortion >= 1.08:
        verdict = f"Mild distortion: true CPA is {distortion}x reported. Watch the value/count ratio."
    else:
        verdict = "Signals are consistent: reported and true CPA agree within tolerance."

    return {
        "inputs": {
            "impressions": impressions, "target_cpa": tCPA, "budget": budget,
            "injection_rate": injection_rate, "dedup_effectiveness": dedup_effectiveness,
            "bidding": "value-based (tROAS style)" if value_based else "count-based (tCPA)",
        },
        "segments": seg_rows,
        "totals": {
            "spend": round(spend, 2),
            "reported_conversions": reported,
            "true_conversions": true_conv,
            "fake_conversions_counted": fake_counted,
            "true_value": round(true_value, 2),
            "reported_cpa": round(reported_cpa, 2),
            "true_cpa": round(true_cpa, 2),
            "cpa_distortion": distortion,
            "true_roas": round(roas, 3),
        },
        "budget_shift": {
            "zero_value_share_round1": round(bot_share_r1, 4),
            "zero_value_share_round2": round(bot_share_r2, 4),
            "delta_points": round((bot_share_r2 - bot_share_r1) * 100, 1),
        },
        "verdict": verdict,
        "vectors": TAMPER_VECTORS,
        "notes": [
            "Round 1 explores with even delivery; round 2 reallocates by the learned bid.",
            "Reported conversions include fake conversions that survived dedup.",
            "True CPA divides the same spend by conversions that produced revenue.",
            "This is a model, not a measurement of any live account.",
        ],
    }

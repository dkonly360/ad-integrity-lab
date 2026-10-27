"""Landing-page policy scanner.

Produces, per restricted vertical:
  - a weighted risk score from the rendered page
  - which of the three evidence channels can still see the trigger terms
  - the actual policy gate (identity/documentation vs text classifier)

The channel matrix is the interesting output: it answers "if I hid this from
the text classifier, what would still catch me?"
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .fetch import Capture, cloaking_report
from .rules import VERTICALS, Vertical

CHANNELS = ("dom", "ocr", "server")


@dataclass
class TermHit:
    term: str
    count: int
    weight: int
    where: str


@dataclass
class VerticalResult:
    id: str
    name: str
    policy: str
    gate: str
    cert: str
    docs: list[str]
    score: int = 0
    risk: str = "clear"
    hits: list[TermHit] = field(default_factory=list)
    channels: dict[str, bool] = field(default_factory=dict)
    caught_by: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    url: str
    final_url: str
    status: int
    title: str
    meta: dict
    primary: str | None
    verdict: str
    summary: str
    verticals: list[VerticalResult]
    channel_chars: dict[str, int]
    cloaking: dict
    headings: list[dict]
    screenshot: str
    notes: list[str]
    recommended_gate_actions: list[str]


def _segments(cap: Capture) -> list[tuple[str, int, str]]:
    """(text, weight, label) — where a term appears decides how badly it counts."""
    segs: list[tuple[str, int, str]] = []
    meta = cap.meta or {}
    if meta.get("title"):
        segs.append((meta["title"], 3, "meta title"))
    if meta.get("description"):
        segs.append((meta["description"], 3, "meta description"))
    if meta.get("og:description"):
        segs.append((meta["og:description"], 2, "og:description"))

    seen_h1 = False
    for h in cap.headings or []:
        if h["tag"] == "h1" and not seen_h1:
            segs.append((h["text"], 3, "h1"))
            seen_h1 = True
        elif h["tag"] == "h1":
            segs.append((h["text"], 2, "h1"))
        else:
            segs.append((h["text"], 2, h["tag"]))
    segs.append((cap.dom_text, 1, "body"))
    return segs


def _count(term: str, text: str) -> int:
    """Whole-word match only; 'apr' must not fire on 'april'."""
    if not text or not term:
        return 0
    pat = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
    return min(len(re.findall(pat, text, re.IGNORECASE)), 4)


def _score_vertical(v: Vertical, segs: list[tuple[str, int, str]]) -> tuple[int, list[TermHit]]:
    hits: dict[str, TermHit] = {}
    score = 0
    for term in v.keywords:
        is_strong = term in v.strong_terms
        for text, weight, where in segs:
            c = _count(term, text)
            if not c:
                continue
            eff_w = weight + (1 if is_strong else 0)
            add = eff_w * c
            score += add
            key = term
            if key in hits:
                hits[key].count += c
                hits[key].weight += add
                if where not in hits[key].where:
                    hits[key].where += f", {where}"
            else:
                hits[key] = TermHit(term=term, count=c, weight=add, where=where)
    ordered = sorted(hits.values(), key=lambda h: h.weight, reverse=True)[:12]
    return score, ordered


def _risk_band(score: int) -> str:
    if score >= 55:
        return "high"
    if score >= 28:
        return "elevated"
    if score >= 12:
        return "possible"
    return "clear"


def scan(cap: Capture) -> ScanResult:
    segs = _segments(cap)
    dom_text = cap.dom_text
    ocr_text = cap.ocr_text
    server_text = cap.server_text

    results: list[VerticalResult] = []
    for v in VERTICALS:
        score, hits = _score_vertical(v, segs)
        if score == 0:
            continue
        channels = {}
        for ch, text in (("dom", dom_text), ("ocr", ocr_text), ("server", server_text)):
            channels[ch] = any(_count(t, text) > 0 for t in v.strong_terms)
        caught = [c for c, ok in channels.items() if ok]
        results.append(VerticalResult(
            id=v.id, name=v.name, policy=v.policy, gate=v.gate, cert=v.cert,
            docs=list(v.docs), score=score, risk=_risk_band(score),
            hits=hits, channels=channels, caught_by=caught,
        ))

    results.sort(key=lambda r: r.score, reverse=True)
    primary = results[0] if results and results[0].score >= 12 else None

    if primary is None:
        verdict = "no restricted vertical detected"
        summary = (
            "No restricted-category vocabulary crossed the review threshold on this page. "
            "That lowers text-classifier risk but does not replace advertiser verification."
        )
    else:
        band = primary.risk
        verdict = f"{primary.name} — {band} text risk"
        ocr_ok = primary.channels.get("ocr", False)
        summary = (
            f"Page reads as '{primary.name}' ({primary.policy}). "
            f"Trigger vocabulary still visible to a vision reviewer: "
            f"{'yes' if ocr_ok else 'not in the captured viewport'}. "
            f"Real gatekeeper: {primary.gate}."
        )
        if len(results) > 1 and results[1].score >= 12:
            summary += f" Secondary signal: {results[1].name}."

    gate_actions: list[str] = []
    if primary:
        gate_actions = [
            "Collect the documentation gate first — this is what causes most disapprovals in this vertical.",
            f"Certification position: {primary.cert}.",
            "Do not change wording to dodge review; the same page must stay compliant for users.",
        ]
        for i, d in enumerate(primary.docs[:4], 1):
            gate_actions.append(f"Prepare doc {i}: {d}")

    res = ScanResult(
        url=cap.url,
        final_url=cap.final_url or cap.url,
        status=cap.status,
        title=(cap.meta or {}).get("title", ""),
        meta=cap.meta or {},
        primary=primary.name if primary else None,
        verdict=verdict,
        summary=summary,
        verticals=results[:5],
        channel_chars={
            "dom": len(dom_text or ""),
            "ocr": len(ocr_text or ""),
            "server": len(server_text or ""),
        },
        cloaking=cloaking_report(cap),
        headings=(cap.headings or [])[:12],
        screenshot=cap.screenshot,
        notes=cap.notes,
        recommended_gate_actions=gate_actions,
    )
    return res


def scan_dict(cap: Capture) -> dict:
    r = scan(cap)
    d = asdict(r)
    if d.get("screenshot"):
        from pathlib import Path
        d["screenshot_name"] = Path(d["screenshot"]).name
    return d

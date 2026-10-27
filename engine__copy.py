"""Ad-copy engine: restricted-term rewrites + full Responsive Search Ad sets.

Two hard rules are enforced mechanically, because they are what actually
gets assets rejected:

  headline    <= 30 characters
  description <= 90 characters

Rewrites are graded by *risk*, not just by whether the term changed. A rewrite
that drifts away from what the landing page literally claims converts a
disapproval problem into a misrepresentation problem, which is worse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .rules import AD_RULES, GLOBAL_SUBS, SUBSTITUTIONS, VERTICAL_INDEX

HEADLINE_MAX = 30
DESCRIPTION_MAX = 90
PATH_MAX = 15

RISK_ORDER = {"low": 1, "medium": 2, "high": 3}


@dataclass
class Applied:
    pattern: str
    replacement: str
    risk: str
    why: str
    count: int


@dataclass
class Lint:
    rule: str
    label: str
    severity: str
    why: str
    matches: list[str]


@dataclass
class Asset:
    text: str
    length: int
    limit: int
    ok: bool


@dataclass
class CopyResult:
    original: str
    rewritten: str
    applied: list[Applied]
    risk: str
    lint_before: list[Lint]
    lint_after: list[Lint]
    warnings: list[str] = field(default_factory=list)


def lint_text(text: str) -> list[Lint]:
    out: list[Lint] = []
    for rule in AD_RULES:
        found = re.findall(rule.pattern, text or "", re.IGNORECASE)
        if found:
            flat = [f if isinstance(f, str) else " ".join(x for x in f if x) for f in found]
            out.append(Lint(rule=rule.id, label=rule.label, severity=rule.severity,
                            why=rule.why, matches=flat[:5]))
    return out


def apply_substitutions(text: str, vertical_id: str) -> tuple[str, list[Applied]]:
    subs = list(SUBSTITUTIONS.get(vertical_id, []))
    applied: list[Applied] = []
    out = text or ""
    for s in subs:
        new, n = re.subn(s.pattern, s.to, out, flags=re.IGNORECASE)
        if n:
            out = new
            applied.append(Applied(pattern=_human(s.pattern), replacement=s.to,
                                   risk=s.risk, why=s.why, count=n))
    # Ad-level claims (guarantees, superlatives) apply to every vertical.
    for s in GLOBAL_SUBS:
        new, n = re.subn(s.pattern, s.to, out, flags=re.IGNORECASE)
        if n:
            out = new
            applied.append(Applied(pattern=_human(s.pattern), replacement=s.to or "(removed)",
                                   risk=s.risk, why=s.why, count=n))
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    if out[:1].islower():
        out = out[0].upper() + out[1:]
    return out, applied


def _human(pattern: str) -> str:
    """Turn a regex fragment into a readable label for the UI."""
    p = pattern
    p = p.replace("\\b", "").replace("\\s?", " ").replace("\\s", " ")
    p = p.replace("(?:", "(").replace("(", "").replace(")", "")
    p = p.replace("?", "").replace("\\", "")
    p = re.sub(r"\s+", " ", p).replace("|", " / ")
    return p.strip(" /")


def rewrite(text: str, vertical_id: str) -> CopyResult:
    vertical_id = vertical_id if vertical_id in SUBSTITUTIONS else "immigration"
    rewritten, applied = apply_substitutions(text, vertical_id)
    risk = "low"
    for a in applied:
        if RISK_ORDER[a.risk] > RISK_ORDER[risk]:
            risk = a.risk

    before = lint_text(text)
    after = lint_text(rewritten)

    warnings: list[str] = []
    if not applied:
        warnings.append(
            "No restricted term matched. If this asset was disapproved, the cause is probably "
            "the account/documentation gate, not the wording."
        )
    if risk == "high":
        warnings.append(
            "This rewrite moves the claim materially. The landing page must make the same claim, "
            "or the ad becomes a misrepresentation violation."
        )
    if any(l.severity == "high" for l in after):
        warnings.append("High-severity lint rules still fire after rewriting — fix those before submitting.")

    return CopyResult(original=text, rewritten=rewritten, applied=applied, risk=risk,
                      lint_before=before, lint_after=after, warnings=warnings)


# --------------------------------------------------------------------------
# Responsive Search Ad generation
# --------------------------------------------------------------------------

HEADLINE_TEMPLATES: dict[str, list[str]] = {
    "immigration": [
        "{Product} in {Geo}", "{Product} Online", "Apply for {Product}", "{Product} Support",
        "Check Your Eligibility", "Fast Document Review", "{Brand} {Product}", "Start Your Application",
        "Track Your Application", "No Hidden Fees", "Human Review Included", "{Geo} Entry Guidance",
        "Apply Online in Minutes", "{Product} Paperwork, Done", "Support in Your Language",
    ],
    "crypto": [
        "{Product} Platform", "Open an Account", "Buy {Product} Safely", "Regulated {Brand}",
        "Low-Fee Trading", "Verify in Minutes", "{Product} in {Geo}", "Start With a Demo",
        "Risk Disclosure Provided", "Licensed Exchange", "Secure Custody", "24/7 Markets",
        "{Brand} Exchange", "Trade With Limits", "Read the Risk Policy",
    ],
    "gambling": [
        "{Brand} Official", "{Product} in {Geo}", "Licensed Operator", "Join in Minutes",
        "T&Cs Apply", "18+ Only", "Responsible Play Tools", "Deposit Limits Included",
        "{Product} Bonus", "Live {Product} Tables", "Withdraw 24/7", "Play on Mobile",
        "{Brand} Sportsbook", "Check Licence Details", "Support Available 24/7",
    ],
    "finance": [
        "{Product} in {Geo}", "Check Eligibility", "Compare {Product} Options", "{Brand} {Product}",
        "Representative Example", "Decision in Minutes", "No Impact to Check", "Fixed Monthly Payment",
        "Read the Full Cost", "Licensed Provider", "Apply Securely Online", "Transparent Terms",
        "{Product} Calculator", "See Your Options", "Support From Real People",
    ],
    "health": [
        "{Product} Online", "Licensed {Product} Clinic", "{Brand} {Product}", "Book a Consultation",
        "Prescription Required", "Talk to a Clinician", "Discreet Delivery", "Verified Pharmacy",
        "Read the Patient Info", "Same-Day Appointments", "{Product} in {Geo}", "Follow-Up Included",
        "Check Your Suitability", "No Prescription, No Order", "Ask a Pharmacist",
    ],
    "adult": [
        "{Brand} Dating", "Meet & Connect", "18+ Community", "Verified Profiles",
        "Free to Join", "{Product} in {Geo}", "Privacy Controls", "Report & Block Tools",
        "Members Near You", "Safe Messaging", "Photo Verification Included", "Cancel Anytime",
        "Start Chatting Today", "Read Our Safety Guide", "Join Free",
    ],
    "tobacco": [
        "{Product} Alternatives", "18+ Only", "Licensed Retailer", "Order Online",
        "Age Verification Required", "{Brand} {Product}", "Nicotine Warning Shown", "Fast Delivery",
        "Check Local Rules", "Buy in {Geo}", "Read the Health Notice", "Support to Quit",
        "Responsible Retail", "Track Your Order", "Restrictions Apply",
    ],
    "essay": [
        "{Product} Guidance", "Editing & Feedback", "{Brand} Support", "Plagiarism Report Included",
        "Talk to a Tutor", "Structure & Citation Help", "Study Support Online", "Original Work Policy",
        "{Product} in {Geo}", "Book a Session", "Sample Chapters", "Formatting Assistance",
        "Unlimited Revisions", "Read Our Policy", "Support in Your Language",
    ],
}

DESCRIPTION_TEMPLATES: dict[str, list[str]] = {
    "immigration": [
        "Prepare and submit your {Product} application with a document check at every step.",
        "We review your paperwork before submission and track the application with you.",
        "Clear pricing with government fees shown separately. Not affiliated with any government.",
        "Start your {Geo} application online and get support in your language, 24/7.",
    ],
    "crypto": [
        "Open an account in minutes after identity verification. Read the risk disclosure first.",
        "Licensed in supported markets with published fees and segregated client funds.",
        "Trade with limits and a demo account. Crypto is volatile and you can lose money.",
        "Local rules apply in {Geo}. Check eligibility before you deposit.",
    ],
    "gambling": [
        "Licensed operator with published licence details. 18+ only, T&Cs apply.",
        "Set deposit limits and take breaks with built-in responsible play tools.",
        "Play on mobile with 24/7 support and transparent withdrawal times.",
        "New players welcome in eligible regions. Check local restrictions first.",
    ],
    "finance": [
        "Check your options in minutes without a hard credit search. Representative example shown.",
        "Fixed monthly payments and the full cost of credit shown before you apply.",
        "A licensed provider in {Geo}. Missed payments can affect your credit file.",
        "Apply securely online and speak to a real person if you need help.",
    ],
    "health": [
        "Talk to a licensed clinician and order only with a valid prescription.",
        "Verified pharmacy with discreet delivery and published patient information.",
        "Check your suitability first. Treatment is not right for everyone.",
        "Same-day consultations in supported regions with follow-up included.",
    ],
    "adult": [
        "Meet verified members near you. 18+ only, with report and block tools built in.",
        "Free to join with privacy controls and no hidden charges.",
        "Photo verification keeps the community real. Read our safety guide first.",
        "Cancel anytime and control who can message you.",
    ],
    "tobacco": [
        "Age verification required at checkout. 18+ only, restrictions apply.",
        "A licensed retailer in supported regions with health warnings on every order.",
        "Order online with tracked delivery and a clear nicotine warning.",
        "Check local rules before ordering. Free support is available to quit.",
    ],
    "essay": [
        "Get structure and citation feedback from tutors, not pre-written work.",
        "Every session includes an originality check and formatting help.",
        "Book a session online and keep the work in your own words.",
        "Support in your language with unlimited revisions included.",
    ],
}


def _fit(text: str, limit: int) -> Asset:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        if " " in cut:
            cut = cut[:cut.rfind(" ")]
        text = cut.rstrip(" ,.-")
    return Asset(text=text, length=len(text), limit=limit, ok=len(text) <= limit)


def build_rsa(vertical_id: str, geo: str = "", brand: str = "", product: str = "") -> dict:
    vertical_id = vertical_id if vertical_id in HEADLINE_TEMPLATES else "immigration"
    v = VERTICAL_INDEX.get(vertical_id)
    geo = (geo or "").strip() or "your country"
    brand = (brand or "").strip() or "Our service"

    if not product:
        product_map = {
            "immigration": "Entry Permit", "crypto": "Crypto", "gambling": "Casino",
            "finance": "Loan", "health": "Treatment", "adult": "Dating", "tobacco": "Vape",
            "essay": "Essay Help",
        }
        product = product_map.get(vertical_id, "Service")

    headlines = []
    for t in HEADLINE_TEMPLATES[vertical_id]:
        raw = t.format(Product=product, Geo=geo, Brand=brand)
        headlines.append(_fit(raw, HEADLINE_MAX))
    # de-duplicate while keeping order
    seen, uniq = set(), []
    for h in headlines:
        if h.text.lower() in seen:
            continue
        seen.add(h.text.lower())
        uniq.append(h)
    headlines = uniq[:15]
    while len(headlines) < 15:
        headlines.append(_fit(f"{product} Help {len(headlines)+1}", HEADLINE_MAX))

    descriptions = [_fit(t.format(Product=product, Geo=geo, Brand=brand), DESCRIPTION_MAX)
                    for t in DESCRIPTION_TEMPLATES[vertical_id]][:4]
    while len(descriptions) < 4:
        descriptions.append(_fit("Apply online and get clear, upfront information.", DESCRIPTION_MAX))

    paths = [_fit(product, PATH_MAX), _fit(geo, PATH_MAX)]

    lint_all = []
    for i, h in enumerate(headlines):
        for l in lint_text(h.text):
            lint_all.append({"asset": f"headline {i+1}", "text": h.text,
                             "rule": l.label, "severity": l.severity})
    for i, d in enumerate(descriptions):
        for l in lint_text(d.text):
            lint_all.append({"asset": f"description {i+1}", "text": d.text,
                             "rule": l.label, "severity": l.severity})

    return {
        "vertical": vertical_id,
        "vertical_name": v.name if v else vertical_id,
        "policy": v.policy if v else "",
        "gate": v.gate if v else "",
        "cert": v.cert if v else "",
        "headlines": [h.__dict__ for h in headlines],
        "descriptions": [d.__dict__ for d in descriptions],
        "paths": [p.__dict__ for p in paths],
        "pinning": [
            "Pin headline 1 to position 1 (product clarity).",
            "Pin the disclaimer headline to position 2 only if policy requires it.",
            "Leave remaining 13 headlines unpinned so the system can learn.",
        ],
        "lint": lint_all,
        "checklist": _checklist(vertical_id),
    }


def _checklist(vertical_id: str) -> list[str]:
    base = [
        "Landing page shows legal business name, registered address and contact details.",
        "Ad wording and landing page make the same claim (no divergence).",
        "Final URL passes the lab's policy scan with no high-risk vertical mismatch.",
        "Advertiser identity and business-operation verification completed in the account.",
    ]
    extra = {
        "immigration": [
            "Landing page states clearly that the advertiser is not a government agency.",
            "Government fee and service fee are shown separately.",
        ],
        "crypto": ["Certification/licence submitted for every targeted country.",
                   "Risk warning visible above the fold."],
        "gambling": ["Licence number and issuing authority shown.",
                     "18+ notice and responsible-gambling links present."],
        "finance": ["Representative APR and a full cost example on the page.",
                    "Licence or registration number displayed."],
        "health": ["Prescription requirement stated where applicable.",
                   "Claim substantiation available for any clinical wording."],
    }
    return base + extra.get(vertical_id, [])

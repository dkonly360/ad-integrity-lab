"""Policy knowledge base for the Ad Integrity Lab.

Every vertical here maps to a real Google Ads policy gate:
  - `cert`      : certification / licence the advertiser must hold
  - `gate`      : what actually causes disapproval (identity vs text)
  - `docs`      : the policy surface a reviewer looks at

Weights: title/h1 = 3, meta/h2-h3 = 2, body = 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Vertical:
    id: str
    name: str
    policy: str
    gate: str
    cert: str
    keywords: list[str] = field(default_factory=list)
    strong_terms: list[str] = field(default_factory=list)  # near-certain signals
    docs: list[str] = field(default_factory=list)


VERTICALS: list[Vertical] = [
    Vertical(
        id="immigration",
        name="Immigration & visa services",
        policy="Government documents & official services / Immigration",
        gate="Business-operation verification + 'not affiliated with government' disclosure",
        cert="Country-specific (licensed consultant in some geos); no global certification",
        keywords=[
            "visa", "evisa", "e-visa", "immigration", "work permit", "residence permit",
            "green card", "permanent residency", "passport", "entry permit", "travel authorisation",
            "travel authorization", "embassy", "consulate", "mofa", "sponsorship", "iqama",
            "golden visa", "digital nomad visa", "schengen", "umrah", "hajj",
        ],
        strong_terms=["visa", "immigration", "green card", "work permit", "residency"],
        docs=[
            "Business registration / trade licence",
            "Government affiliation disclaimer on landing page",
            "Fee breakdown (service fee vs government fee)",
            "Refund policy",
        ],
    ),
    Vertical(
        id="crypto",
        name="Cryptocurrency & blockchain",
        policy="Financial products & services / Cryptocurrency",
        gate="Certification required in most countries + licensing proof",
        cert="Required (country-scoped), e.g. VASP/MiCA/FinCEN registration",
        keywords=[
            "crypto", "cryptocurrency", "bitcoin", "btc", "ethereum", "altcoin", "token",
            "blockchain", "defi", "staking", "nft", "web3", "exchange", "wallet", "mining",
            "yield farming", "presale", "airdrop",
        ],
        strong_terms=["crypto", "bitcoin", "token presale", "staking", "defi"],
        docs=["Exchange licence", "Registration number", "Risk disclosure"],
    ),
    Vertical(
        id="gambling",
        name="Gambling & betting",
        policy="Gambling and games",
        gate="Certification + licensed operator in the targeted geography",
        cert="Required (per-country licence)",
        keywords=[
            "casino", "betting", "sportsbook", "poker", "slots", "roulette", "blackjack",
            "lottery", "bookmaker", "odds", "jackpot", "free spins", "wager", "bingo",
        ],
        strong_terms=["casino", "betting", "sportsbook", "slots", "jackpot"],
        docs=["Gambling licence number", "Responsible-gaming statement", "Geo restrictions"],
    ),
    Vertical(
        id="finance",
        name="Consumer finance & credit",
        policy="Financial products & services / Credit & loans",
        gate="Advertiser verification + disclosure of APR / representative example",
        cert="Verification only; some geos add licensing",
        keywords=[
            "loan", "loans", "credit", "credit card", "mortgage", "refinance", "debt",
            "payday", "installment", "instalment", "bnpl", "interest rate", "apr",
            "credit repair", "bail bond", "insurance", "trading platform", "forex",
        ],
        strong_terms=["loan", "payday", "credit repair", "apr", "refinance"],
        docs=["Licence / registration", "Representative APR", "Full cost example"],
    ),
    Vertical(
        id="health",
        name="Healthcare, pharma & supplements",
        policy="Healthcare & medicines",
        gate="Certification for prescription drugs + claim substantiation",
        cert="Required for Rx/online pharmacy; claims need evidence",
        keywords=[
            "pharmacy", "prescription", "medicine", "tablet", "capsule", "supplement",
            "weight loss", "clinic", "treatment", "therapy", "dentist", "implants",
            "ozempic", "semaglutide", "viagra", "minoxidil", "cure", "clinically proven",
        ],
        strong_terms=["prescription", "pharmacy", "clinically proven", "weight loss"],
        docs=["Licence", "Prescription requirement statement", "Claim studies"],
    ),
    Vertical(
        id="adult",
        name="Adult content & dating",
        policy="Adult content",
        gate="Geo-scoped allowance only; explicit content restricted",
        cert="Not applicable",
        keywords=["dating", "hookup", "escort", "adult", "xxx", "onlyfans", "sugar", "cam"],
        strong_terms=["escort", "xxx", "adult content"],
        docs=["Age gate", "Content rating"],
    ),
    Vertical(
        id="tobacco",
        name="Tobacco, vape & alcohol",
        policy="Tobacco products / Alcohol",
        gate="Prohibited or heavily restricted by geography",
        cert="Geo-dependent",
        keywords=["vape", "vaping", "e-cigarette", "nicotine", "pouch", "cigarette", "shisha",
                  "alcohol", "whisky", "vodka", "beer delivery", "liquor"],
        strong_terms=["vape", "nicotine", "e-cigarette"],
        docs=["Geo eligibility", "Health warning"],
    ),
    Vertical(
        id="essay",
        name="Academic / ghost-writing services",
        policy="Education & assignment services (restricted in several geos)",
        gate="Prohibited in some markets; 'assignment help' style claims",
        cert="Not applicable",
        keywords=["assignment help", "essay writing", "thesis", "dissertation", "do my homework",
                  "ghostwriting", "coursework help"],
        strong_terms=["assignment help", "do my homework", "essay writing service"],
        docs=["Scope of service", "Originality statement"],
    ),
]

VERTICAL_INDEX = {v.id: v for v in VERTICALS}


# --------------------------------------------------------------------------
# Ad-text lint rules (applies to headlines / descriptions, not the landing page)
# --------------------------------------------------------------------------

@dataclass
class AdRule:
    id: str
    label: str
    pattern: str
    severity: str
    why: str


AD_RULES: list[AdRule] = [
    AdRule("superlative", "Unsubstantiated superlative",
           r"\b(best|no\.?\s?1|number one|#1|world'?s\s+(best|leading)|cheapest|fastest|guaranteed)\b",
           "high",
           "Superlatives need proof; reviewers ask for substantiation."),
    AdRule("guarantee", "Guarantee / certainty claim",
           r"\b(guaranteed|100%|assured approval|sure shot|definitely)\b",
           "high",
           "Approval outcomes are not the advertiser's to guarantee."),
    AdRule("govt_affiliation", "Implied government affiliation",
           r"\b(official|government|govt|ministry|embassy|consulate)\s+(portal|website|site|approved|partner)\b",
           "high",
           "Must disclose non-affiliation for government-document services."),
    AdRule("urgency", "Artificial urgency",
           r"\b(limited time|today only|last chance|hurry|act now|expires today)\b",
           "low",
           "Allowed but flagged by review when repeated across assets."),
    AdRule("allcaps", "ALL CAPS asset",
           r"^[A-Z0-9\s\W]{12,}$",
           "low",
           "Caps-heavy assets read as shouty; also a cheap spam signal."),
    AdRule("bangs", "Repeated exclamation marks",
           r"[!?]{2,}",
           "low",
           "Multiple punctuation is a known low-quality ad signal."),
    AdRule("phone", "Phone number inside ad text",
           r"(\+?\d[\d\s\-()]{7,}\d)",
           "medium",
           "Phone numbers belong in call assets, not headline text."),
    AdRule("price", "Unqualified price claim",
           r"(?:[£$€₹]\s?\d+|\d+\s?(?:usd|aed|eur|gbp|pkr|inr))\b",
           "medium",
           "Price claims must match the landing page or it becomes misrepresentation."),
]


# --------------------------------------------------------------------------
# Substitution maps: restricted term -> compliance-safe wording.
# `risk` = how far the new wording drifts from the landing page's literal claim.
# --------------------------------------------------------------------------

@dataclass
class Sub:
    pattern: str
    to: str
    risk: str
    why: str


SUBSTITUTIONS: dict[str, list[Sub]] = {
    "immigration": [
        Sub(r"\bvisa services?\b", "entry permit support", "low",
            "Describes the document work without claiming to issue the visa."),
        Sub(r"\bvisa\b", "entry permit", "low",
            "Same product, softer trigger term."),
        Sub(r"\be-?visa\b", "online entry permit", "low", "Literal equivalent."),
        Sub(r"\bimmigration services?\b", "relocation & documentation support", "medium",
            "Drops the regulated-service label; keep a matching section on the page."),
        Sub(r"\bguaranteed approval\b", "eligibility checked before you pay", "low",
            "Removes the guarantee, keeps the promise concrete."),
        Sub(r"\b100% approval\b", "documents reviewed before submission", "low",
            "Process claim instead of outcome claim."),
        Sub(r"\bwe (?:get|obtain) your visa\b", "we prepare and submit your application", "low",
            "Accurate scope of service."),
        Sub(r"\bgreen card\b", "permanent residence application", "low", "Formal equivalent."),
        Sub(r"\bwork permit\b", "employment entry permit", "low", "Formal equivalent."),
        Sub(r"\bofficial (?:visa )?portal\b", "application support desk", "high",
            "Only safe if the page states it is not government-affiliated."),
    ],
    "crypto": [
        Sub(r"\binvest(?:ment)?\b", "participation", "medium", "Softer financial-invitation wording."),
        Sub(r"\bguaranteed returns?\b", "targeted strategy", "low", "Removes the return promise."),
        Sub(r"\bprofit\b", "performance", "low", "Neutral term."),
        Sub(r"\bpresale\b", "early access round", "low", "Equivalent, less spammy."),
    ],
    "gambling": [
        Sub(r"\bfree spins\b", "bonus rounds", "low", "Same mechanic."),
        Sub(r"\bjackpot\b", "prize pool", "low", "Equivalent."),
        Sub(r"\bbetting\b", "match predictions", "medium", "Only if the page supports it."),
    ],
    "finance": [
        Sub(r"\binstant approval\b", "decision in minutes", "low", "Process, not outcome."),
        Sub(r"\bno interest\b", "0% introductory period", "low", "Precise version."),
        Sub(r"\bguaranteed loan\b", "eligibility check", "low", "Removes guarantee."),
        Sub(r"\bdebt relief\b", "repayment planning support", "medium", "Narrower claim."),
    ],
    "health": [
        Sub(r"\bcures?\b", "supports", "medium", "Avoids a medical claim; page must match."),
        Sub(r"\bclinically proven\b", "clinically studied", "low", "Only with a study reference."),
        Sub(r"\blose \d+\s?kg\b", "reach your target weight", "medium", "Removes a numeric promise."),
    ],
    "tobacco": [
        Sub(r"\bvape\b", "nicotine alternative device", "medium", "Compliance wording."),
        Sub(r"\be-?cigarette\b", "nicotine alternative device", "medium", "Compliance wording."),
    ],
    "essay": [
        Sub(r"\bwrite your essay\b", "essay guidance & editing", "medium",
            "Shifts from ghost-writing to support."),
        Sub(r"\bdo my homework\b", "homework support", "low", "Equivalent, softer."),
    ],
    "adult": [
        Sub(r"\bhookup\b", "meet & connect", "low", "Softer."),
        Sub(r"\bescort\b", "companion service", "low", "Equivalent."),
    ],
}


# Applied to every vertical: these are ad-level claims, not category terms.
GLOBAL_SUBS: list[Sub] = [
    Sub(r"\b100%\s?(approval|success|guaranteed)\b", "documents reviewed first", "low",
        "Removes an outcome guarantee that cannot be substantiated."),
    # Dropping the word keeps the sentence grammatical wherever it sits
    # ("Guaranteed visa approval" -> "entry permit approval").
    Sub(r"\bguaranteed\b\s*", "", "low",
        "Guarantee language needs proof; the claim stands without it."),
    Sub(r"\bno\.?\s?1\b", "a trusted choice", "medium",
        "Ranking claims need third-party evidence."),
    Sub(r"\b(best|cheapest|fastest)\b", "a practical", "medium",
        "Superlatives invite substantiation requests."),
]


def compile_all(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns]

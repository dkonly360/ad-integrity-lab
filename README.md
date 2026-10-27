# Ad Integrity Lab

A working, publicly hosted lab that tests two things an ad-integrity team actually
argues about:

1. **Can a landing page hide restricted content from an ad review system?**
2. **What happens to automated bidding when conversion signals are poisoned?**

The first question is answered with a real build and a measurement, not an opinion.
The second is answered with an inspectable simulation. Both are reproducible from
the UI, and every run is written to an experiment log with its inputs and outcome.

---

## Live

```
https://bubble-object-diff-sys.trycloudflare.com
```

The tunnel is an ephemeral Cloudflare quick tunnel tied to the host that started it.
For a durable deployment use the Dockerfile below on any small VM (or Fly.io /
Render / Cloud Run). Nothing in the app depends on the tunnel.

---

## Modules

| Module | What it does | Output |
|---|---|---|
| **Policy scan** | Renders a landing page in headless Chromium, scores it against eight restricted verticals, weights terms by location (title/H1 ×3, H2/H3 ×2, body ×1), and reports which of three evidence channels can still see the trigger words. Also diffs the page served to a normal browser against the page served to a Googlebot user-agent. | verdict, risk bands, trigger table, cloaking diff, documentation-gate checklist |
| **Canvas lab** | Builds two real canvas-only clones of the page — pixels replayed into a `<canvas>`, and every text node redrawn with its computed font then deleted from the DOM — then re-measures all three channels on the clone. | measurable before/after, downloadable artifacts, verdict |
| **Ad copy** | Rewrites restricted vocabulary into compliant wording with a *drift* grade, lints both versions, and generates a full RSA set (15 headlines ≤30 chars, 4 descriptions ≤90 chars, 2 paths) plus a pre-submission checklist. | rewrite table + lint + keyword-sized assets |
| **Conversions** | Two-round target-CPA simulation. Round 1 explores, round 2 reallocates by the learned bid. Fake conversions are injected at a chosen rate and optionally removed by verification. | reported vs true CPA, distortion ratio, ROAS, budget shift, tamper vectors with tells and fixes |

---

## The three evidence channels

Every capture produces four artifacts, because "who sees what" is the whole problem:

| Channel | How it is produced | What it represents |
|---|---|---|
| **Server** | `httpx` GET with a Googlebot user-agent, no JS | what a raw fetch sees |
| **DOM** | Chromium `--dump-dom` after JS + `data:` nothing | what a rendered crawl sees |
| **OCR** | Chromium screenshot → Tesseract | what a vision reviewer sees |

A technique can win the first two and still lose the third. That is the finding.

---

## Measured result (canvas evasion)

Target: `https://www.ivisa.com/`, 2026-10-26. Trigger list: `visa, evisa, embassy, passport`.

| Metric | Original page | Image-mode canvas clone | Text-mode canvas clone |
|---|---|---|---|
| Visible DOM text | 6,874 chars | **0 chars** | 75 chars |
| Trigger terms found via DOM | 100% | **0%** | 40% |
| Trigger terms found via OCR | — | **60%** | 60% |
| OCR text volume | 1,031 chars | 1,031 chars | 1,031 chars |

Reading: the canvas technique **does** remove the text channel completely — that part
is real and it works. The review channel is untouched: the same restricted vocabulary
is still recovered from the pixels. Canvas buys evasion of one classifier and buys
nothing against a vision reviewer, while adding an independent
circumventing-systems violation.

---

## Measured result (conversion poisoning)

Same model, two configurations, 200k impressions, $45 target CPA, $20k budget:

| Configuration | Reported CPA | True CPA | Distortion | True ROAS | Zero-value budget share |
|---|---|---|---|---|---|
| Clean account | $16.24 | $16.24 | 1.00× | 5.90 | 5% |
| 0.4× fake conversions, no verification, count-based bidding | **$1.71** | **$39.94** | **23.37×** | 0.29 | **95%** |
| Same attack, 90% verification/dedup | $10.84 | $27.59 | 2.55× | 2.00 | 68% |
| Same attack, value-based bidding | $5.75 | $16.16 | 2.81× | 6.06 | **0%** |

Two product conclusions fall out of that table:

- **Verification is a tactical mitigation.** It reduces the damage but the model still
  rewards inventory that does not pay back, because a surviving fake conversion is
  indistinguishable from a cheap one.
- **Value-based bidding is the structural mitigation.** When the optimiser is anchored
  on value rather than counts, the zero-value cohort cannot win the auction even with
  a fully poisoned count signal. Budget share stays at 0%.

That is a defensible ranking of mitigations, which is the sort of thing this work
needs to produce.

---

## Run it locally

```bash
pip install -r requirements.txt
# Chromium and Tesseract must be on PATH (Debian: chromium, tesseract-ocr)
uvicorn main:app --host 0.0.0.0 --port 8080
```

Docker:

```bash
docker build -t ad-integrity-lab .
docker run -p 8080:8080 ad-integrity-lab
```

Smoke test (no network needed):

```bash
python3 tests/smoke.py
```

---

## Safety properties that are enforced in code

- `engine/fetch.py::assert_public_host` resolves every submitted hostname and refuses
  private, loopback, link-local, multicast and reserved addresses, plus cloud metadata
  names. A public deployment with arbitrary user URLs is otherwise an SSRF pivot.
- Only `http`/`https` and ports 80/443/8080/8443 are accepted; credentials in URLs are refused.
- One Chromium capture runs at a time (`asyncio.Semaphore(1)`), with a 3 MB transfer cap
  per response and 1 s spacing between the human and bot fetches to the same host.
- 12 job submissions per IP per 10 minutes.
- Clone artifacts are served with `Content-Security-Policy: sandbox allow-scripts`, so a
  captured page can never run in the lab's origin.
- Long tasks are jobs with polling, so a proxy never holds a 60 s request.

## Limits, stated plainly

- The canvas measurement is a **viewport crop**. OCR over a full-page render would
  recover more, not less, text; the reported survival rate is a floor, not a ceiling.
- The conversion module is a **model with declared parameters**, not a measurement of any
  live account. It is deliberately small enough to audit in one sitting.
- Policy categories are a curated subset (eight verticals, English vocabulary). It is a
  research instrument, not a compliance oracle.
- No advertisement is served and no account is ever submitted anywhere.

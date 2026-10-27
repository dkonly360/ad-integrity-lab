"""URL guard + dual-channel renderer + cloaking detector.

For every target the lab collects four evidence channels:

  raw_human : HTML served to a normal browser UA (no JS)
  raw_bot   : HTML served to Googlebot UA (no JS)   <- cloaking comparison
  rendered  : Chromium post-JS DOM                  <- what Google's renderer sees
  ocr       : screenshot through Tesseract          <- what a vision reviewer sees

A page can hide from `raw_*` and even from `rendered`, but `ocr` is the channel
that canvas tricks cannot touch. That asymmetry is the whole point of the lab.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from . import browser

ARTIFACT_DIR = Path(__file__).resolve().parent.parent / "artifacts"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

MAX_BYTES = 3_000_000
RENDER_TIMEOUT = 45
HTTP_TIMEOUT = 20.0

HUMAN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
BOT_UA = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; "
    "+http://www.google.com/bot.html)"
)

# Only one Chromium at a time: 4 vCPU box, and it keeps target load low.
_RENDER_LOCK = asyncio.Semaphore(1)


class UnsafeTarget(ValueError):
    """Raised when a URL points somewhere the lab must not fetch."""


BLOCKED_HOSTS = {
    "localhost", "localhost.localdomain", "ip6-localhost", "metadata",
    "metadata.google.internal", "instance-data",
}
BLOCKED_SUFFIX = (".local", ".internal", ".localdomain", ".home.arpa", ".lan")


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise UnsafeTarget("Empty URL.")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", raw):
        raw = "https://" + raw
    u = urlparse(raw)
    if u.scheme not in ("http", "https"):
        raise UnsafeTarget("Only http:// and https:// targets are allowed.")
    if not u.hostname:
        raise UnsafeTarget("URL has no hostname.")
    if u.username or u.password:
        raise UnsafeTarget("Credentials in URL are not accepted.")
    if u.port and u.port not in (80, 443, 8080, 8443):
        raise UnsafeTarget("Non-standard port refused by lab policy.")
    netloc = u.hostname + (f":{u.port}" if u.port else "")
    return urlunparse((u.scheme, netloc, u.path or "/", "", u.query, ""))


def assert_public_host(host: str) -> None:
    """Refuse private/reserved destinations.

    The lab is deployed publicly, so without this check it becomes an SSRF
    pivot into its own network and cloud metadata endpoints.
    """
    host_l = (host or "").lower().strip("[]")
    if not host_l:
        raise UnsafeTarget("Missing host.")
    if host_l in BLOCKED_HOSTS or any(host_l.endswith(s) for s in BLOCKED_SUFFIX):
        raise UnsafeTarget(f"Host '{host}' is not a public internet host.")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeTarget(f"Cannot resolve host '{host}'.") from exc

    seen = False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        seen = True
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise UnsafeTarget(
                f"Host '{host}' resolves to a non-public address ({addr})."
            )
        if ip.version == 6 and ip.ipv4_mapped and ip.ipv4_mapped.is_private:
            raise UnsafeTarget(f"Host '{host}' maps to a private IPv4 address.")
    if not seen:
        raise UnsafeTarget(f"No usable address for host '{host}'.")


def artifact_slug(url: str) -> str:
    h = hashlib.sha256(url.encode()).hexdigest()[:12]
    host = urlparse(url).hostname or "target"
    clean = re.sub(r"[^a-zA-Z0-9.\-]", "_", host)[:50]
    return f"{clean}_{h}"


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

def soup_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))


def extract_headings(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    out = []
    for tag in soup.find_all(re.compile(r"^h[1-3]$")):
        txt = tag.get_text(" ", strip=True)
        if txt:
            out.append({"tag": tag.name, "text": re.sub(r"\s+", " ", txt)[:300]})
    return out[:40]


def extract_meta(html: str) -> dict:
    soup = BeautifulSoup(html or "", "lxml")
    meta: dict = {}
    if soup.title and soup.title.string:
        meta["title"] = re.sub(r"\s+", " ", soup.title.string).strip()[:300]
    for name in ("description", "keywords"):
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            meta[name] = tag["content"].strip()[:500]
    og = soup.find("meta", attrs={"property": "og:description"})
    if og and og.get("content"):
        meta["og:description"] = og["content"].strip()[:500]
    return meta


def ocr_image(path: str | Path) -> str:
    try:
        res = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", "3"],
            capture_output=True, text=True, timeout=120,
        )
        return re.sub(r"\n{3,}", "\n\n", (res.stdout or "")).strip()
    except Exception as exc:
        return f"[ocr unavailable: {exc}]"


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------

@dataclass
class Capture:
    url: str
    final_url: str = ""
    status: int = 0
    raw_human: str = ""
    raw_bot: str = ""
    rendered: str = ""
    ocr_text: str = ""
    screenshot: str = ""
    headings: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    slugs: str = ""
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.rendered or self.raw_human)

    @property
    def dom_text(self) -> str:
        return soup_text(self.rendered or self.raw_human)

    @property
    def server_text(self) -> str:
        return soup_text(self.raw_bot or self.raw_human)


async def _http_get(url: str, ua: str, client: httpx.AsyncClient) -> tuple[str, int, str]:
    try:
        r = await client.get(url, headers={"User-Agent": ua}, follow_redirects=True)
        body = r.text if len(r.content) <= MAX_BYTES else r.text[:MAX_BYTES]
        return body, r.status_code, str(r.url)
    except Exception as exc:
        return "", 0, f"[fetch error: {type(exc).__name__}]"


def _chromium_render(url: str, slug: str) -> tuple[str, str, str]:
    """Render once in the shared browser: DOM and screenshot from one load."""
    shot = ARTIFACT_DIR / f"{slug}.png"
    try:
        return browser.render(url, shot, HUMAN_UA)
    except browser.BrowserUnavailable as exc:
        dom, path, err = _chromium_render_cli(url, slug)
        return dom, path, (f"shared browser unavailable ({exc}); " + err).strip("; ")


def _chromium_render_cli(url: str, slug: str) -> tuple[str, str, str]:
    """Fallback: two cold Chromium runs, one for the DOM, one for the screenshot."""
    profile = tempfile.mkdtemp(prefix="lab-chrome-")
    shot = ARTIFACT_DIR / f"{slug}.png"
    base = [
        "chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--hide-scrollbars", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions",
        "--disable-background-networking", "--disable-sync",
        f"--user-data-dir={profile}",
        "--window-size=1366,1400",
        "--virtual-time-budget=9000",
        f"--user-agent={HUMAN_UA}",
    ]
    dom, err = "", ""
    try:
        res = subprocess.run(
            base + ["--dump-dom", url],
            capture_output=True, text=True, timeout=RENDER_TIMEOUT,
        )
        dom = res.stdout or ""
        if not dom:
            err = (res.stderr or "")[-400:]
    except subprocess.TimeoutExpired:
        err = "render timeout"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"

    try:
        subprocess.run(
            base + [f"--screenshot={shot}", url],
            capture_output=True, text=True, timeout=RENDER_TIMEOUT,
        )
    except Exception as exc:
        err = (err + f" | screenshot: {type(exc).__name__}").strip(" |")
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    return dom, (str(shot) if shot.exists() else ""), err


async def capture(url: str, want_ocr: bool = True) -> Capture:
    cap = Capture(url=url, slugs=artifact_slug(url))
    host = urlparse(url).hostname or ""
    assert_public_host(host)

    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        verify=False,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        headers={"Accept-Language": "en-US,en;q=0.9"},
    ) as client:
        (cap.raw_human, st, final) = await _http_get(url, HUMAN_UA, client)
        cap.status = st
        cap.final_url = final if not final.startswith("[") else url
        await asyncio.sleep(1.0)  # be polite: one request per second to the same host
        cap.raw_bot, _, _ = await _http_get(url, BOT_UA, client)

    import warnings
    warnings.filterwarnings("ignore")

    async with _RENDER_LOCK:
        rendered, shot, err = await asyncio.to_thread(_chromium_render, url, cap.slugs)
    cap.rendered = rendered
    cap.screenshot = shot
    if err:
        cap.notes.append(f"render: {err[:200]}")
    if not cap.rendered:
        cap.rendered = cap.raw_human

    cap.headings = extract_headings(cap.rendered) or extract_headings(cap.raw_human)
    cap.meta = extract_meta(cap.rendered) or extract_meta(cap.raw_human)

    if want_ocr and cap.screenshot:
        cap.ocr_text = await asyncio.to_thread(ocr_image, cap.screenshot)

    if not cap.rendered and not cap.raw_human:
        cap.error = "Target returned no content over HTTP."
    return cap


def capture_sync(url: str, want_ocr: bool = True) -> Capture:
    return asyncio.run(capture(url, want_ocr=want_ocr))


# --------------------------------------------------------------------------
# cloaking detector
# --------------------------------------------------------------------------

_STOP = set("""
a an the and or of to in for on with your you our we is are be as at by from this that
it its all any can will just get more most other some such only own same so than too
very s t don now d ll m o re ve y ain aren couldn didn doesn hadn hasn haven isn ma
mightn mustn needn shan shouldn wasn weren won wouldn page home click here learn read
""".split())

MARKERS = ["casino", "betting", "slots", "jackpot", "free spins", "viagra", "crypto",
           "loan", "payday", "vape", "escort", "visa", "immigration", "essay"]


def token_set(text: str) -> set[str]:
    toks = re.findall(r"[a-z0-9]{3,}", (text or "").lower())
    return {t for t in toks if t not in _STOP}


def cloaking_report(cap: Capture) -> dict:
    """Compare what a bot and a human are served.

    A large divergence in the restricted-noun vocabulary is the signature of
    cloaking: the same URL, two different businesses.
    """
    h = token_set(soup_text(cap.raw_human))
    b = token_set(soup_text(cap.raw_bot))
    if not h and not b:
        return {"available": False, "reason": "no server-side HTML to compare"}

    only_bot = sorted(b - h)[:25]
    only_human = sorted(h - b)[:25]
    shared = len(h & b)
    union = len(h | b) or 1
    jaccard = round(shared / union, 3)

    def markers(tokens: set[str]) -> list[str]:
        return [m for m in MARKERS if m in tokens]

    human_marks = markers(h)
    bot_marks = markers(b)
    hidden_from_bot = [m for m in human_marks if m not in b]
    hidden_from_human = [m for m in bot_marks if m not in h]

    len_h, len_b = len(cap.raw_human or ""), len(cap.raw_bot or "")
    size_delta = round(abs(len_h - len_b) / max(len_h, len_b, 1), 3)

    suspicion = 0
    if hidden_from_bot:
        suspicion += 45 + 8 * len(hidden_from_bot)
    if hidden_from_human:
        suspicion += 15
    if jaccard < 0.55:
        suspicion += 25
    elif jaccard < 0.75:
        suspicion += 10
    if size_delta > 0.35:
        suspicion += 10
    suspicion = min(100, suspicion)

    if suspicion >= 55:
        verdict = "strong cloaking signal"
    elif suspicion >= 30:
        verdict = "weak/mixed signal — recheck before drawing conclusions"
    else:
        verdict = "consistent content (no cloaking signal)"

    return {
        "available": True,
        "human_tokens": len(h),
        "bot_tokens": len(b),
        "jaccard": jaccard,
        "bytes_human": len_h,
        "bytes_bot": len_b,
        "size_delta": size_delta,
        "only_bot": only_bot,
        "only_human": only_human,
        "human_markers": human_marks,
        "bot_markers": bot_marks,
        "hidden_from_bot": hidden_from_bot,
        "hidden_from_human": hidden_from_human,
        "suspicion": suspicion,
        "verdict": verdict,
    }

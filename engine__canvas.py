"""Canvas conversion + detectability measurement.

Two real flavours are produced from a captured page:

  image-mode : the rendered pixels are replayed into a <canvas>; all text nodes
               are stripped. Visually faithful, DOM text = 0.
  text-mode  : every text node is measured with a Range, redrawn onto an
               overlay canvas with the node's computed font, then deleted.
               Keeps sharp text and a near-identical layout.

Both defeat a DOM/text classifier by construction. Neither defeats OCR, and
measuring that is the point of the module — the lab reports the delta instead
of asserting a bypass.
"""

from __future__ import annotations

import base64
import html as html_mod
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from .fetch import ARTIFACT_DIR, HUMAN_UA, Capture, ocr_image

RENDER_TIMEOUT = 45


# --------------------------------------------------------------------------
# client-side text-node canvasifier (works on any page it is injected into)
# --------------------------------------------------------------------------

CANVASIFY_JS = r"""
(function canvasify(){
  if (window.__cvDone) return; window.__cvDone = true;
  var body = document.body;
  function tag(k,v){ try{ body.setAttribute(k,String(v)); }catch(e){} }
  try {
  var dpr = Math.min(window.devicePixelRatio || 1, 2);
  var W = Math.max(document.documentElement.scrollWidth, body.scrollWidth);
  var H = Math.max(document.documentElement.scrollHeight, body.scrollHeight);
  // Chromium refuses canvases past ~16k px per side and ~256MP total.
  var MAXSIDE = 16000, MAXAREA = 100000000;
  W = Math.min(W, MAXSIDE); H = Math.min(H, MAXSIDE);
  if (W*H*dpr*dpr > MAXAREA) { dpr = Math.max(1, Math.sqrt(MAXAREA/(W*H))); }
  var cv = document.createElement('canvas');
  cv.width = Math.max(1, Math.ceil(W*dpr));
  cv.height = Math.max(1, Math.ceil(H*dpr));
  cv.setAttribute('data-canvasified','1');
  cv.style.cssText = 'position:absolute;left:0;top:0;width:'+W+'px;height:'+H+'px;'
                   + 'z-index:2147483646;pointer-events:none;background:'
                   + (getComputedStyle(body).backgroundColor || '#fff');
  var ctx = cv.getContext('2d');
  if (!ctx) { tag('data-cv-err','no-2d-context'); return; }
  ctx.scale(dpr,dpr);
  ctx.fillStyle = getComputedStyle(body).backgroundColor || '#fff';
  ctx.fillRect(0,0,W,H);

  var nodes = [], walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, null);
  var n;
  while ((n = walker.nextNode())) {
    var raw = n.nodeValue;
    if (!raw || !raw.trim()) continue;
    var el = n.parentElement;
    if (!el) continue;
    var tn = el.tagName;
    if (tn==='SCRIPT'||tn==='STYLE'||tn==='NOSCRIPT'||tn==='CANVAS'||tn==='TITLE') continue;
    var cs = getComputedStyle(el);
    if (cs.visibility==='hidden' || cs.display==='none' || parseFloat(cs.opacity||'1')===0) continue;
    var r = document.createRange();
    r.selectNodeContents(n);
    var rects = [];
    try { rects = Array.prototype.slice.call(r.getClientRects()); } catch(e) { rects = []; }
    if (!rects.length) continue;
    nodes.push({node:n, el:el, cs:cs, rects:rects, text:raw});
  }
  tag('data-cv-nodes', nodes.length);

  nodes.forEach(function(item){
    var cs=item.cs, rects=item.rects;
    var fs = parseFloat(cs.fontSize) || 16;
    ctx.font = (cs.fontStyle||'normal')+' '+(cs.fontWeight||'400')+' '+fs+'px '+(cs.fontFamily||'sans-serif');
    ctx.fillStyle = cs.color || '#000';
    ctx.textBaseline = 'alphabetic';
    var words = item.text.split(/(\s+)/).filter(function(w){return w.length;});
    var li = 0, line = '';
    function flush(){
      if (!line) return;
      var rect = rects[Math.min(li, rects.length-1)];
      var baseY = rect.bottom - Math.max(2, (rect.height - fs)/2) - fs*0.15;
      ctx.fillText(line.replace(/\s+$/,''), rect.left, baseY);
      li++;
      line = '';
    }
    for (var i=0;i<words.length;i++){
      var w = words[i];
      if (!w.trim()) { line += ' '; continue; }
      var rect = rects[Math.min(li, rects.length-1)];
      var test = line + w;
      if (ctx.measureText(test).width > rect.width + 1 && line.trim() !== '') { flush(); line = w; }
      else { line = test; }
    }
    flush();
  });

  tag('data-cv-drawn', nodes.length);
  body.appendChild(cv);

  // Strip the DOM text only after every position has been measured and painted.
  var stripped = 0;
  nodes.forEach(function(item){
    try { item.node.nodeValue = ''; stripped++; } catch(e){}
  });
  tag('data-cv-stripped', stripped);
  tag('data-cv-inner', (body.innerText || '').length);
  } catch (e) { tag('data-cv-err', e && e.message ? e.message : String(e)); }
})();
"""


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------

def _img_b64(path: str | Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def build_image_clone(cap: Capture, links: list[dict] | None = None) -> dict:
    """Pixels -> canvas. The classic 'whole site in a canvas' artifact."""
    if not cap.screenshot or not Path(cap.screenshot).exists():
        return {"ok": False, "error": "No screenshot captured for this target."}

    b64 = _img_b64(cap.screenshot)
    hotspots = ""
    for i, l in enumerate(links or []):
        x, y, w, h = l.get("x", 0), l.get("y", 0), l.get("w", 120), l.get("h", 40)
        href = html_mod.escape(l.get("href", "#"), quote=True)
        hotspots += (
            f'<a class="hs" href="{href}" style="left:{x}px;top:{y}px;'
            f'width:{w}px;height:{h}px" data-i="{i}"></a>\n'
        )

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{html_mod.escape(cap.meta.get('title','page')[:80])}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  html,body{{margin:0;padding:0;background:#000;overflow-x:hidden}}
  /* link hotspots carry no text, so innerText stays empty */
  a.hs{{position:absolute;display:block;z-index:2147483647;background:transparent;
        text-decoration:none;color:transparent;font-size:0}}
  canvas{{display:block}}
</style></head>
<body>
<canvas id="c"></canvas>
{hotspots}
<script>
(function(){{
  var img = new Image();
  img.onload = function(){{
    var c = document.getElementById('c');
    c.width = img.naturalWidth; c.height = img.naturalHeight;
    c.style.width = img.naturalWidth + 'px';
    c.style.height = img.naturalHeight + 'px';
    c.getContext('2d').drawImage(img, 0, 0);
    document.body.setAttribute('data-canvas-ready','1');
    document.body.setAttribute('data-inner', String((document.body.innerText||'').length));
  }};
  img.src = "data:image/png;base64,{b64}";
}})();
</script>
</body></html>"""
    out = ARTIFACT_DIR / f"{cap.slugs}.canvas-image.html"
    out.write_text(doc, encoding="utf-8")
    return {"ok": True, "mode": "image", "path": str(out),
            "bytes": out.stat().st_size, "note": "pixels replayed into canvas; all text nodes absent"}


def build_text_clone(cap: Capture) -> dict:
    """Inject the canvasifier into a static snapshot of the rendered page."""
    html_src = cap.rendered or cap.raw_human
    if not html_src:
        return {"ok": False, "error": "No rendered HTML to convert."}

    soup = BeautifulSoup(html_src, "lxml")
    # A public artifact must not run the target's own JS against the lab origin.
    for s in soup.find_all("script"):
        s.decompose()
    for tag in soup.find_all(True):
        for attr in ("onload", "onerror", "onclick", "onmouseover"):
            tag.attrs.pop(attr, None)

    js = soup.new_tag("script")
    js.string = CANVASIFY_JS
    (soup.body or soup).append(js)

    out = ARTIFACT_DIR / f"{cap.slugs}.canvas-text.html"
    out.write_text(str(soup), encoding="utf-8")
    return {"ok": True, "mode": "text", "path": str(out),
            "bytes": out.stat().st_size,
            "note": "each text node redrawn with its computed font, then deleted"}


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

def _chromium_measure(target: str, slug: str) -> tuple[str, str, str]:
    profile = tempfile.mkdtemp(prefix="lab-meas-")
    shot = ARTIFACT_DIR / f"{slug}.measured.png"
    url = target if "://" in target else Path(target).resolve().as_uri()
    base = [
        "chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--hide-scrollbars", "--no-first-run",
        "--allow-file-access-from-files", "--disable-extensions",
        f"--user-data-dir={profile}", "--window-size=1366,1400",
        "--virtual-time-budget=9000", f"--user-agent={HUMAN_UA}",
    ]
    dom, err = "", ""
    try:
        res = subprocess.run(base + ["--dump-dom", url],
                             capture_output=True, text=True, timeout=RENDER_TIMEOUT)
        dom = res.stdout or ""
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    try:
        subprocess.run(base + [f"--screenshot={shot}", url],
                       capture_output=True, text=True, timeout=RENDER_TIMEOUT)
    except Exception as exc:
        err = (err + f" | shot: {type(exc).__name__}").strip(" |")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return dom, (str(shot) if shot.exists() else ""), err


def _dom_text(html: str) -> str:
    """Visible body text only — <title>/<head> are not page content."""
    soup = BeautifulSoup(html or "", "lxml")
    for t in soup(["script", "style", "noscript", "template", "head"]):
        t.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))


def _term_survival(terms: list[str], text: str) -> dict:
    t = (text or "").lower()
    found = [x for x in terms if x in t]
    return {
        "terms_checked": len(years := terms),
        "terms_found": found,
        "survival": round(len(found) / max(len(terms), 1), 3),
    }


def measure_clone(path: str, terms: list[str], slug: str) -> dict:
    dom, shot, err = _chromium_measure(path, f"{slug}-clone")
    soup_dbg = BeautifulSoup(dom or "", "lxml")
    body_tag = soup_dbg.find("body")
    diag = {}
    if body_tag is not None:
        for k in ("data-cv-nodes", "data-cv-drawn", "data-cv-stripped",
                  "data-cv-inner", "data-cv-err", "data-canvas-ready", "data-inner"):
            if body_tag.has_attr(k):
                diag[k] = body_tag[k]
    dom_text = _dom_text(dom)
    ocr_text = ocr_image(shot) if shot else ""
    inner = diag.get("data-inner") or diag.get("data-cv-inner")
    try:
        inner_chars = int(inner)
    except (TypeError, ValueError):
        inner_chars = len(dom_text)
    return {
        "path": path,
        "inner_chars": inner_chars,
        "dom_chars": len(dom_text),
        "dom_text": dom_text[:1500],
        "ocr_chars": len(ocr_text),
        "ocr_text": ocr_text[:1500],
        "by_dom": _term_survival(terms, dom_text),
        "by_ocr": _term_survival(terms, ocr_text),
        "measured_screenshot": shot,
        "diag": diag,
        "error": err,
    }


@dataclass
class CanvasExperiment:
    slug: str
    terms: list[str]
    baseline: dict
    image_mode: dict
    text_mode: dict
    verdict: str
    numbers: dict


def run_experiment(cap: Capture, terms: list[str]) -> CanvasExperiment:
    baseline = {
        "dom_chars": len(cap.dom_text or ""),
        "ocr_chars": len(cap.ocr_text or ""),
        "by_dom": _term_survival(terms, cap.dom_text),
        "by_ocr": _term_survival(terms, cap.ocr_text),
    }

    img_build = build_image_clone(cap)
    txt_build = build_text_clone(cap)

    img_meas = measure_clone(img_build["path"], terms, f"{cap.slugs}-img") if img_build.get("ok") else {
        "error": img_build.get("error"), "by_dom": {"survival": 0}, "by_ocr": {"survival": 0},
        "dom_chars": 0, "ocr_chars": 0,
    }
    txt_meas = measure_clone(txt_build["path"], terms, f"{cap.slugs}-txt") if txt_build.get("ok") else {
        "error": txt_build.get("error"), "by_dom": {"survival": 0}, "by_ocr": {"survival": 0},
        "dom_chars": 0, "ocr_chars": 0,
    }

    dom_killed = baseline["by_dom"]["survival"] > 0 and img_meas.get("by_dom", {}).get("survival", 0) == 0
    ocr_survives = img_meas.get("by_ocr", {}).get("survival", 0) > 0

    if dom_killed and ocr_survives:
        verdict = (
            "Text-channel evasion confirmed, review-channel evasion refuted: DOM text drops to zero "
            "while OCR still recovers the restricted vocabulary. A vision reviewer sees the same page."
        )
    elif dom_killed and not ocr_survives:
        verdict = (
            "DOM text dropped to zero and OCR did not recover the terms in the captured viewport. "
            "That is a measurement artefact of the crop, not a durable bypass — scroll and zoom "
            "in a real review recover text."
        )
    elif not dom_killed:
        verdict = "Canvas conversion did not remove the text channel for this page; nothing was bypassed."
    else:
        verdict = "Inconclusive for this target."

    numbers = {
        "inner_chars_before": len(cap.dom_text or ""),
        "inner_chars_after_image": img_meas.get("inner_chars", 0),
        "inner_chars_after_text": txt_meas.get("inner_chars", 0),
        "dom_chars_before": baseline["dom_chars"],
        "dom_chars_after": img_meas.get("dom_chars", 0),
        "ocr_chars_before": baseline["ocr_chars"],
        "ocr_chars_after": img_meas.get("ocr_chars", 0),
        "dom_survival_before": baseline["by_dom"]["survival"],
        "dom_survival_after": img_meas.get("by_dom", {}).get("survival", 0),
        "ocr_survival_after": img_meas.get("by_ocr", {}).get("survival", 0),
        "text_mode_dom_chars": txt_meas.get("dom_chars", 0),
        "text_mode_dom_survival": txt_meas.get("by_dom", {}).get("survival", 0),
        "text_mode_ocr_survival": txt_meas.get("by_ocr", {}).get("survival", 0),
    }

    return CanvasExperiment(
        slug=cap.slugs, terms=terms, baseline=baseline,
        image_mode={**img_build, **img_meas}, text_mode={**txt_build, **txt_meas},
        verdict=verdict, numbers=numbers,
    )

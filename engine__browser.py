"""One long-lived headless browser, shared by every capture.

The original pipeline launched a fresh Chromium for every DOM dump and every
screenshot: six cold starts per canvas experiment. On a small instance
(0.1 vCPU / 512 MB) each cold start costs 20-40 s, which is where the canvas
timeouts came from. Here a single Playwright browser lives on one dedicated
thread (Playwright's sync objects are thread-bound), and each page load
returns the DOM *and* the screenshot from the same render.

If Playwright is missing or the browser cannot start, callers fall back to the
Chromium CLI path, so the lab degrades instead of failing.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VIEWPORT = {"width": 1366, "height": 1400}
NAV_TIMEOUT_MS = int(os.environ.get("LAB_NAV_TIMEOUT_MS", "30000"))
SETTLE_MS = int(os.environ.get("LAB_SETTLE_MS", "6000"))
# Recycle the browser periodically so slow leaks never reach the memory cap.
# On a 512 MB host set LAB_PAGES_PER_BROWSER=1: the browser then exits after
# every page, so it never holds memory while Tesseract runs.
MAX_PAGES_PER_BROWSER = max(1, int(os.environ.get("LAB_PAGES_PER_BROWSER", "25")))

LAUNCH_ARGS = [
    "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
    "--no-zygote", "--renderer-process-limit=1", "--disable-extensions",
    "--disable-background-networking", "--disable-sync", "--mute-audio",
    "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
    "--js-flags=--max-old-space-size=256",
]

_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lab-browser")
_state: dict = {"pw": None, "browser": None, "pages": 0, "disabled": ""}
_lock = threading.Lock()


class BrowserUnavailable(RuntimeError):
    pass


def _close() -> None:
    try:
        if _state["browser"] is not None:
            _state["browser"].close()
    except Exception:
        pass
    try:
        if _state["pw"] is not None:
            _state["pw"].stop()
    except Exception:
        pass
    _state.update(pw=None, browser=None, pages=0)


def _ensure():
    if _state["disabled"]:
        raise BrowserUnavailable(_state["disabled"])
    b = _state["browser"]
    if b is not None and b.is_connected() and _state["pages"] < MAX_PAGES_PER_BROWSER:
        return b
    _close()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        _state["disabled"] = f"playwright not installed ({exc})"
        raise BrowserUnavailable(_state["disabled"]) from exc
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True, args=LAUNCH_ARGS)
    except Exception as exc:
        pw.stop()
        raise BrowserUnavailable(f"browser launch failed: {exc}") from exc
    _state.update(pw=pw, browser=browser, pages=0)
    return browser


def _render(url: str, shot: Path, user_agent: str, ready_selector: str | None) -> tuple[str, str, str]:
    browser = _ensure()
    _state["pages"] += 1
    ctx = browser.new_context(viewport=VIEWPORT, user_agent=user_agent,
                              ignore_https_errors=True, locale="en-US")
    page = ctx.new_page()
    err = ""
    dom = ""
    try:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            # A slow subresource should not throw away a usable page.
            err = f"navigation: {type(exc).__name__}"
        try:
            if ready_selector:
                page.wait_for_selector(ready_selector, state="attached", timeout=SETTLE_MS)
            else:
                page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
        except Exception:
            pass
        try:
            dom = page.content()
        except Exception as exc:
            err = (err + f" | dom: {type(exc).__name__}").strip(" |")
        try:
            page.screenshot(path=str(shot), full_page=False, timeout=15000)
        except Exception as exc:
            err = (err + f" | screenshot: {type(exc).__name__}").strip(" |")
    finally:
        try:
            ctx.close()
        except Exception:
            # A wedged context usually means a wedged browser: start fresh next time.
            _close()
        if _state["pages"] >= MAX_PAGES_PER_BROWSER:
            _close()
    return dom, (str(shot) if shot.exists() else ""), err


def render(url: str, shot: Path, user_agent: str, ready_selector: str | None = None,
           timeout: float = 90) -> tuple[str, str, str]:
    """Load ``url`` once; return (dom, screenshot_path, error).

    Raises BrowserUnavailable when the shared browser cannot be used at all.
    """
    with _lock:
        fut = _EXEC.submit(_render, url, shot, user_agent, ready_selector)
        try:
            return fut.result(timeout=timeout)
        except BrowserUnavailable:
            raise
        except Exception as exc:
            _EXEC.submit(_close)
            raise BrowserUnavailable(f"render failed: {type(exc).__name__}: {exc}") from exc

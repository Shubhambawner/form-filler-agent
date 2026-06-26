import random
from playwright.async_api import async_playwright

from . import select_strategies, selector_agent
from .select_strategies import NeedsSelectorAgent
from .utils import _values_match



class BrowserClient:
    """Drives a real browser via the Playwright Python SDK, exposing
    accessibility-tree snapshots and role/name based actions for the agent."""

    # Third-party hostnames/path fragments to exclude from the network log.
    _NOISE_PATTERNS = (
        "analytics", "googletagmanager", "google-analytics", "doubleclick",
        "hotjar", "amplitude", "segment.io", "mixpanel", "clarity.ms",
        "fullstory", "heapanalytics", "facebook.net", "twitter.com",
        "linkedin.com/li/", "matomo",
    )
    # Static asset extensions to skip.
    _STATIC_EXTS = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                    ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map", ".webp")

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._playwright = None
        self.context = None
        self.page = None
        self._active_frame = None
        self._page_host = ""       # hostname of the current page, for filtering
        self._network_log: list = []  # backend calls since last drain

    async def connect(self):
        print("[Browser] Launching browser...")
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        self.context = await self.browser.new_context(
            permissions=["clipboard-read", "clipboard-write"],
        )
        await self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        self.page = await self.context.new_page()
        self._active_frame = None
        # Register the response listener for network-activity reporting.
        self.page.on("response", self._on_response)
        print("[Browser] Ready.")

    def _on_response(self, response):
        """Sync event handler — captures same-domain backend responses."""
        url = response.url
        try:
            host = url.split("//")[-1].split("/")[0]
        except Exception:
            return
        # Must be same host as the page (or a subdomain of it).
        if self._page_host and self._page_host not in host and host not in self._page_host:
            return
        # Skip noise: trackers, CDN assets, static files.
        url_lower = url.lower()
        if any(p in url_lower for p in self._NOISE_PATTERNS):
            return
        path = url.split(host)[-1].split("?")[0]
        if any(path.lower().endswith(ext) for ext in self._STATIC_EXTS):
            return
        self._network_log.append({
            "method": response.request.method,
            "path": path[:150],
            "status": response.status,
        })

    def drain_network_log(self) -> list:
        """Returns all captured backend calls since the last call and clears the buffer."""
        log = self._network_log[:]
        self._network_log.clear()
        return log

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _wait_for_stable(self, timeout: int = 8000, poll_dom: bool = False, pre_snapshot: str = None):
        """Wait for the page to settle after an action.

        `poll_dom=False` (default): waits only for networkidle — right for fills,
        checks, uploads that modify a single input and never trigger navigation.

        `poll_dom=True`: additionally polls the ARIA snapshot in two phases:
          Phase 1 (only when pre_snapshot is given): wait until the DOM actually
          *differs* from the pre-action state, so we don't exit early while the
          page is waiting for a server response (e.g. login submit — the form stays
          visible and "stable" until the server replies, then the page re-renders).
          Phase 2: wait for the new state to stabilise (two identical reads 200ms
          apart). Cap at 5s per phase."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

        if not poll_dom:
            return

        frame = self._active_frame or self.page.main_frame

        # Phase 1: wait for the DOM to change from the pre-action state.
        if pre_snapshot:
            for _ in range(25):  # 25 × 200ms = 5s max
                try:
                    current = await frame.locator("body").aria_snapshot(timeout=1000)
                except Exception:
                    break
                if current != pre_snapshot:
                    break
                await self.page.wait_for_timeout(200)

        # Phase 2: wait for the new state to stabilise (two identical reads 200ms apart).
        prev = ""
        stable = 0
        for _ in range(25):  # 25 × 200ms = 5s max
            try:
                current = await frame.locator("body").aria_snapshot(timeout=1000)
            except Exception:
                break
            if current == prev:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            prev = current
            await self.page.wait_for_timeout(200)

    async def _detect_content_frame(self):
        """Find the child frame that holds the actual page content (form fields,
        Apply button, etc.) when the top-level frame is just a branding/wrapper
        shell containing a single content iframe.

        Sets self._active_frame to that child frame, or to the main frame when
        there is no meaningful child frame (the common case for most ATS sites).
        All snapshot() and locator_for() calls then operate on the right context."""
        best_frame, best_len = None, 0
        for frame in self.page.frames[1:]:
            url = frame.url
            if not url or url in ("about:blank", "") or url.startswith("javascript:"):
                continue
            try:
                snap = await frame.locator("body").aria_snapshot(timeout=3000)
                if snap and len(snap) > best_len:
                    best_len, best_frame = len(snap), frame
            except Exception:
                continue

        if best_frame and best_len > 100:
            if self._active_frame is None or self._active_frame.url != best_frame.url:
                print(f"[Browser] Content frame detected: {best_frame.url}")
            self._active_frame = best_frame
        else:
            self._active_frame = self.page.main_frame

    async def _move_mouse_naturally(self, target_locator=None):
        """Move the mouse through 2-4 random waypoints before settling near the
        target element, mimicking casual human cursor movement."""
        vp = self.page.viewport_size or {"width": 1280, "height": 720}
        for _ in range(random.randint(2, 4)):
            x = random.randint(80, vp["width"] - 80)
            y = random.randint(80, vp["height"] - 80)
            await self.page.mouse.move(x, y)
            await self.page.wait_for_timeout(random.randint(40, 120))
        if target_locator:
            try:
                bbox = await target_locator.bounding_box()
                if bbox:
                    tx = bbox["x"] + bbox["width"] / 2 + random.randint(-4, 4)
                    ty = bbox["y"] + bbox["height"] / 2 + random.randint(-4, 4)
                    await self.page.mouse.move(tx, ty)
            except Exception:
                pass

    async def _set_clipboard(self, text: str):
        """Write text to the browser clipboard via the Web Clipboard API.
        Works cross-platform (no OS clipboard utilities needed)."""
        await self.page.evaluate("async (t) => { await navigator.clipboard.writeText(t); }", text)

    async def _human_fill(self, locator, value: str):
        """Fill an input via the Playwright locator (ARIA role+name, stable).

        Tries clipboard paste first. If the value doesn't appear in the field
        afterwards (paste blocked/disabled on this input), falls back to
        character-by-character typing through the locator."""
        await self._move_mouse_naturally(locator)
        await locator.fill("")
        await self._set_clipboard(value)
        await locator.press("Control+v")
        try:
            actual = await locator.input_value()
        except Exception:
            actual = ""
        if not _values_match(actual, value):
            print(f"[Browser] Paste did not stick on '{await locator.get_attribute('name') or value[:20]}'; falling back to typing.")
            await locator.fill("")
            await locator.type(value, delay=30)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def navigate(self, url: str):
        print(f"[Browser] Navigating to {url}")
        self._page_host = url.split("//")[-1].split("/")[0]
        self._network_log.clear()  # fresh log per navigation
        await self.page.goto(url, wait_until="load")
        await self._wait_for_stable(poll_dom=True)
        await self._detect_content_frame()

    async def snapshot(self) -> str:
        """Returns an ARIA accessibility-tree snapshot of the active frame."""
        frame = self._active_frame or self.page.main_frame
        return await frame.locator("body").aria_snapshot()

    def locator_for(self, role: str, name: str, nth: int = None):
        frame = self._active_frame or self.page.main_frame
        locator = frame.get_by_role(role, name=name, exact=True)
        if nth is not None:
            locator = locator.nth(nth)
        return locator

    async def compute_signature(self, role: str, name: str, nth: int = None) -> str:
        return await select_strategies.compute_signature(self.locator_for(role, name, nth))

    async def execute_action(self, action: dict):
        """Executes a single role/name based action via the Playwright API."""
        act = action.get("action")
        role = action.get("role")
        name = action.get("name")
        value = action.get("value")
        nth = action.get("nth")
        recipe = action.get("recipe")

        print(f"[Browser] Executing {act} on {role} '{name}'" + (f" [nth={nth}]" if nth is not None else "") + (f" = {value!r}" if value is not None else "") + (" (recipe)" if recipe else ""))
        locator = self.locator_for(role, name, nth)

        # Capture pre-action snapshot for clicks so _wait_for_stable can detect
        # when the page actually changes (avoids false "stable" on server-wait states).
        pre_snap = None
        if act == "click":
            try:
                pre_snap = await self.snapshot()
            except Exception:
                pass

        if act == "fill":
            await self._human_fill(locator, value)
            actual = await locator.input_value()
            if not _values_match(actual, value):
                raise AssertionError(f"'{name}' still shows {actual!r} after fill with {value!r}")
        elif act == "click":
            await self._move_mouse_naturally(locator)
            await locator.click()
            if role in ("radio", "checkbox") and not await locator.is_checked():
                raise AssertionError(f"'{name}' is not checked after click")
        elif act in ("select", "combobox_select"):
            if recipe:
                await selector_agent.run_recipe(self.page, locator, name, nth, recipe, action.get("chosen_label"))
            elif act == "select":
                try:
                    await select_strategies.native_select(self.page, locator, name, value, nth)
                except Exception:
                    raise NeedsSelectorAgent(action)
            else:
                raise NeedsSelectorAgent(action)
        elif act == "check":
            await self._move_mouse_naturally(locator)
            await locator.check()
            if not await locator.is_checked():
                raise AssertionError(f"'{name}' is not checked after check")
        elif act == "uncheck":
            await self._move_mouse_naturally(locator)
            await locator.uncheck()
            if await locator.is_checked():
                raise AssertionError(f"'{name}' is still checked after uncheck")
        elif act == "upload":
            async with self.page.expect_file_chooser() as fc_info:
                await locator.click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(value)
        else:
            raise ValueError(f"Unknown action type: {act}")

        # Clicks can trigger full-page navigations and SPA re-renders; poll the
        # DOM for stability after them.  Fills/checks/uploads only affect a single
        # input and never need DOM polling.
        is_click = act == "click"
        await self._wait_for_stable(timeout=15000 if is_click else 5000, poll_dom=is_click, pre_snapshot=pre_snap)
        await self._detect_content_frame()

    async def screenshot(self, path: str):
        await self.page.screenshot(path=path, full_page=True)

    async def screenshot_bytes(self) -> bytes:
        """Returns a PNG screenshot as raw bytes (for inline multimodal LLM calls)."""
        return await self.page.screenshot(full_page=True)

    async def reload(self):
        """Hard-reloads the current page and re-detects the active content frame."""
        print("[Browser] Reloading page...")
        self._network_log.clear()
        await self.page.reload(wait_until="load")
        await self._wait_for_stable(poll_dom=True)
        await self._detect_content_frame()

    async def close(self):
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        print("[Browser] Closed.")

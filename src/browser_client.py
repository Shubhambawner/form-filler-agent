from playwright.async_api import async_playwright

class BrowserClient:
    """Drives a real browser via the Playwright Python SDK, exposing
    accessibility-tree snapshots and role/name based actions for the agent."""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._playwright = None
        self.browser = None
        self.page = None

    async def connect(self):
        print("[Browser] Launching browser...")
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(headless=self.headless)
        self.page = await self.browser.new_page()
        print("[Browser] Ready.")

    async def navigate(self, url: str):
        print(f"[Browser] Navigating to {url}")
        await self.page.goto(url)

    async def snapshot(self) -> str:
        """Returns an ARIA accessibility-tree snapshot of the current page."""
        return await self.page.locator("body").aria_snapshot()

    async def execute_action(self, action: dict):
        """Executes a single role/name based action via the Playwright API."""
        act = action.get("action")
        role = action.get("role")
        name = action.get("name")
        value = action.get("value")
        nth = action.get("nth")

        print(f"[Browser] Executing {act} on {role} '{name}'" + (f" [nth={nth}]" if nth is not None else "") + (f" = {value!r}" if value is not None else ""))
        locator = self.page.get_by_role(role, name=name)
        if nth is not None:
            locator = locator.nth(nth)

        if act == "fill":
            await locator.fill(value)
        elif act == "click":
            await locator.click()
        elif act == "select":
            await locator.select_option(value)
        elif act == "check":
            await locator.check()
        elif act == "uncheck":
            await locator.uncheck()
        elif act == "upload":
            async with self.page.expect_file_chooser() as fc_info:
                await locator.click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(value)
        else:
            raise ValueError(f"Unknown action type: {act}")

    async def screenshot(self, path: str):
        await self.page.screenshot(path=path, full_page=True)

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        print("[Browser] Closed.")

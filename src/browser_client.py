from playwright.async_api import async_playwright

from . import select_strategies, selector_agent
from .select_strategies import NeedsSelectorAgent
from .utils import _values_match


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

    def locator_for(self, role: str, name: str, nth: int = None):
        locator = self.page.get_by_role(role, name=name)
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

        if act == "fill":
            await locator.fill(value)
            actual = await locator.input_value()
            if not _values_match(actual, value):
                raise AssertionError(f"'{name}' still shows {actual!r} after fill with {value!r}")
        elif act == "click":
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
            await locator.check()
            if not await locator.is_checked():
                raise AssertionError(f"'{name}' is not checked after check")
        elif act == "uncheck":
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

    async def screenshot(self, path: str):
        await self.page.screenshot(path=path, full_page=True)

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        print("[Browser] Closed.")

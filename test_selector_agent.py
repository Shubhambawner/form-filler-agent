import asyncio
import json
import os
from dotenv import load_dotenv

# Load the .env file before importing src modules, since src.agent reads
# GEMINI_API_KEY at import time (overrides stale system env vars)
load_dotenv(override=True)

from src.db import init_db, delete_recipe
from src.browser_client import BrowserClient
from src.run_logger import RunLogger
from src.select_strategies import NeedsSelectorAgent, RecipeFailed
from src import selector_agent
from test_inputs import TARGET_URL, REAL_DOMAIN, TEST_DOMAIN, SELECT_FIELDS as FIELDS

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# Exercises the selector agent in isolation, with NO main-agent (run_react_agent)
# calls at all: just navigate to a page and ask it to resolve a handful of
# {role, name, value} dropdowns -- exactly the input the main agent would hand
# it, with no profile data attached.
#
# Recipes discovered by this test are cached under TEST_DOMAIN (shared with
# test_full_discovery.py / test_select_cached.py / test_cached.py), so repeat
# runs benefit from their own cache without ever touching the real
# REAL_DOMAIN recipes those tests build and rely on.

# Force fresh discovery for part 1 on every run. Safe to leave True since it
# only clears TEST_DOMAIN rows.
CLEAR_CACHE = True


async def discover_part(browser, logger):
    """Part 1: ask the selector agent to resolve each field from scratch (or
    from this test's own cache), as the main agent would for a fresh
    combobox_select with no recipe attached yet."""
    if CLEAR_CACHE:
        for role, name, _value, _nth in FIELDS:
            delete_recipe(TEST_DOMAIN, role, name)

    discovered = []
    for i, (role, name, value, nth) in enumerate(FIELDS, start=1):
        action = {"action": "combobox_select", "role": role, "name": name, "value": value}
        if nth is not None:
            action["nth"] = nth

        print(f"\n=== [Discover {i}] {role} '{name}' = {value!r} ===")

        stale = None
        try:
            await browser.execute_action(action)
            print("  -> handled without selector agent (recipe already attached)")
            discovered.append((action, None))
            continue
        except (NeedsSelectorAgent, RecipeFailed) as e:
            if isinstance(e, RecipeFailed):
                stale = {"recipe": e.stale_recipe, "description": action.get("description"), "reason": str(e)}
            print(f"  -> needs selector agent ({type(e).__name__}: {e}); resolving...")

        result = await selector_agent.resolve(browser, action, TEST_DOMAIN, logger=logger,
                                                iteration=f"discover{i}", stale=stale)
        print(f"  -> result: {json.dumps(result, indent=2)}")
        discovered.append((action, result))

    return discovered


async def replay_part(browser, discovered):
    """Part 2: re-fill every field using ONLY the recipe discovered in part 1
    (action["recipe"] / action["chosen_label"], attached directly to the
    action) via browser.execute_action's run_recipe path -- the "generated
    script" -- with NO selector agent / LLM calls at all."""
    print("\n--- Part 2: replaying with generated recipes (no agent) ---")
    for i, (action, result) in enumerate(discovered, start=1):
        name = action["name"]
        if result is None or not result.get("success"):
            print(f"\n=== [Replay {i}] {action['role']} '{name}' -- skipped (no recipe from part 1) ===")
            continue

        replay_action = {**action, "recipe": result["recipe"], "chosen_label": result["chosen_label"]}
        print(f"\n=== [Replay {i}] {action['role']} '{name}' = {action['value']!r} "
              f"(recipe with {len(result['recipe'])} op(s)) ===")
        await browser.execute_action(replay_action)
        print("  -> ok")


async def main():
    init_db()
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY not found. Please ensure your .env file exists in the root directory.")
        return

    logger = RunLogger(REAL_DOMAIN, DATA_DIR)
    print(f"[Test] Logging to {logger.run_dir}")

    browser = BrowserClient()
    await browser.connect()
    try:
        await browser.navigate(TARGET_URL)
        discovered = await discover_part(browser, logger)

        await browser.screenshot(os.path.join(logger.run_dir, "after_discover.png"))

        # Reload to a clean page so part 2 proves the recipes are portable
        # across navigations, not just re-confirming an already-selected value.
        await browser.navigate(TARGET_URL)
        await replay_part(browser, discovered)

        await browser.screenshot(logger.final_screenshot_path())
        print(f"\n[Test] Final screenshot saved to {logger.final_screenshot_path()}")
    finally:
        await browser.close()


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())

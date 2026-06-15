import asyncio
import os
from dotenv import load_dotenv

# Load the .env file before importing src modules, since src.agent reads
# GEMINI_API_KEY at import time (overrides stale system env vars)
load_dotenv(override=True)

from src.db import init_db, delete_recipe
from src.executor import process_form
from test_inputs import TARGET_URL, TEST_DOMAIN, SELECT_FIELDS


async def main():
    init_db()
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY not found. Please ensure your .env file exists in the root directory.")
        return

    # force_refresh=True clears the TEST_DOMAIN flow cache; also clear the
    # TEST_DOMAIN select-recipe cache for each field so resolve() can't take
    # its exact-cache-hit shortcut and must run discover() fresh.
    for role, name, _value, _nth in SELECT_FIELDS:
        delete_recipe(TEST_DOMAIN, role, name)

    print(f"--- Full discovery test (isolated cache: {TEST_DOMAIN}) ---")
    print(f"Target: {TARGET_URL}")

    result = await process_form(TARGET_URL, force_refresh=True, cache_domain=TEST_DOMAIN)
    if result["status"] == "healed_needs_restart":
        print("--- Restarting with Healed Flow ---")
        result = await process_form(TARGET_URL, cache_domain=TEST_DOMAIN)

    print(f"--- Discovery Result: {result['status']} ---")


if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(main())

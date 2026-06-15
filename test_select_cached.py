import asyncio
import os
from dotenv import load_dotenv

# Load the .env file before importing src modules, since src.agent reads
# GEMINI_API_KEY at import time (overrides stale system env vars)
load_dotenv(override=True)

from src.db import init_db, get_flow_variants
from src.executor import process_form
from test_inputs import TARGET_URL, TEST_DOMAIN


async def main():
    init_db()
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY not found. Please ensure your .env file exists in the root directory.")
        return

    print(f"--- Select-cached flow discovery (isolated cache: {TEST_DOMAIN}) ---")
    print(f"Target: {TARGET_URL}")

    # Discard any cached FLOW (but not select_recipes) and regenerate it.
    # Combobox actions should hit exact-cache select-recipe hits (populated
    # by test_full_discovery.py) and need no LLM calls.
    result = await process_form(TARGET_URL, force_refresh=True, cache_domain=TEST_DOMAIN)
    if result["status"] == "healed_needs_restart":
        print("--- Restarting with Healed Flow ---")
        result = await process_form(TARGET_URL, cache_domain=TEST_DOMAIN)

    print(f"--- Discovery Result: {result['status']} ---")

    print("\n--- DB checks ---")
    variants = get_flow_variants(TEST_DOMAIN)
    assert len(variants) == 1, (
        f"expected the re-discovered flow to update the existing variant in place "
        f"(1 row) for {TEST_DOMAIN}, got {len(variants)} -- possible duplicate/ping-pong variant"
    )
    variant = variants[0]
    assert variant["success_count"] >= 2, f"expected success_count >= 2 (updated in place), got {variant['success_count']}"
    print(f"  cached_flows: 1 variant (id={variant['id']}, success_count={variant['success_count']})")


if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(main())

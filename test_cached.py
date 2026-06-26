import asyncio
import os
from dotenv import load_dotenv

# Load the .env file before importing src modules, since src.agent reads
# GEMINI_API_KEY at import time (overrides stale system env vars)
load_dotenv(override=True)

from src.db import init_db, get_flow_variants
from src.executor import process_form
from test_inputs import TARGET_URL, TEST_DOMAIN


async def main() -> str:
    init_db()
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY not found. Please ensure your .env file exists in the root directory.")
        return "error"

    print(f"--- Cached flow replay (isolated cache: {TEST_DOMAIN}) ---")
    print(f"Target: {TARGET_URL}")

    # Replay the flow cached by test_select_cached.py -- no force_refresh, so
    # this exercises the pure cached-flow replay path (no agent/selector calls
    # expected at all).
    result = await process_form(TARGET_URL, cache_domain=TEST_DOMAIN)
    if result["status"] in ("healed_needs_restart", "needs_restart"):
        print(f"--- Restarting ({result['status']}) ---")
        result = await process_form(TARGET_URL, cache_domain=TEST_DOMAIN)

    print(f"--- Replay Result: {result['status']} ---")

    print("\n--- DB checks ---")
    variants = get_flow_variants(TEST_DOMAIN)
    assert len(variants) == 1, f"expected pure replay to leave exactly 1 flow variant for {TEST_DOMAIN}, got {len(variants)}"
    variant = variants[0]
    print(f"  cached_flows: 1 variant (id={variant['id']}, success_count={variant['success_count']})")

    return result["status"]


if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    _exit_code = 0
    _status = None
    try:
        _status = asyncio.run(main())
    except Exception as e:
        print(f"FAILED: {e}")
        _exit_code = 1

    if _status == "dry_run_complete":
        try:
            input("\nBrowser is open for inspection. Press Enter to close it and exit...")
        except (KeyboardInterrupt, EOFError):
            pass

    os._exit(_exit_code)

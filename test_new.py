import asyncio
import os
from dotenv import load_dotenv

# Load the .env file before importing src modules, since src.agent reads
# GEMINI_API_KEY at import time (overrides stale system env vars)
load_dotenv(override=True)

from src.db import init_db
from src.executor import process_form

async def main():
    # Initialize the local SQLite cache
    init_db()
    
    # Double-check that python-dotenv successfully found and loaded the key
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY not found. Please ensure your .env file exists in the root directory.")
        return

    target_link = "https://ats.rippling.com/fello-careers/jobs/c0748edd-5d3d-4e0c-95ba-6c2d8e7aabb4/apply"
    print(f"--- Starting Form Filler System ---")
    print(f"Target: {target_link}")
    
    # Run the executor loop (always discard any cached flow and regenerate)
    result = await process_form(target_link, force_refresh=True)
    
    # If a self-healing event happened, rerun the execution phase with the updated flow
    if result["status"] == "healed_needs_restart":
        print("--- Restarting with Healed Flow ---")
        result = await process_form(target_link)
        
    print(f"--- Final Result: {result['status']} ---")

if __name__ == "__main__":
    # Windows environments often need this specific event loop policy for subprocesses
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        
    asyncio.run(main())
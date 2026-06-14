import os
from .db import get_cached_flow, save_flow, delete_flow
from .utils import is_final_submit, flow_has_final_submit
from .agent import run_react_agent
from .browser_client import BrowserClient
from .run_logger import RunLogger

MAX_DISCOVERY_ITERATIONS = 20
DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

async def process_form(url: str, force_refresh: bool = False):
    domain = url.split("//")[-1].split("/")[0]

    if force_refresh:
        print(f"[Executor] Force refresh requested. Clearing cached flow for {domain}...")
        delete_flow(domain)

    flow_sequence = get_cached_flow(domain)

    logger = RunLogger(domain, DATA_DIR)
    print(f"[Executor] Logging this request's artifacts to {logger.run_dir}")

    browser = BrowserClient()
    await browser.connect()

    try:
        await browser.navigate(url)

        # 1. Discovery Phase (if no cache exists)
        if not flow_sequence:
            print(f"[Executor] No cached flow for {domain}. Handing to Agent...")
            flow_sequence = await discover_flow(browser, url, domain, logger)
            if flow_has_final_submit(flow_sequence):
                save_flow(domain, flow_sequence)
            else:
                print(f"[Executor] Generated flow has no final submit step; not caching.")
            return {"status": "dry_run_complete" if flow_has_final_submit(flow_sequence) else "discovery_incomplete"}

        # 2. Replay Phase (cached flow)
        executed = []

        for i, action in enumerate(flow_sequence):
            if is_final_submit(action):
                executed.append(action)
                print(f"[DRY RUN] Intercepted final submit step: {action}")
                await browser.screenshot(logger.final_screenshot_path())
                return {"status": "dry_run_complete"}

            try:
                await browser.execute_action(action)
                executed.append(action)
            except Exception as e:
                print(f"[Executor] Cached step {i + 1} failed: {action}. Triggering self-healing...")

                error_context = {
                    "previousFlow": executed,
                    "failedActions": [{"action": action, "errorDetails": str(e)}]
                }
                healed_tail = await discover_flow(browser, url, domain, logger, error_context=error_context)
                new_flow = executed + healed_tail
                if flow_has_final_submit(new_flow):
                    save_flow(domain, new_flow)
                else:
                    print(f"[Executor] Healed flow has no final submit step; not caching.")

                return {"status": "healed_needs_restart"}

        return {"status": "flow_complete"}

    finally:
        await browser.close()

async def discover_flow(browser, url, domain, logger, error_context=None):
    """Iteratively snapshots the current page, asks the agent for the next batch of
    actions, and executes them until a final submit step is reached or the agent
    has nothing left to do."""
    flow_sequence = []

    # Failures persist across iterations (re-offered to the agent every time) until an
    # action targeting the same element succeeds, so a single missed retry doesn't
    # permanently drop a field.
    pending_failures = list(error_context["failedActions"]) if error_context else []

    def same_target(a, b):
        return (a.get("role") == b.get("role")
                and a.get("name") == b.get("name")
                and a.get("nth") == b.get("nth"))

    for _ in range(MAX_DISCOVERY_ITERATIONS):
        iteration = logger.next_iteration()
        snapshot = await browser.snapshot()
        logger.log_snapshot(iteration, snapshot)
        current_error_context = {"previousFlow": flow_sequence, "failedActions": pending_failures} if pending_failures else None
        batch = await run_react_agent(url, snapshot, flow_sequence, current_error_context, logger=logger, iteration=iteration)

        if not batch:
            break

        new_failures = []
        submit_action = None
        for action in batch:
            if is_final_submit(action):
                # Defer judgement until the whole batch has run: only accept this as the
                # terminal step if nothing else in the batch failed.
                submit_action = action
                continue

            try:
                await browser.execute_action(action)
                flow_sequence.append(action)
                pending_failures = [f for f in pending_failures if not same_target(f["action"], action)]
            except Exception as e:
                print(f"[Executor] Action failed during discovery: {action}. Continuing batch...")
                new_failures.append({"action": action, "errorDetails": str(e)})

        if submit_action is not None and not new_failures:
            flow_sequence.append(submit_action)
            print(f"[DRY RUN] Intercepted final submit step: {submit_action}")
            await browser.screenshot(logger.final_screenshot_path())
            break

        for nf in new_failures:
            pending_failures = [f for f in pending_failures if not same_target(f["action"], nf["action"])]
            pending_failures.append(nf)

    return flow_sequence

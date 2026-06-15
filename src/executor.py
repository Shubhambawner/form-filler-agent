import os
from .db import get_cached_flow, save_flow, delete_flow
from .utils import is_final_submit, flow_has_final_submit
from .agent import run_react_agent
from .browser_client import BrowserClient
from .run_logger import RunLogger
from .select_strategies import NeedsSelectorAgent, RecipeFailed
from . import selector_agent

MAX_DISCOVERY_ITERATIONS = 20
DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

async def process_form(url: str, force_refresh: bool = False, cache_domain: str = None):
    domain = url.split("//")[-1].split("/")[0]

    # `cache_domain` lets callers point the flow/select-recipe caches at an
    # isolated namespace (e.g. "<domain>#some-test") while still navigating
    # to the real `url` and logging under the real `domain` -- so test runs
    # don't read/clear/overwrite the real cached data for this site.
    cache_key = cache_domain or domain

    if force_refresh:
        print(f"[Executor] Force refresh requested. Clearing cached flow for {cache_key}...")
        delete_flow(cache_key)

    flow_sequence = get_cached_flow(cache_key)

    logger = RunLogger(domain, DATA_DIR)
    print(f"[Executor] Logging this request's artifacts to {logger.run_dir}")

    browser = BrowserClient()
    await browser.connect()

    try:
        await browser.navigate(url)

        # 1. Discovery Phase (if no cache exists)
        if not flow_sequence:
            print(f"[Executor] No cached flow for {cache_key}. Handing to Agent...")
            flow_sequence = await discover_flow(browser, url, cache_key, logger)
            if flow_has_final_submit(flow_sequence):
                save_flow(cache_key, flow_sequence)
            else:
                print(f"[Executor] Generated flow has no final submit step; not caching.")
            return {"status": "dry_run_complete" if flow_has_final_submit(flow_sequence) else "discovery_incomplete"}

        # 2. Replay Phase (cached flow) -- a faithful, serial re-run of the
        # discovery transcript. Steps tagged "expected_failure" failed during
        # discovery too (by design, e.g. a value that didn't match any option
        # on the first try); failing again is expected and is skipped rather
        # than triggering self-healing.
        executed = []

        async def self_heal(action, error_str, step_no):
            print(f"[Executor] Cached step {step_no} failed: {action}. Triggering self-healing...")
            error_context = {
                "previousFlow": executed,
                "failedActions": [{"action": action, "errorDetails": error_str}]
            }
            healed_tail = await discover_flow(browser, url, cache_key, logger, error_context=error_context)
            new_flow = executed + healed_tail
            if flow_has_final_submit(new_flow):
                save_flow(cache_key, new_flow)
            else:
                print(f"[Executor] Healed flow has no final submit step; not caching.")

        for i, action in enumerate(flow_sequence):
            if is_final_submit(action):
                executed.append(action)
                print(f"[DRY RUN] Intercepted final submit step: {action}")
                await browser.screenshot(logger.final_screenshot_path())
                return {"status": "dry_run_complete"}

            try:
                await browser.execute_action(action)
                executed.append(action)
            except (NeedsSelectorAgent, RecipeFailed) as e:
                if action.get("expected_failure"):
                    print(f"[Executor] Cached step {i + 1} failed as expected (recorded during discovery); skipping: {action}")
                    executed.append(action)
                    continue

                stale = None
                if isinstance(e, RecipeFailed):
                    stale = {"recipe": e.stale_recipe, "description": action.get("description"), "reason": str(e)}
                print(f"[Executor] Cached step {i + 1} needs re-discovery: {action}. Resolving...")
                result = await selector_agent.resolve(browser, action, cache_key, logger=logger,
                                                        iteration=f"replay-{i + 1}", stale=stale)
                if result["success"]:
                    updated_action = {**action, "recipe": result["recipe"], "chosen_label": result["chosen_label"],
                                       "description": result["description"], "expected_failure": False}
                    executed.append(updated_action)
                    save_flow(cache_key, executed + flow_sequence[i + 1:])
                    continue

                await self_heal(action, result["error"], i + 1)
                return {"status": "healed_needs_restart"}
            except Exception as e:
                if action.get("expected_failure"):
                    print(f"[Executor] Cached step {i + 1} failed as expected (recorded during discovery); skipping: {action}")
                    executed.append(action)
                    continue

                await self_heal(action, str(e), i + 1)
                return {"status": "healed_needs_restart"}

        return {"status": "flow_complete"}

    finally:
        await browser.close()

async def discover_flow(browser, url, domain, logger, error_context=None):
    """Iteratively snapshots the current page, asks the agent for the next batch of
    actions, and executes them until a final submit step is reached or the agent
    has nothing left to do.

    Every attempted action (success or failure) is recorded into flow_sequence, in
    order, so a cached flow is a faithful transcript of discovery. Failed actions
    are tagged "expected_failure": True (with "errorDetails") so replay can skip
    over them without triggering self-healing."""
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

        # Terminal states: an empty batch (nothing left to do), or a batch
        # consisting of exactly the final submit click (everything else is done).
        if not batch:
            break
        if len(batch) == 1 and is_final_submit(batch[0]):
            flow_sequence.append({**batch[0], "expected_failure": False})
            print(f"[DRY RUN] Intercepted final submit step: {batch[0]}")
            await browser.screenshot(logger.final_screenshot_path())
            break

        new_failures = []
        select_idx = 0
        for action in batch:
            if is_final_submit(action):
                # Bundled with other actions, so not yet terminal. Never click it
                # prematurely; let a future iteration re-evaluate once it's alone.
                print(f"[Executor] Deferring final submit step until it is the only action left: {action}")
                continue

            try:
                await browser.execute_action(action)
                flow_sequence.append({**action, "expected_failure": False})
                pending_failures = [f for f in pending_failures if not same_target(f["action"], action)]
            except (NeedsSelectorAgent, RecipeFailed) as e:
                stale = None
                if isinstance(e, RecipeFailed):
                    stale = {"recipe": e.stale_recipe, "description": action.get("description"), "reason": str(e)}

                print(f"[Executor] {action['action']} needs the selector agent: {action}. Discovering...")
                select_idx += 1
                result = await selector_agent.resolve(browser, action, domain, logger=logger,
                                                        iteration=f"{iteration}-{select_idx}", stale=stale)
                if result["success"]:
                    flow_sequence.append({**action, "recipe": result["recipe"], "chosen_label": result["chosen_label"],
                                           "description": result["description"], "expected_failure": False})
                    pending_failures = [f for f in pending_failures if not same_target(f["action"], action)]
                else:
                    flow_sequence.append({**action, "expected_failure": True, "errorDetails": result["error"]})
                    new_failures.append({"action": action, "errorDetails": result["error"]})
            except Exception as e:
                print(f"[Executor] Action failed during discovery: {action}. Continuing batch...")
                flow_sequence.append({**action, "expected_failure": True, "errorDetails": str(e)})
                new_failures.append({"action": action, "errorDetails": str(e)})

        for nf in new_failures:
            pending_failures = [f for f in pending_failures if not same_target(f["action"], nf["action"])]
            pending_failures.append(nf)
    else:
        print(f"[Executor] Reached MAX_DISCOVERY_ITERATIONS ({MAX_DISCOVERY_ITERATIONS}) without a terminal state.")

    return flow_sequence

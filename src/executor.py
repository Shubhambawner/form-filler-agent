import os
import asyncio
from .db import find_best_flow, save_flow_variant
from .utils import is_final_submit, flow_has_final_submit, flow_has_needs_login, extract_field_signature
from . import login_agent
from . import credentials as creds_store
from .agent import run_react_agent
from .browser_client import BrowserClient
from .run_logger import RunLogger
from .select_strategies import NeedsSelectorAgent, RecipeFailed
from . import selector_agent
from . import embeddings

MAX_DISCOVERY_ITERATIONS = 60
DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

# How similar a freshly-(re)discovered flow's initial-page embedding must be
# to an existing variant's for it to be treated as an update to THAT variant
# (a genuine dev-side change to the same listing) rather than a new variant
# (a different listing/template with a different field set).
SAME_VARIANT_THRESHOLD = 0.97

async def process_form(url: str, force_refresh: bool = False, cache_domain: str = None):
    domain = url.split("//")[-1].split("/")[0]

    # `cache_domain` lets callers point the flow/select-recipe caches at an
    # isolated namespace (e.g. "<domain>#some-test") while still navigating
    # to the real `url` and logging under the real `domain` -- so test runs
    # don't read/clear/overwrite the real cached data for this site.
    cache_key = cache_domain or domain
    domain_creds = creds_store.load(cache_key)

    logger = RunLogger(domain, DATA_DIR)
    print(f"[Executor] Logging this request's artifacts to {logger.run_dir}")

    browser = BrowserClient()
    await browser.connect()

    _keep_browser_open = False
    try:
        await browser.navigate(url)

        # Snapshot the page before any actions. Retry for up to 15s if the
        # page hasn't rendered accessible content yet (e.g. Workday SPA still
        # booting after networkidle fires).
        for _snap_attempt in range(5):
            initial_snapshot = await browser.snapshot()
            if initial_snapshot and initial_snapshot.strip():
                break
            print(f"[Executor] Initial snapshot empty; waiting 3s for page to render (attempt {_snap_attempt + 1}/5)...")
            await asyncio.sleep(3)
        else:
            raise RuntimeError("Page did not render accessible content after 15s.")

        field_sig = extract_field_signature(initial_snapshot)
        query_embedding = embeddings.embed_text(field_sig if field_sig else initial_snapshot[:4000])

        # `force_refresh` skips retrieval/replay entirely and runs fresh
        # discovery; the save step below still dedupes against existing
        # variants, so this updates the matching variant in place rather than
        # piling up duplicates when re-run against the same listing.
        flow_match = None if force_refresh else find_best_flow(cache_key, query_embedding)
        flow_sequence = flow_match["mcp_tool_sequence"] if flow_match else None

        def save_variant(new_flow):
            existing = find_best_flow(cache_key, query_embedding)
            update_id = existing["id"] if existing and existing["similarity"] >= SAME_VARIANT_THRESHOLD else None
            save_flow_variant(cache_key, initial_snapshot, query_embedding, new_flow, update_id=update_id)

        # 1. Discovery Phase (if no matching cached flow variant exists)
        if not flow_sequence:
            if force_refresh:
                print(f"[Executor] Force refresh requested for {cache_key}. Running fresh discovery...")
            else:
                print(f"[Executor] No matching cached flow variant for {cache_key}. Handing to Agent...")
            flow_sequence = await discover_flow(browser, url, cache_key, logger, domain_creds=domain_creds)

            # Auth gate detected during discovery: hand off to the login agent,
            # then tell the caller to restart with a fresh browser so login steps
            # become part of the next run's discovered flow (reproducibility).
            if flow_has_needs_login(flow_sequence):
                login_result = await login_agent.run(browser, url, cache_key, logger)
                if login_result["status"] == "otp_required":
                    return {"status": "otp_required"}
                if login_result["status"] != "success":
                    return {"status": "login_failed", "reason": login_result.get("reason")}
                # Credentials are now stored (or were already stored). Signal
                # the caller to restart process_form from scratch so the next
                # run opens a fresh browser, loads the credentials, and
                # discovers the full flow including the login steps.
                print(f"[Executor] Auth gate cleared; restart required for reproducible flow discovery.")
                return {"status": "needs_restart"}

            if flow_has_final_submit(flow_sequence):
                save_variant(flow_sequence)
                _keep_browser_open = True
                return {"status": "dry_run_complete"}
            else:
                print(f"[Executor] Generated flow has no final submit step; not caching.")
                return {"status": "discovery_incomplete"}

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
            healed_tail = await discover_flow(browser, url, cache_key, logger, error_context=error_context, domain_creds=domain_creds)
            new_flow = executed + healed_tail
            if flow_has_final_submit(new_flow):
                save_variant(new_flow)
            else:
                print(f"[Executor] Healed flow has no final submit step; not caching.")

        for i, action in enumerate(flow_sequence):
            if is_final_submit(action):
                executed.append(action)
                print(f"[DRY RUN] Intercepted final submit step: {action}")
                await browser.screenshot(logger.final_screenshot_path())
                _keep_browser_open = True
                return {"status": "dry_run_complete"}

            if action.get("action") == "wait":
                secs = min(max(int(action.get("seconds", 2)), 1), 5)
                print(f"[Executor] Replaying wait: {secs}s")
                await asyncio.sleep(secs)
                executed.append(action)
                continue

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
                    save_flow_variant(cache_key, initial_snapshot, query_embedding,
                                       executed + flow_sequence[i + 1:], update_id=flow_match["id"])
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
        if not _keep_browser_open:
            await browser.close()
        logger.close()

_MAX_CONSECUTIVE_WAIT_SECS = 20  # abort discovery if agent waits this long with no real action

async def discover_flow(browser, url, domain, logger, error_context=None, domain_creds=None):
    """Iteratively snapshots the current page, asks the agent for the next batch of
    actions, and executes them until a final submit step is reached.

    Empty batches trigger a 2-second wait and a retry (the agent may be waiting
    for the page to settle). The loop only terminates on: an is_final submit
    action being intercepted, a needs_login signal, 20 consecutive seconds of
    waits/empty batches with no real action, or MAX_DISCOVERY_ITERATIONS."""
    flow_sequence = []
    pending_failures = list(error_context["failedActions"]) if error_context else []
    consecutive_wait_secs = 0

    def same_target(a, b):
        return (a.get("role") == b.get("role")
                and a.get("name") == b.get("name")
                and a.get("nth") == b.get("nth"))

    network_activity = None   # populated after each batch from the browser's response log
    pending_screenshot = None  # bytes, set when agent requests a screenshot

    for iter_num in range(MAX_DISCOVERY_ITERATIONS):
        iteration = logger.next_iteration()
        snapshot = await browser.snapshot()
        logger.log_snapshot(iteration, snapshot)
        current_error_context = {"previousFlow": flow_sequence, "failedActions": pending_failures} if pending_failures else None
        batch = await run_react_agent(url, snapshot, flow_sequence, current_error_context, logger=logger, iteration=iteration, domain_creds=domain_creds, network_activity=network_activity or None, screenshot_data=pending_screenshot)
        pending_screenshot = None  # consumed

        # Empty batch: page may still be loading/transitioning. Wait and retry
        # rather than treating it as "nothing to do" termination.
        if not batch:
            wait_secs = 2
            print(f"[Executor] Agent returned empty batch; waiting {wait_secs}s before retry...")
            await asyncio.sleep(wait_secs)
            consecutive_wait_secs += wait_secs
            if consecutive_wait_secs >= _MAX_CONSECUTIVE_WAIT_SECS:
                print(f"[Executor] {_MAX_CONSECUTIVE_WAIT_SECS}s consecutive wait reached without progress; aborting.")
                break
            continue

        if len(batch) == 1 and batch[0].get("action") == "needs_login":
            flow_sequence.append({**batch[0], "expected_failure": False})
            print(f"[Executor] Auth gate detected; handing off to login agent.")
            break

        # The only termination: agent explicitly marks is_final on a lone submit
        # action (skip on iter 0 — first page is often a job listing with an
        # "Apply" navigation button, not the true form submission).
        if iter_num > 0 and len(batch) == 1 and is_final_submit(batch[0]):
            flow_sequence.append({**batch[0], "expected_failure": False})
            print(f"[DRY RUN] Intercepted final submit step: {batch[0]}")
            await browser.screenshot(logger.final_screenshot_path())
            break

        new_failures = []
        select_idx = 0
        abort = False
        for action in batch:
            if is_final_submit(action):
                # Bundled with other actions — defer until it arrives alone.
                print(f"[Executor] Deferring final submit step until it is the only action left: {action}")
                continue

            if action.get("action") == "wait":
                secs = min(max(int(action.get("seconds", 2)), 1), 5)
                print(f"[Executor] Agent requested wait: {secs}s")
                await asyncio.sleep(secs)
                consecutive_wait_secs += secs
                flow_sequence.append({**action, "expected_failure": False})
                if consecutive_wait_secs >= _MAX_CONSECUTIVE_WAIT_SECS:
                    print(f"[Executor] {_MAX_CONSECUTIVE_WAIT_SECS}s consecutive wait reached; aborting.")
                    abort = True
                    break
                continue

            if action.get("action") == "screenshot":
                print(f"[Executor] Agent requested screenshot; will include in next turn.")
                pending_screenshot = await browser.screenshot_bytes()
                flow_sequence.append({**action, "expected_failure": False})
                continue

            if action.get("action") == "reload":
                print(f"[Executor] Agent requested page reload.")
                await browser.reload()
                consecutive_wait_secs = 0
                flow_sequence.append({**action, "expected_failure": False})
                continue

            # Any real browser action resets the consecutive-wait counter.
            consecutive_wait_secs = 0

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

        if abort:
            break

        # Drain backend calls made during this batch; pass to agent next turn.
        network_activity = browser.drain_network_log() or None

        for nf in new_failures:
            pending_failures = [f for f in pending_failures if not same_target(f["action"], nf["action"])]
            pending_failures.append(nf)
    else:
        print(f"[Executor] Reached MAX_DISCOVERY_ITERATIONS ({MAX_DISCOVERY_ITERATIONS}) without a terminal state.")

    return flow_sequence

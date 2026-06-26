import json

from google.genai import types

from . import db, embeddings, select_strategies
from .agent import client, MODEL_NAME
from .select_strategies import RecipeFailed
from .utils import _values_match

MAX_STEPS = 10
MAX_FULL_SNAPSHOTS = 2

OP_VOCABULARY = """\
- {"op": "click_target"} -- click the widget itself.
- {"op": "type", "text": "<literal text>"} -- type this literal text into whatever currently has focus.
- {"op": "key", "key": "Enter"|"ArrowDown"|"ArrowUp"|"Escape"|"Tab"|"Space"} -- press a single key.
- {"op": "clear"} -- select-all and delete the current value of the focused field.
- {"op": "click_option", "label": "<literal visible text>"} -- click the option/element with this exact visible text.

Control ops (never stored in the final recipe):
- {"op": "request_full_snapshot"} -- get a full-page accessibility snapshot on the NEXT turn. Use this
  when you suspect the widget's options render elsewhere on the page (e.g. a portal/overlay) and
  aren't visible in the local snapshot.
- {"op": "done", "chosen_label": "<literal text now shown for this field>", "description": "<plain-language summary of what you did, why, and where the value came from>"} --
  you believe the field now correctly shows the desired value.
- {"op": "give_up", "reason": "<why nothing works -- e.g. list the real available options you saw>"} --
  nothing you tried works and you have no more ideas.
"""


def _log_usage(logger, label, prompt, response):
    if logger is None:
        return
    usage = response.usage_metadata
    logger.log_llm_call(label, prompt, response.text, {
        "model": MODEL_NAME,
        "prompt_token_count": getattr(usage, "prompt_token_count", None),
        "candidates_token_count": getattr(usage, "candidates_token_count", None),
        "cached_content_token_count": getattr(usage, "cached_content_token_count", None),
        "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
        "tool_use_prompt_token_count": getattr(usage, "tool_use_prompt_token_count", None),
        "total_token_count": getattr(usage, "total_token_count", None),
    })


def _build_discover_prompt(role, name, signature, value, local_snap, full_snap, transcript, hints, cross_site_hints, stale):
    parts = [f"""
You are a specialist sub-agent that figures out how to operate a single dropdown/combobox-style
form widget by emitting ONE low-level UI action ("op") per turn. You do not fill in the rest of
the form -- only this one widget.

### TARGET ELEMENT ###
role: {role!r}
accessible name: {name!r}
structural signature: {signature!r}

### DESIRED VALUE ###
{value!r}

### LOCAL SNAPSHOT (accessibility tree around the target element) ###
{local_snap}
"""]

    if full_snap:
        parts.append(f"""
### FULL PAGE SNAPSHOT (requested) ###
{full_snap}
""")

    if stale:
        parts.append(f"""
### A PREVIOUSLY-DISCOVERED RECIPE FOR THIS EXACT FIELD JUST FAILED ###
recipe: {json.dumps(stale.get("recipe"))}
description: {stale.get("description")!r}
failure: {stale.get("reason")}
The page has likely changed shape since that recipe was recorded. Discover a fresh sequence of
ops from scratch; do not assume the old recipe's ops still apply, though its description may
still hint at the right general approach.
""")

    if hints:
        hint_lines = []
        for h in hints:
            hint_lines.append(
                f"- {h['role']} \"{h['name']}\" on {h['domain']} (was set to {h['value']!r}): {h['description']}"
            )
        parts.append("""
### HINTS FROM OTHER PREVIOUSLY-DISCOVERED FIELDS ON THIS SITE (inspiration only) ###
These are descriptions of how other widgets on this same site were handled (e.g. earlier in this
same run, or in a past run). They are for a DIFFERENT field/value -- do NOT replay their ops
verbatim. Use them only to inform your approach for THIS widget and value.
""" + "\n".join(hint_lines) + "\n")

    if cross_site_hints:
        hint_lines = []
        for h in cross_site_hints:
            hint_lines.append(
                f"- {h['role']} \"{h['name']}\" on {h['domain']} (was set to {h['value']!r}): {h['description']}"
            )
        parts.append("""
### HINTS FROM SIMILAR FIELDS ON OTHER SITES (inspiration only) ###
These are descriptions of how similarly-named/valued widgets were handled on DIFFERENT sites.
The page structure there is almost certainly different -- do NOT replay their ops verbatim, and
do NOT assume this widget behaves the same way. Use them only to understand what kind of answer
this field is likely looking for (e.g. how a value like this maps to an option's wording).
""" + "\n".join(hint_lines) + "\n")

    parts.append(f"""
### DISCOVERY STRATEGIES ###
Widgets vary widely. If the basic sequence (click_target → type → click_option) does not work,
try these escalating approaches — in order:

1. TYPE VARIATIONS — if typing the full value shows no options:
   - clear, then type just the first 1-3 characters (autocomplete often triggers earlier)
   - type a single space or press Backspace once after typing to re-trigger the dropdown
   - try a different substring (e.g. middle of the value, not the start)

2. KEYBOARD TRIGGERS — if typing alone doesn't open the list:
   - press ArrowDown immediately after click_target (opens many native-style pickers)
   - press Space to toggle/expand the widget without typing
   - press Enter after typing to fire a search/lookup (search-style comboboxes)

3. OPTION NOT VISIBLE IN LOCAL SNAPSHOT:
   - use request_full_snapshot — some widgets render their option list in a DOM portal far
     from the widget itself (react-select, Workday-style overlays); the full snapshot will
     reveal it
   - if you can see the list but not the exact option, try a shorter/different search term
   - look in the local snapshot for a nearby "Search" button or magnifying-glass icon and
     click it (some widgets need an explicit trigger)

4. RECOVERY / RESET:
   - press Escape to close a stuck or stale dropdown, then restart from click_target
   - clear + click_target to get back to a clean open state before retrying

5. KEYBOARD ACCEPTANCE:
   - if the desired option is highlighted in the list (visible in transcript/snapshot),
     pressing Enter often selects it — try this before click_option
   - Tab after typing sometimes confirms the typed value in plain-text comboboxes

6. TRANSCRIPT CHECK:
   - if a previous op in the transcript already shows the option list, use click_option
     with the EXACT text that appeared (match capitalisation, accents, punctuation)
   - never invent option text that was not visible in a snapshot

### OP VOCABULARY -- return EXACTLY ONE of the following as a JSON object ###
{OP_VOCABULARY}
""")

    if transcript:
        parts.append(f"""
### OPS TRIED SO FAR ON THIS WIDGET, THIS RUN ###
{json.dumps(transcript, indent=2)}
""")

    parts.append("Return ONLY a single JSON object for your next op. No markdown, no explanations.")

    return "\n".join(parts)


async def run_recipe(page, locator, name, nth, recipe, chosen_label):
    """Replays a previously-discovered, literal-valued recipe. Raises
    RecipeFailed (carrying the stale recipe for re-discovery) if any op
    fails or the final state doesn't reflect `chosen_label`."""
    try:
        for op in recipe:
            await select_strategies.apply_op(page, locator, op)
    except Exception as e:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        raise RecipeFailed(f"recipe op failed for '{name}': {e}", stale_recipe=recipe)

    target = chosen_label or ""
    context = await select_strategies._combobox_context(page, name, nth)
    for _ in range(8):
        if _values_match(context, target) or select_strategies._label_fragment_present(context, target):
            return
        await page.wait_for_timeout(100)
        context = await select_strategies._combobox_context(page, name, nth)

    raise RecipeFailed(
        f"recipe executed for '{name}' but {target!r} is not reflected on the page: {context!r}",
        stale_recipe=recipe,
    )


async def discover(browser, action, domain, logger=None, iteration=None, stale=None, hints=None, cross_site_hints=None) -> dict:
    """Runs a ReAct loop that discovers, from scratch, a sequence of
    primitive ops that sets the widget to the desired value. Returns
    {"success": True, "recipe": [...], "chosen_label": ..., "description": ...}
    or {"success": False, "error": ...}."""
    role = action.get("role")
    name = action.get("name")
    value = action.get("value")
    nth = action.get("nth")
    locator = browser.locator_for(role, name, nth)
    signature = await select_strategies.compute_signature(locator)

    recipe = []
    transcript = []
    full_snapshot_requests = 0
    request_full_next = stale is not None

    for step in range(1, MAX_STEPS + 1):
        local_snap = await select_strategies.local_snapshot(locator)
        full_snap = None
        if request_full_next and full_snapshot_requests < MAX_FULL_SNAPSHOTS:
            full_snap = await browser.snapshot()
            full_snapshot_requests += 1
        request_full_next = False

        prompt = _build_discover_prompt(role, name, signature, value, local_snap, full_snap, transcript, hints, cross_site_hints, stale)
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        _log_usage(logger, f"{iteration}_step_{step}", prompt, response)

        try:
            op = json.loads(response.text)
        except Exception:
            transcript.append({"op": None, "result": "failed: model did not return valid JSON"})
            continue

        kind = op.get("op")

        if kind == "request_full_snapshot":
            request_full_next = True
            transcript.append({"op": op, "result": "ok (full page snapshot will be shown next turn)"})
            continue

        if kind == "give_up":
            return {"success": False, "error": op.get("reason") or f"selector agent gave up on '{name}'"}

        if kind == "done":
            chosen_label = op.get("chosen_label", "")
            description = op.get("description", "")
            context = await select_strategies._combobox_context(browser.page, name, nth)
            for _ in range(8):
                if _values_match(context, chosen_label) or select_strategies._label_fragment_present(context, chosen_label):
                    return {"success": True, "recipe": recipe, "chosen_label": chosen_label, "description": description}
                await browser.page.wait_for_timeout(100)
                context = await select_strategies._combobox_context(browser.page, name, nth)
            transcript.append({"op": op, "result": (
                f"verification failed: chosen_label {chosen_label!r} does not match what the field currently "
                f"shows: {context!r}. If your ops already produced the right result, do NOT try new ops -- "
                f"just emit 'done' again with chosen_label set EXACTLY to the text shown for this field above "
                f"(e.g. a dial code like '+91' rather than a country name, if that's what's displayed)."
            )})
            continue

        # Primitive op
        try:
            await select_strategies.apply_op(browser.page, locator, op)
            recipe.append(op)
            transcript.append({"op": op, "result": "ok"})
        except Exception as e:
            transcript.append({"op": op, "result": f"failed: {e}"})

    try:
        await browser.page.keyboard.press("Escape")
    except Exception:
        pass
    return {"success": False, "error": f"selector agent exhausted {MAX_STEPS} steps without success for '{name}'"}


async def resolve(browser, action, domain, logger=None, iteration=None, stale=None) -> dict:
    """Resolves a "select"/"combobox_select" action.

    If `stale` is None and a recipe is cached for this exact field with the
    same `value`, replays it literally. Otherwise (no cache, value changed,
    or the cached recipe just failed) runs a fresh discovery, given hints
    from semantically-similar past recipes (cross-domain/cross-field --
    inspiration only, never replayed directly).

    Returns {"success": True, "recipe": [...], "chosen_label": ..., "description": ...}
    or {"success": False, "error": ...}.
    """
    role = action.get("role")
    name = action.get("name")
    value = action.get("value")
    nth = action.get("nth")
    locator = browser.locator_for(role, name, nth)
    signature = await select_strategies.compute_signature(locator)

    cached = db.get_recipe(domain, role, name)

    if stale is None and cached and cached["value"] == value:
        try:
            await run_recipe(browser.page, locator, name, nth, cached["recipe"], cached["chosen_label"])
            return {"success": True, "recipe": cached["recipe"], "chosen_label": cached["chosen_label"], "description": cached["description"]}
        except RecipeFailed as e:
            stale = {"recipe": e.stale_recipe, "description": cached["description"], "reason": str(e)}

    hints = []
    cross_site_hints = []
    if cached and cached["value"] != value:
        hints.append({
            "domain": domain, "role": role, "name": name, "value": cached["value"],
            "description": cached["description"],
        })

    try:
        query_embedding = embeddings.embed_text(f"role={role} signature={signature}\nvalue={value}")
        for row in db.find_similar_recipes(query_embedding, domain=domain, exclude=(domain, role, name), top_k=3):
            hints.append({
                "domain": row["domain"], "role": row["role"], "name": row["name"], "value": row["value"],
                "description": row["description"],
            })
        base_domain = domain.split("#")[0]
        for row in db.find_similar_recipes(query_embedding, exclude_base_domain=base_domain, top_k=3):
            cross_site_hints.append({
                "domain": row["domain"], "role": row["role"], "name": row["name"], "value": row["value"],
                "description": row["description"],
            })
    except Exception as e:
        print(f"[SelectorAgent] Vector search unavailable, continuing without hints: {e}")

    result = await discover(browser, action, domain, logger=logger, iteration=iteration, stale=stale,
                             hints=hints or None, cross_site_hints=cross_site_hints or None)

    if result["success"]:
        try:
            embed_input = f"role={role} signature={signature}\nvalue={value}\n{result['description']}"
            embedding = embeddings.embed_text(embed_input)
        except Exception as e:
            print(f"[SelectorAgent] Embedding unavailable, saving recipe without it: {e}")
            embedding = None
        db.save_recipe(domain, role, name, signature, value, result["recipe"], result["chosen_label"], result["description"], embedding)
        return {"success": True, "recipe": result["recipe"], "chosen_label": result["chosen_label"], "description": result["description"]}

    return {"success": False, "error": result["error"]}

# form-filler-agent

An agent that fills out job-application forms (Greenhouse, etc.) end-to-end
using Playwright + Gemini, with a caching layer so repeat runs against the
same site become near-instant, deterministic replays instead of fresh LLM
reasoning.

## High-level flow

1. `executor.process_form(url, force_refresh=False, cache_domain=None)` is
   the single entry point.
2. **Cache lookup**: `cache_key = cache_domain or domain` (see "Cache
   isolation" below). If a `cached_flows` row exists for `cache_key` and
   `force_refresh` is False, go to **Replay**. Otherwise go to **Discovery**.
3. **Discovery** (`executor.discover_flow`): a ReAct loop --
   - snapshot the page (`browser.snapshot()`, full-page ARIA tree)
   - `agent.run_react_agent()` (Gemini) returns a JSON array of actions for
     this page state
   - each action is executed via `browser.execute_action()`
   - `combobox_select` (and any failed native `select`) actions raise
     `NeedsSelectorAgent` -> handled by `selector_agent.resolve()` (see
     below)
   - every attempted action (success or failure) is appended to
     `flow_sequence`, tagged `expected_failure: true/false`
   - loop ends when the agent returns a batch consisting of exactly one
     final-submit click, or an empty batch, or `MAX_DISCOVERY_ITERATIONS`
     (20) is hit
   - on success (`flow_has_final_submit`), the whole `flow_sequence` is
     persisted via `db.save_flow(cache_key, flow_sequence)`
4. **Replay** (`executor.process_form`'s replay loop): re-executes
   `flow_sequence` step by step via `browser.execute_action`. Steps tagged
   `expected_failure: true` are skipped (they failed during discovery too, by
   design -- e.g. a value with no matching option). Steps that fail
   unexpectedly trigger self-healing:
   - `NeedsSelectorAgent` / `RecipeFailed` -> `selector_agent.resolve(...,
     stale=...)` for that ONE field; on success the flow is patched in place
     and replay continues. On failure -> full-page self-heal.
   - any other exception -> full-page self-heal: re-run `discover_flow` from
     here with `error_context` describing the failure, splice the healed tail
     onto `executed`, save, and return `{"status": "healed_needs_restart"}`
     so the caller re-invokes `process_form` from scratch with the new flow.
5. Both discovery and replay stop (dry-run) at the final submit button --
   `is_final_submit()` intercepts it, takes a screenshot
   (`logger.final_screenshot_path()`), and returns `dry_run_complete` without
   actually clicking it.

## Module map (`src/`)

- **`agent.py`** -- the main ReAct agent. Loads `kb/profile.json` (candidate
  profile data) at import time. `build_system_prompt()` builds the big prompt
  (action schema, rules, profile data, current snapshot, previously-executed
  actions, error-recovery context). `run_react_agent()` calls
  `gemini-flash-latest` and parses the JSON action array. `client` (the
  `genai.Client`) and `MODEL_NAME` are reused by `selector_agent.py`.
- **`browser_client.py`** -- thin Playwright wrapper. `locator_for(role,
  name, nth)` = `page.get_by_role(role, name=name)[.nth(nth)]`.
  `execute_action()` dispatches by `action["action"]`:
  `fill`/`click`/`check`/`uncheck`/`upload` are direct Playwright calls with a
  post-condition assertion; `select`/`combobox_select` either replay an
  attached `recipe` via `selector_agent.run_recipe`, try
  `select_strategies.native_select` (for `select`), or raise
  `NeedsSelectorAgent`.
- **`select_strategies.py`** -- low-level building blocks for the selector
  agent:
  - `compute_signature(locator)` -- `"tagName|role|aria-haspopup|aria-autocomplete"`,
    a structural fingerprint used as part of the recipe cache key context.
  - `local_snapshot(locator)` -- the snapshot the selector agent sees by
    default. Takes the element's own `aria_snapshot()`, then walks 2/3/4
    ancestor levels (`locator.locator("xpath=" + "/".join([".."] * n))`)
    and keeps whichever snapshot has the most lines (catches sibling
    labels/error text/the rest of a custom widget). Then appends
    `_related_popup_snapshot()`.
  - `_related_popup_snapshot(locator)` -- finds a portal-rendered open
    listbox/menu "owned" by `locator` via the ARIA combobox-pattern
    relationship attributes (`aria-controls` / `aria-owns` /
    `aria-activedescendant` -> `closest('[role="listbox"],[role="menu"],...]')`),
    falling back to any visible `[role="listbox"]`/`[role="menu"]` on the
    page. This is what lets react-select-style portals (option list appended
    near `<body>`, not inside the combobox's DOM ancestry) show up in the
    LOCAL snapshot without a full-page snapshot request.
  - `apply_op(page, locator, op)` -- executes ONE primitive op: `click_target`,
    `type` (literal text via `keyboard.type`), `key` (single keypress),
    `clear` (Ctrl+A, Delete), `click_option` (click `role=option` by exact
    label, falling back to `get_by_text`).
  - `native_select` -- the only hardcoded strategy left; real `<select>`
    elements.
  - `listbox_click` / `type_and_enter` / `keyboard_nav` / `click_text_match`
    -- the OLD preset strategies, now unused/dead code (kept per a past
    decision to "let the code be there, but unlink" rather than delete).
  - `NeedsSelectorAgent`, `RecipeFailed`, `NoMatchingOption` -- exception
    types executor.py and browser_client.py react to.
- **`selector_agent.py`** -- the dropdown specialist. See "Selector agent"
  below.
- **`embeddings.py`** -- `embed_text(text)` (model `gemini-embedding-001`,
  via `agent.client`) and `cosine_similarity(a, b)`.
- **`db.py`** -- SQLite (`data/flows.db`). See "Database" below.
- **`run_logger.py`** -- `RunLogger(domain, data_dir)` creates
  `data/runs/<domain>/<timestamp>/` with `snapshots/`, `llm/`,
  `token_usage.json`, `final.png`. `next_iteration()` for discovery-loop
  numbering; `log_llm_call(label, prompt, response_text, usage)` writes
  `llm/<label>_prompt.txt` + `llm/<label>_response.json` and accumulates
  token-usage totals.
- **`utils.py`** -- `_normalize` (whitespace/case fold), `_values_match`
  (loose substring/alnum-stripped comparison used everywhere to verify an
  action "stuck"), `is_final_submit` / `flow_has_final_submit` (regex over
  button names, excluding "next/continue/back").
- **`executor.py`** -- orchestration. `process_form` (entry point) and
  `discover_flow` (the discovery ReAct loop), described above.

## Selector agent (`selector_agent.py`)

Replaces a brittle "pick from 5 hand-written strategies" design. For each
`combobox_select` (or failed native `select`), a specialist sub-agent
**discovers its own sequence of primitive UI ops** via a small ReAct loop
(`discover()`, max `MAX_STEPS=10` turns), then that exact literal-valued
sequence is cached and replayed directly next time (`run_recipe()`).

- **Op vocabulary** (`OP_VOCABULARY`): `click_target`, `type`, `key`,
  `clear`, `click_option` (the "recipe" primitives, literal values, no
  templating) plus control ops `request_full_snapshot` (full-page ARIA
  snapshot on the NEXT turn -- max `MAX_FULL_SNAPSHOTS=2` per discovery),
  `done` (`chosen_label` + `description`), `give_up` (`reason`).
- **`discover(browser, action, domain, logger, iteration, stale=None,
  hints=None, cross_site_hints=None)`** -- the ReAct loop. Each turn:
  `local_snapshot()` (+ full snapshot if requested/stale), build prompt
  (`_build_discover_prompt`), call Gemini, execute the returned op. On `done`,
  verifies `chosen_label` against `_combobox_context()` (a few lines of
  full-page snapshot around the field) via `_values_match` /
  `_label_fragment_present`; if it doesn't match, the transcript tells the
  model NOT to try new ops but to re-emit `done` with the label EXACTLY as
  shown (fixed a bug where the model would otherwise spiral into extra ops).
- **`run_recipe(page, locator, name, nth, recipe, chosen_label)`** -- replays
  a cached recipe's ops literally via `apply_op`, then does the same
  `chosen_label` verification. Raises `RecipeFailed(reason,
  stale_recipe=recipe)` on any op failure or verification failure.
- **`resolve(browser, action, domain, logger, iteration, stale=None)`** --
  the entry point executor.py calls:
  1. `cached = db.get_recipe(domain, role, name)`. If `stale is None` and
     `cached["value"] == value`, try `run_recipe` with the cached recipe --
     success returns immediately with **zero LLM calls**. Failure sets
     `stale` and falls through.
  2. Build **hints** (same-site) and **cross_site_hints** (other real sites)
     -- see "RAG hints" below.
  3. `discover(..., stale=stale, hints=hints, cross_site_hints=cross_site_hints)`.
  4. On success: embed `f"role={role} signature={signature}\nvalue={value}\n{description}"`
     and `db.save_recipe(...)` (upsert, bumps `success_count` on conflict).

### RAG hints (two separate sections in the discovery prompt)

`domain` here is whatever `executor.process_form` is using as `cache_key`
(real domain in production, or `<real-domain>#test` etc. for test runs).

1. **"HINTS FROM OTHER PREVIOUSLY-DISCOVERED FIELDS ON THIS SITE"** --
   `db.find_similar_recipes(query_embedding, domain=domain, exclude=(domain,
   role, name), top_k=3)`: cosine-similarity search **restricted to rows with
   this exact `domain`**. This is what lets sibling fields discovered earlier
   in the SAME run (their `save_recipe` already committed) show up as hints
   for later fields, and is what prevents a different namespace's
   already-solved identical field+value from handing a "fresh" discovery its
   answer (the original "cheating" problem in test runs). Also includes,
   unconditionally, the field's OWN cached recipe if it exists for a
   DIFFERENT `value` (e.g. re-discovering "notice period" for a new desired
   value, with the old value's description as a hint).
2. **"HINTS FROM SIMILAR FIELDS ON OTHER SITES"** --
   `db.find_similar_recipes(query_embedding, exclude_base_domain=domain.split("#")[0],
   top_k=3)`: global search EXCLUDING every namespace of the current real
   site (strips any `#...` suffix before comparing). Framed in the prompt as
   "different DOM, don't replay ops, use only for what kind of answer this
   field wants". **Currently always empty** -- the DB only has
   `job-boards.greenhouse.io` (+ its `#test` namespace) rows. It will start
   populating once a second real ATS domain is discovered.

## Database (`data/flows.db`, `src/db.py`)

- **`cached_flows`**: `domain UNIQUE`, `mcp_tool_sequence` (JSON list of
  actions, the full discovery transcript), `last_updated`.
  `get_cached_flow` / `save_flow` / `delete_flow`.
- **`select_recipes`**: `UNIQUE(domain, role, name)`. Columns: `signature`,
  `value` (the value this recipe was discovered FOR), `recipe` (JSON list of
  primitive ops), `chosen_label`, `description` (plain-language, also the
  embedding input), `embedding` (JSON float list), `success_count`,
  `last_used`. `get_recipe` / `save_recipe` (upsert, bumps `success_count`) /
  `delete_recipe`.
- **`find_similar_recipes(embedding, domain=None, exclude_base_domain=None,
  exclude=None, top_k=3)`** -- brute-force cosine similarity in Python over
  `select_recipes` rows. `domain` restricts to exact-domain rows (same-site
  hints); `exclude_base_domain` excludes all `#`-suffixed namespaces of that
  base domain (cross-site hints).

### Cache isolation (`cache_domain` param)

`process_form(url, force_refresh=False, cache_domain=None)`:
`cache_key = cache_domain or domain`. ALL flow/select-recipe cache reads,
writes, and deletes use `cache_key`. `RunLogger` and `browser.navigate` always
use the REAL `domain`/`url`. This lets test runs point every cache operation
at an isolated namespace (e.g. `job-boards.greenhouse.io#test`) while still
hitting the real target site and logging under its real directory --
production's `job-boards.greenhouse.io` cache rows are never read, cleared, or
overwritten by test runs.

## Test suite (root `*.py`)

All testing against the Greenhouse demo job uses ONE shared isolated
namespace, defined in **`test_inputs.py`**:
- `TARGET_URL`, `REAL_DOMAIN`, `TEST_DOMAIN = REAL_DOMAIN + "#test"`,
  `SELECT_FIELDS` (the 4 combobox fields on this form: Country, years of
  experience, relocating, notice period -- each as `(role, name, value,
  nth)`).

**Standard pipeline** (run in this order on a fresh checkout / after schema
changes):
1. **`test_full_discovery.py`** -- `delete_recipe(TEST_DOMAIN, role, name)`
   for each `SELECT_FIELDS` entry, then `process_form(TARGET_URL,
   force_refresh=True, cache_domain=TEST_DOMAIN)`. Forces every combobox
   through `selector_agent.discover()`'s full ReAct loop (no exact-cache
   shortcuts). Expect `dry_run_complete`, 4x fresh discoveries logged as
   `iter_<n>-<m>_step_*`.
2. **`test_select_cached.py`** -- `process_form(TARGET_URL,
   force_refresh=True, cache_domain=TEST_DOMAIN)` (flow cache cleared, select
   recipes NOT cleared). Re-discovers the flow; combobox actions should hit
   exact-cache-recipe replays (zero LLM calls) UNLESS the main agent picks a
   different value than last time for some field (then that one field
   re-discovers, with same-site hints from its siblings).
3. **`test_cached.py`** -- `process_form(TARGET_URL, cache_domain=TEST_DOMAIN)`,
   no `force_refresh`. Pure cached-flow replay -- every action shows `(recipe)`
   where applicable, zero LLM calls, `dry_run_complete`.

**`test_selector_agent.py`** -- standalone, only needed when iterating on
`selector_agent.py`/`select_strategies.py` in isolation, with NO main agent.
Part 1 (`discover_part`): for each `SELECT_FIELDS` entry, clears its
`TEST_DOMAIN` recipe and calls `selector_agent.resolve()` directly. Part 2
(`replay_part`): re-navigates to a clean page and replays each discovered
recipe via `browser.execute_action({**action, "recipe":..., "chosen_label":...})`
-- no agent/LLM involved.

`test_new.py` was removed (superseded by the pipeline above).

## Logging / run artifacts

Every `process_form` call creates `data/runs/<real-domain>/<YYYYMMDD_HHMMSS>/`:
- `snapshots/iter_NN.txt` -- full-page ARIA snapshot at each discovery
  iteration.
- `llm/iter_NN_prompt.txt` + `_response.json` -- main-agent calls.
- `llm/iter_<N>-<M>_step_<S>_prompt.txt` + `_response.json` -- selector-agent
  calls for the M-th combobox encountered in main-agent iteration N, step S
  of its ReAct loop.
- `llm/replay-<i>_step_<S>_*` -- selector-agent calls during cached-flow
  replay (field-level re-discovery after a `RecipeFailed`).
- `token_usage.json` -- per-call + totals for prompt/candidates/cached/thoughts/
  tool-use/total token counts.
- `final.png` / `after_discover.png` -- screenshots.

## Config

- `.env`: `GEMINI_API_KEY`, `GOOGLE_API_KEY` (either works; `genai.Client`
  picks one up -- if both set, a startup message says which is used).
- `kb/profile.json` -- the candidate profile data injected into the main
  agent's prompt (personal details, experience answers, etc.). `kb/resume.pdf`
  is the file `upload` actions attach.
- Model: `gemini-flash-latest` for both the main agent and selector agent
  (`agent.MODEL_NAME`); `gemini-embedding-001` for embeddings.

## Known follow-ups / open questions

- Cross-site RAG hints section exists but is currently always empty (only one
  real site's data exists). Worth re-checking once a second ATS domain has
  been discovered, to confirm the section renders sensibly and the
  "conceptual inspiration only" framing holds up with real different-DOM
  examples.
- The 4 old preset strategies in `select_strategies.py`
  (`listbox_click`/`type_and_enter`/`keyboard_nav`/`click_text_match`) are
  dead code, intentionally kept rather than deleted.
- `readme.md` is stale (references `test.py`, `mcp_client.py` which no longer
  exist/aren't used the same way) -- this CLAUDE.md supersedes it for
  architecture purposes.

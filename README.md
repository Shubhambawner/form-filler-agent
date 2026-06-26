# Form Filler Agent

An autonomous agent that fills out job-application forms end-to-end — Greenhouse, Workday, Rippling, iCIMS, and others — using a Playwright-driven browser and Gemini Flash as the reasoning engine.

The core design goal is **run-cost asymmetry**: the first run against a form does full LLM reasoning (discovery); every subsequent run replays a cached action sequence with zero LLM calls. A self-healing layer ensures the cache stays valid as forms change.

![Agent filling a Visa/Workday job application form](image.png)

---

## Example Run — Rippling ATS (Discovery Mode)

A complete trace of a real discovery run against `ats.rippling.com` — **2 main-agent iterations, 3 selector-agent steps, 15,527 tokens total, dry-run complete in one pass.**

<details>
<summary><strong>Iteration 1 — Agent sees blank form, plans 10 actions</strong></summary>

**ARIA snapshot the agent receives (`snapshots/iter_01.txt`):**
```
- heading "Application: Software Engineer 2 - AI Platform"
- button "Exit to job board"
- progressbar "Progress bar"
- text: Résumé* Total 0 file selected
- button "Drop or select (.doc / .docx / .pdf)"
- textbox "First name"
- textbox "Last name"
- textbox "Email"
- combobox "Pronouns"
- textbox "Current company"
- combobox "Search": +1 US         ← phone country code, wrong default
- textbox "Phone number"
- textbox "Location"
- textbox "LinkedIn Link"
- button "Drop or select (.doc / .docx / .pdf)"   ← cover letter
- radiogroup:
  - radio "Yes - I consent to receiving text messages"
  - radio "No - I do not consent to receiving text messages"
- button "Apply" [disabled]
```

**Agent decision (`llm/iter_01_response.json`) — 6,402 tokens:**
```json
[
  { "action": "upload",           "role": "button",   "name": "Drop or select (.doc / .docx / .pdf)", "value": "D:\\...\\resume.pdf", "nth": 0 },
  { "action": "fill",             "role": "textbox",  "name": "First name",     "value": "Shubham" },
  { "action": "fill",             "role": "textbox",  "name": "Last name",      "value": "Bawner" },
  { "action": "fill",             "role": "textbox",  "name": "Email",          "value": "shubhambawner.official@gmail.com" },
  { "action": "fill",             "role": "textbox",  "name": "Current company","value": "Texas Instruments" },
  { "action": "combobox_select",  "role": "combobox", "name": "Search",         "value": "+91" },
  { "action": "fill",             "role": "textbox",  "name": "Phone number",   "value": "7410730191" },
  { "action": "fill",             "role": "textbox",  "name": "Location",       "value": "Bengaluru, India" },
  { "action": "fill",             "role": "textbox",  "name": "LinkedIn Link",  "value": "https://www.linkedin.com/in/shubham-bawner-456131205/" },
  { "action": "click",            "role": "radio",    "name": "Yes - I consent to receiving text messages" }
]
```

The `combobox_select` on `"Search"` triggers the selector sub-agent (see below). All other actions execute directly via Playwright.

</details>

<details>
<summary><strong>Selector sub-agent — discovering how to set the phone country code to +91 (3 steps)</strong></summary>

The `"Search"` combobox is a custom widget (not a native `<select>`). The sub-agent explores it in a ReAct loop, emitting one primitive op per turn.

**Step 1 — `llm/iter_1-1_step_1_response.json` (985 tokens):**
```json
{ "op": "click_target" }
```
Clicks the combobox to open the dropdown.

**Step 2 — `llm/iter_1-1_step_2_response.json` (1,310 tokens):**
```json
{ "op": "click_option", "label": "+91 IN - India" }
```
Local snapshot after `click_target` showed the option list. Agent clicks the exact label.

**Step 3 — `llm/iter_1-1_step_3_response.json` (1,181 tokens):**
```json
{
  "op": "done",
  "chosen_label": "+91 IN",
  "description": "Clicked the combobox and selected the '+91 IN - India' option, which is now successfully displayed as '+91 IN' in the combobox."
}
```
Verification passes (`+91 IN` is present in the field context). **Recipe `[click_target, click_option "+91 IN - India"]` is saved to `select_recipes`.** Future runs for the same field replay this directly with zero LLM calls.

</details>

<details>
<summary><strong>Iteration 2 — Agent verifies filled state, returns final submit</strong></summary>

**ARIA snapshot the agent receives (`snapshots/iter_02.txt`) — every field now populated:**
```
- text: Résumé* Total 1 file selected
- status: File resume.pdf uploaded successfully
- textbox "First name": Shubham
- textbox "Last name": Bawner
- textbox "Email": shubhambawner.official@gmail.com
- textbox "Current company": Texas Instruments
- combobox "Search": +91 IN          ← corrected from +1 US
- textbox "Phone number": 74107 30191
- textbox "Location": Bengaluru, India
- textbox "LinkedIn Link": https://www.linkedin.com/in/shubham-bawner-456131205/
- radio "Yes - I consent to receiving text messages" [checked]
- button "Apply"                      ← now enabled
```

**Agent decision (`llm/iter_02_response.json`) — 5,649 tokens:**
```json
[{ "action": "click", "role": "button", "name": "Apply" }]
```

`is_final_submit()` intercepts this (lone submit button action), takes a screenshot, and returns `dry_run_complete` **without clicking**. The complete action sequence is stored as a flow variant.

</details>

<details>
<summary><strong>Final screenshot + token summary</strong></summary>

**`final.png` — captured at the moment of interception:**

![Rippling application form fully filled, Apply button ready](data/runs/ats.rippling.com/20260619_002933/final.png)

**Token usage (`token_usage.json`):**

| Call | Role | Tokens |
|------|------|--------|
| iter 1 — main agent | Plans 10 actions from blank form | 6,402 |
| iter 1-1 step 1 — selector agent | `click_target` | 985 |
| iter 1-1 step 2 — selector agent | `click_option "+91 IN - India"` | 1,310 |
| iter 1-1 step 3 — selector agent | `done` + verification | 1,181 |
| iter 2 — main agent | Sees filled form, returns submit | 5,649 |
| **Total** | | **15,527** |

Next run against the same form: **0 LLM calls** — the flow variant and the `+91` recipe both replay from cache.

</details>

---

## How It Works

### Two-Phase Execution

```
process_form(url)
    │
    ├─ navigate() → initial ARIA snapshot → embed field signature
    │
    ├─ [Cache hit]  ──► Replay phase  ──► dry_run_complete  (0 LLM calls)
    │
    └─ [Cache miss] ──► Discovery phase ──► save variant ──► dry_run_complete
```

**Discovery** is a ReAct loop: snapshot → Gemini reasons → execute actions → repeat until the final submit button appears alone. The full action transcript is stored as a *flow variant* keyed to the page's initial ARIA snapshot embedding.

**Replay** re-executes the stored transcript step-by-step. Steps that failed during discovery (e.g. a value with no matching option) are tagged `expected_failure: true` and skipped cleanly. Unexpected failures trigger surgical self-healing before falling back to a full re-discovery.

### Flow Variants and Embedding Matching

A single ATS domain (`job-boards.greenhouse.io`) hosts thousands of listings. Early versions stored one flow per domain and overwrote it on every heal — two different listings would ping-pong the cache forever.

The fix: each discovered flow is stored as its own *variant* row, keyed by the page's initial-state embedding. `find_best_flow()` retrieves the variant whose starting page was most semantically similar to the current one.

The embedding input is `extract_field_signature()` — the snapshot reduced to one `"role: name"` line per form field, dropping headings and buttons that are identical across all listings. This makes the embedding sensitive to *field set differences* (what actually distinguishes variants) while robust to job-title interpolation in field names ("...as a DevRel Engineer?" vs "...as a Backend Engineer?").

A cosine similarity threshold (`SAME_VARIANT_THRESHOLD = 0.97`) determines whether a re-discovered flow updates an existing variant in place or becomes a new row — this is what breaks the L1/L2 ping-pong.

### Selector Sub-Agent

Custom dropdown widgets (react-select, Workday overlays, etc.) are not trivially automatable — the right sequence of clicks, key presses, and waits varies by widget. Hardcoding strategies breaks constantly.

Instead, a specialist sub-agent discovers the interaction recipe for each field via its own ReAct loop (up to 10 steps):

```
click_target → type "India" → [local snapshot shows option list] → click_option "India" → done
```

The **recipe** — a literal-valued sequence of primitive ops (`click_target`, `type`, `key`, `clear`, `click_option`) — is cached per `(domain, role, name)`. Future encounters replay it with zero LLM calls. If the page changes and the recipe fails, `RecipeFailed` is raised with the stale recipe attached, triggering a fresh discovery that can use the old recipe's description as a hint.

**RAG hints** are injected into each discovery prompt in two tiers:
1. *Same-site hints* — semantically similar recipes already discovered for other fields on this domain (including siblings discovered earlier in the same run). Scoped to exact domain to prevent cross-site "cheating."
2. *Cross-site hints* — similar recipes from other real ATS domains, framed as "what kind of answer this field expects, not how to operate this specific widget."

The selector agent also handles the tricky case of portaled option lists (react-select appends the dropdown near `<body>`, outside the combobox's DOM ancestry) via `request_full_snapshot` and `_related_popup_snapshot()` which follows ARIA combobox-pattern relationships (`aria-controls`/`aria-owns`/`aria-activedescendant`).

### Self-Healing

```
Replay step fails
    │
    ├─ NeedsSelectorAgent / RecipeFailed ──► selector_agent.resolve(stale=...)
    │       success ──► patch action in-place, save updated variant, continue
    │       failure ──► full-page self-heal ▼
    │
    └─ Any other exception ──► discover_flow(error_context=...) from current page
            ──► splice healed tail onto executed prefix
            ──► save merged variant
            ──► return healed_needs_restart (caller restarts with new flow)
```

Self-healing carries the `error_context` (failed action + details) into the next discovery, so Gemini knows exactly what went wrong and can correct its value choice or action type.

### Login / Signup Agent

When the main agent encounters an auth gate it signals `needs_login`. A specialist agent takes over with access to stored credentials (login path) or a signup-relevant profile subset (registration path). It supports:
- Credential-based login
- Fresh account registration with credential persistence for future runs
- OTP/CAPTCHA escalation back to the user
- On-demand full-profile injection when signup asks for more data

Credentials are stored per `cache_key` (same namespace isolation as flows and recipes), never in `profile.json` which the main agent also sees.

### Anti-Detection

The browser is launched with bot fingerprint evasion (`--disable-blink-features=AutomationControlled`, `navigator.webdriver` hidden). Fills use clipboard paste via the Web Clipboard API rather than synthetic key events; per-character typing is a fallback for paste-blocked inputs. Clicks are preceded by natural multi-waypoint mouse movement with random offsets and delays.

---

## Architecture

```
src/
├── executor.py          # Orchestration: process_form(), discover_flow()
├── agent.py             # Main ReAct agent — Gemini Flash, ARIA snapshot → action batch
├── selector_agent.py    # Dropdown specialist — recipe discovery, replay, RAG hints
├── browser_client.py    # Playwright wrapper — snapshot, execute_action, network log
├── select_strategies.py # Low-level widget primitives — apply_op, local_snapshot, etc.
├── login_agent.py       # Auth specialist — login, signup, credential persistence
├── db.py                # SQLite — flow variants + select recipes, cosine search
├── embeddings.py        # gemini-embedding-001 via genai.Client, cosine_similarity
├── run_logger.py        # Per-run artifact tree — snapshots, LLM calls, token usage
├── credentials.py       # credentials.json load/save
└── utils.py             # _values_match, is_final_submit, extract_field_signature

kb/
├── profile.json         # Candidate profile injected into the main agent's prompt
└── resume.pdf           # Attached by upload actions

data/
├── flows.db             # SQLite cache (flow variants + select recipes)
└── runs/<domain>/<ts>/  # Per-run logs: snapshots, LLM prompts/responses, token usage
```

**Models used:**
- `gemini-flash-latest` — main agent, selector agent, login agent
- `gemini-embedding-001` — flow variant embeddings, recipe RAG search

---

## Setup

```bash
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# Add your GEMINI_API_KEY to .env

# Edit kb/profile.json with your details
# Place your resume at kb/resume.pdf
```

---

## Usage

```python
import asyncio
from src.executor import process_form

# First run: full discovery (LLM reasoning)
result = await process_form("https://job-boards.greenhouse.io/company/jobs/12345")
# → {"status": "dry_run_complete"}  (stops before final submit, takes screenshot)

# Subsequent runs: pure replay (zero LLM calls)
result = await process_form("https://job-boards.greenhouse.io/company/jobs/12345")
# → {"status": "dry_run_complete"}

# Force re-discovery (e.g. form changed)
result = await process_form(url, force_refresh=True)
```

The agent always dry-runs — it intercepts the final submit button, takes a `final.png` screenshot, and returns without clicking. Review the screenshot before uncommenting actual submission.

---

## Test Suite

All tests share an isolated `#test` cache namespace (`TEST_DOMAIN = real_domain + "#test"`), so test runs never touch the production cache.

Run in this order after a fresh checkout or schema change:

| Script | What it exercises |
|--------|-------------------|
| `test_full_discovery.py` | Full LLM discovery with all select-recipe caches cleared — exercises every code path |
| `test_select_cached.py` | Force-refresh flow discovery, but keep select recipes — verifies recipe replay (zero selector-agent LLM calls) |
| `test_cached.py` | Pure cached replay — zero LLM calls end-to-end |
| `test_selector_agent.py` | Selector agent in isolation (no main agent), direct `resolve()` calls |

---

## Key Design Decisions

**Why embeddings instead of exact-string or Jaccard matching for flow variants?** ATS templates interpolate job titles into field names. Exact matching would create a new variant for every listing; Jaccard would too for long strings. Embeddings are robust to single-token substitutions (the job title) while still being sensitive to genuinely different field sets (what actually distinguishes an "apply now" form from a "work sample" form on the same domain).

**Why per-field recipe caching instead of per-page widget strategies?** Widget DOM structure varies by vendor version and page. A per-field recipe cache hits exactly when the field is the same even if other fields changed, and misses exactly when it needs to rediscover. Coarser granularity would over-invalidate; finer granularity (per-field-per-value) would under-reuse.

**Why namespace isolation (`domain#test`) instead of a separate test database?** It lets test runs exercise the real query and storage paths through the same `db.py` functions, confirming that cache namespacing works correctly. A separate test DB would hide bugs in the scoping logic.

**Why does `force_refresh` not pre-delete existing variants?** Deletion is destructive and non-idempotent. The save-dedup logic at the end (cosine similarity ≥ 0.97 → update in place; else → new variant) handles convergence correctly without needing a delete step.

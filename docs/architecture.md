# Architecture

```mermaid
flowchart TD
    START(["process_form(url)"])
    START --> NAV["navigate · ARIA snapshot"]
    NAV --> EMBED["embed field signature\ngemini-embedding-001"]
    EMBED --> LOOKUP[/"find_best_flow — cosine similarity"\]

    LOOKUP -- "no match" --> DISC
    LOOKUP -- "match found" --> REPLAY

    subgraph DISC ["  Discovery — LLM-driven  "]
        direction TB
        D1["snapshot ARIA tree"] --> D2["Gemini Flash\nplan action batch"]
        D2 --> D3{"action type"}
        D3 -- "fill · click\ncheck · upload" --> D4["Playwright\nexecute"]
        D3 -- "combobox_select" --> SEL
        D3 -- "needs_login" --> AUTH

        subgraph SEL ["Selector Sub-Agent"]
            S1{"recipe cached?"} -- yes --> S2["replay ops\n0 LLM calls"]
            S1 -- no --> S3["ReAct loop\ndiscover op sequence"]
            S3 --> S4["save to select_recipes"]
        end

        subgraph AUTH ["Login Agent"]
            A1["login / signup\ncredential persistence"]
        end

        D4 & S2 & S4 & A1 --> DFINAL{"final submit?"}
        DFINAL -- no --> D1
        DFINAL -- yes --> DSAVE["save flow variant\ncached_flows + embedding"]
    end

    subgraph REPLAY ["  Replay — deterministic, 0 LLM calls  "]
        direction TB
        R1["execute cached step"] --> RFAIL{"step fails?"}
        RFAIL -- no --> RFINAL{"final submit?"}
        RFAIL -- "recipe broken" --> RHEAL1["Selector Sub-Agent\nre-discover · stale hint"]
        RFAIL -- "other error" --> RHEAL2["full-page self-heal\ndiscover_flow from here"]
        RHEAL1 -- success --> RFINAL
        RHEAL1 -- fail --> RHEAL2
        RHEAL2 --> RSAVE["splice healed tail\nsave merged variant"]
        RFINAL -- no --> R1
    end

    DSAVE --> DONE
    RFINAL -- yes --> DONE
    RSAVE --> DONE

    DONE(["dry_run_complete · final.png saved"])

    classDef llm      fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef cache    fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef agent    fill:#fef9c3,stroke:#ca8a04,color:#713f12
    classDef heal     fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
    classDef terminal fill:#f0fdf4,stroke:#15803d,color:#14532d,font-weight:bold

    class D2,S3 llm
    class S2,DSAVE,RSAVE cache
    class SEL,AUTH,S1,S3,S4,A1 agent
    class RHEAL1,RHEAL2 heal
    class START,DONE terminal
```

---

## Two-Phase Execution

```
process_form(url)
    │
    ├─ navigate() → initial ARIA snapshot → embed field signature
    │
    ├─ [Cache hit]  ──► Replay  ──► dry_run_complete  (0 LLM calls)
    │
    └─ [Cache miss] ──► Discovery ──► save variant ──► dry_run_complete
```

**Discovery** is a ReAct loop: snapshot the page → Gemini reasons over the ARIA tree → execute the returned action batch → repeat until the final submit button appears alone. Every action (successful or failed) is appended to the transcript with an `expected_failure` tag. On completion the full transcript is stored as a *flow variant*.

**Replay** re-executes the transcript step-by-step via Playwright. Steps tagged `expected_failure: true` are skipped cleanly. Unexpected failures trigger self-healing (see below).

---

## Flow Variants and Embedding Matching

A single ATS domain (`job-boards.greenhouse.io`) hosts thousands of listings with varying field sets. A naive one-flow-per-domain cache would ping-pong: listing L1 heals itself to L2's shape, L2 reappears and heals back, forever.

Each discovered flow is stored as its own *variant* row, keyed by an embedding of the page's initial ARIA snapshot. `find_best_flow()` retrieves the variant whose starting page was most semantically similar to the current one via cosine similarity.

The embedding input is `extract_field_signature()` — the snapshot reduced to one `"role: name"` line per form field, stripping headings and buttons that are identical across all listings on a template. This makes the embedding sensitive to *field set differences* while robust to job-title interpolation in field names (`"...as a DevRel Engineer?"` vs `"...as a Backend Engineer?"`).

A cosine similarity threshold (`SAME_VARIANT_THRESHOLD = 0.97`) controls whether a re-discovered flow updates an existing variant in place (minor dev-side change, same listing) or inserts a new row (different listing/template with a different field set).

---

## Selector Sub-Agent

Custom dropdown widgets — react-select, Workday overlays, phone-code pickers — are not trivially automatable. The right sequence of clicks, keypresses, and waits varies by widget implementation. Hardcoded strategies break constantly.

A specialist sub-agent discovers the interaction recipe for each field via its own ReAct loop (up to 10 steps), emitting one primitive op per turn:

```
click_target → [snapshot shows option list] → click_option "+91 IN - India" → done
```

The **recipe** — a literal-valued sequence of `click_target`, `type`, `key`, `clear`, `click_option` ops — is cached per `(domain, role, name)`. Future encounters replay it with zero LLM calls. If the page changes and the recipe fails, `RecipeFailed` is raised with the stale recipe attached, triggering fresh discovery that receives the old description as a hint.

### RAG hints (two tiers)

Each discovery prompt receives ranked hints from the embedding index:

1. **Same-site hints** — semantically similar recipes already stored for other fields on this exact domain, including siblings discovered earlier in the same run (their `save_recipe` commits before the next field is attempted). Scoped to exact domain so a cross-site near-duplicate can't hand a "fresh" discovery its answer.
2. **Cross-site hints** — similar recipes from other ATS domains, framed as inspiration for *what kind of answer* a field expects, not how to operate this specific DOM.

### Portal handling

Some widgets render their option list in a DOM portal near `<body>`, outside the combobox's DOM ancestry. `_related_popup_snapshot()` follows ARIA combobox-pattern relationships (`aria-controls` / `aria-owns` / `aria-activedescendant`) to find the open listbox and append it to the local snapshot — so the sub-agent sees the options without needing a full-page snapshot request.

---

## Self-Healing

```
Replay step fails
    │
    ├─ NeedsSelectorAgent / RecipeFailed
    │       └─► selector_agent.resolve(stale=...)
    │               success ──► patch action in-place, save updated variant, continue
    │               failure ──► full-page heal ▼
    │
    └─ Any other exception
            └─► discover_flow(error_context=...) from current page state
                    ──► splice healed tail onto already-executed prefix
                    ──► save merged variant
                    ──► return healed_needs_restart  (caller restarts with new flow)
```

The `error_context` carries the failed action and its error details into the next discovery so the agent knows exactly what went wrong and can correct its value choice or action type.

---

## Login / Signup Agent

When the main agent encounters an auth gate it returns `[{"action": "needs_login"}]`. A specialist agent takes over:

- **Stored credentials** → logs in directly
- **No credentials** → registers a new account using profile data, persists credentials for future runs
- **OTP / CAPTCHA** → escalates to the user immediately
- **Needs more data** → requests the full `profile.json` on the next iteration via `get_full_profile`

Credentials are stored per `cache_key` (same namespace isolation as flows and recipes), never in `profile.json` which the main agent sees.

---

## Anti-Detection

- Browser launched with `--disable-blink-features=AutomationControlled`; `navigator.webdriver` is undefined via `add_init_script`
- Fills use clipboard paste via the Web Clipboard API; character-by-character typing is the fallback for paste-blocked inputs
- Every click is preceded by natural multi-waypoint mouse movement with random offsets and timing

---

## Module Map

```
src/
├── executor.py          # Orchestration: process_form(), discover_flow()
├── agent.py             # Main ReAct agent — Gemini Flash, ARIA snapshot → action batch
├── selector_agent.py    # Dropdown specialist — recipe discovery, replay, RAG hints
├── browser_client.py    # Playwright wrapper — snapshot, execute_action, network log
├── select_strategies.py # Low-level widget primitives — apply_op, local_snapshot, etc.
├── login_agent.py       # Auth specialist — login, signup, credential persistence
├── db.py                # SQLite — flow variants + select recipes, cosine search
├── embeddings.py        # gemini-embedding-001, cosine_similarity
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

**Models:** `gemini-flash-latest` for all agents · `gemini-embedding-001` for flow variant + recipe embeddings

---

## Key Design Decisions

**Why embeddings for flow variant matching, not exact-string or Jaccard?**
ATS templates interpolate job titles into field names. Exact matching creates a new variant per listing. Jaccard is similarly brittle on long strings with small substitutions. Embeddings are robust to single-token job-title swaps while still being sensitive to genuinely different field sets.

**Why per-field recipe caching, not per-page widget strategies?**
Widget DOM structure varies by vendor version and page. A per-field cache hits exactly when the field is the same even if other fields changed, and misses exactly when it needs to rediscover. Coarser granularity over-invalidates; finer granularity (per-field-per-value) under-reuses.

**Why `domain#test` namespace isolation instead of a separate test database?**
Test runs exercise the real query and storage paths through the same `db.py` functions, confirming that namespace scoping works correctly. A separate test DB would hide bugs in the scoping logic itself.

**Why doesn't `force_refresh` pre-delete existing variants?**
Deletion is destructive and non-idempotent. The save-dedup logic (cosine ≥ 0.97 → update in place; else → new row) handles convergence correctly and is already the right place for that decision.

---

## Setup

```bash
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# Set GEMINI_API_KEY in .env

# Fill in kb/profile.json with your details
# Place your resume at kb/resume.pdf
```

## Usage

```python
from src.executor import process_form

# First run: full LLM discovery
result = await process_form("https://job-boards.greenhouse.io/company/jobs/12345")
# → {"status": "dry_run_complete"}   stops before final submit, saves final.png

# Subsequent runs: zero LLM calls
result = await process_form("https://job-boards.greenhouse.io/company/jobs/12345")
# → {"status": "dry_run_complete"}

# Force re-discovery
result = await process_form(url, force_refresh=True)
```

The agent always dry-runs — it intercepts the final submit, screenshots it, and returns without clicking.

## Test Suite

All tests use an isolated `#test` cache namespace so they never touch the production cache.

| Script | What it exercises |
|--------|-------------------|
| `test_full_discovery.py` | Full LLM discovery, all select-recipe caches cleared |
| `test_select_cached.py` | Force-refresh flow discovery, recipe cache intact — verifies recipe replay |
| `test_cached.py` | Pure cached replay — zero LLM calls end-to-end |
| `test_selector_agent.py` | Selector agent in isolation, no main agent |

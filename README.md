# Form Filler Agent

Autonomous end-to-end job application agent. Drives a real browser via Playwright, reasons over ARIA accessibility trees with Gemini Flash, and fills forms across Greenhouse, Workday, Rippling, iCIMS, and others.

---

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

**Discovery then caching.** The first run is LLM-driven — a ReAct loop snapshots the page, asks Gemini what to do, and executes until the final submit is reached. That transcript is stored as a flow variant. Every subsequent run replays it deterministically with zero LLM calls.

**Specialist agents for complex sub-tasks.** Custom dropdown widgets delegate to a *selector sub-agent* that discovers and caches a literal op-sequence per field (`click_target → click_option → done`). Auth gates hand off to a *login agent* for credentials, account creation, and OTP escalation — keeping both out of the main agent's context.

**RAG-based snapshot matching.** Each flow variant is stored with an embedding of the initial ARIA snapshot. Re-entry retrieves the closest variant by cosine similarity — robust to job-title substitutions in field names, sensitive to genuinely different field sets. The same index drives per-field hint injection inside the selector sub-agent.

**Self-heal based re-discovery.** When replay breaks, field-level repair runs first (one LLM call, surgical). On failure, discovery re-runs from the current page with the failure as context, splices a healed tail onto the already-executed prefix, and saves the merged result.

---

→ [Architecture & design decisions](docs/architecture.md)
→ [Example run — Rippling ATS, full trace](docs/example-run.md)

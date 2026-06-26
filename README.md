# Form Filler Agent

Autonomous end-to-end job application agent. Drives a real browser via Playwright, reasons over ARIA accessibility trees with Gemini Flash, and fills forms across Greenhouse, Workday, Rippling, iCIMS, and others.

---

**Discovery then caching.** The first run against a form is LLM-driven — a ReAct loop snapshots the page, asks the model what to do, and executes until the final submit button is reached. That transcript is stored. Every subsequent run replays it deterministically with zero LLM calls.

**Specialist agents for complex sub-tasks.** The main agent handles page-level reasoning. Custom dropdown widgets are delegated to a *selector sub-agent* that discovers and caches a literal op-sequence per field (`click_target → click_option → done`). Auth gates are handed off to a *login agent* that handles credentials, account creation, and OTP escalation — keeping both concerns out of the main agent's context.

**RAG-based snapshot matching.** Each discovered flow is stored with an embedding of the page's initial ARIA snapshot. On re-entry, the closest-matching variant is retrieved by cosine similarity — robust to job-title substitutions in field names, sensitive to genuinely different field sets. The same index powers per-field hint injection for the selector sub-agent.

**Self-heal based re-discovery.** When replay breaks, the system tries field-level repair first (one LLM call, surgical). If that fails, it re-runs discovery from the current page with the failure as context, splices the healed tail onto the already-executed prefix, and saves the merged result.

---

![Agent filling a Visa/Workday job application form](image.png)

---

→ [Architecture & design decisions](docs/architecture.md)  
→ [Example run — Rippling ATS (full trace)](docs/example-run.md)

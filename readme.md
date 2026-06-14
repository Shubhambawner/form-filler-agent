form-filler-agent/
├── data/
│   └── flows.db          # SQLite storage (auto-generated on first run)
├── kb/
│   └── profile.json      # Pre-stored profile data for the agent
├── src/
│   ├── __init__.py       # Empty file to make src a module
│   ├── db.py             # SQLite connection and caching logic
│   ├── utils.py          # Regex helpers and dry-run interception
│   ├── mcp_client.py     # Python MCP client to talk to Playwright
│   ├── agent.py          # Gemini ReAct logic & self-healing
│   └── executor.py       # Deterministic flow runner
├── test.py               # Main entry point
└── requirements.txt      # Python dependencies
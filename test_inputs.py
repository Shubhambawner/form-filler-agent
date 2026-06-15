"""Shared inputs for the form-filler test suite.

Standard pipeline (run in this order against a fresh checkout):
  test_full_discovery.py -> test_select_cached.py -> test_cached.py
All three operate on the same isolated TEST_DOMAIN namespace, so they never
read, clear, or overwrite the real REAL_DOMAIN flow/select-recipe caches,
while still benefiting from each other's cached state and from same-site RAG
hints (see db.find_similar_recipes's `domain` scoping).

test_selector_agent.py exercises the selector agent directly (no main agent)
-- only needed when iterating on selector_agent.py / select_strategies.py in
isolation.
"""

TARGET_URL = "https://job-boards.greenhouse.io/hackerrank/jobs/7907492"
REAL_DOMAIN = TARGET_URL.split("//")[-1].split("/")[0]

TEST_DOMAIN = REAL_DOMAIN + "#test"

# The combobox fields on this form, with the values the agent fills them
# with. Used by test_full_discovery.py (to force-clear their select_recipes
# before a full re-discovery) and test_selector_agent.py (to exercise
# selector_agent.resolve() directly for each one).
SELECT_FIELDS = [
    # (role, name, value, nth)
    ("combobox", "Country", "India", None),
    ("combobox", "How many years of experience do you have as a DevRel Engineer?", "2", None),
    ("combobox", "Would you be open to relocating to Bangalore?", "Yes", None),
    ("combobox", "How long is your notice period?", "2 months", None),
]

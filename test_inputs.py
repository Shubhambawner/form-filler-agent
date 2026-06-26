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

# TARGET_URL = "https://careers-kinaxis.icims.com/jobs/34911/forward-deployed-engineer%2c-transformation/job" # failsbe cause of hcapcha

TARGET_URL = "https://visa.wd5.myworkdayjobs.com/Visa/job/IN---Bengaluru-India/Software-Engineer--AI-Engineer--Python--1-2-years--experience-only-_REF082785W/apply"
# TARGET_URL = "https://ats.rippling.com/rippling/jobs/69b9d52d-c313-4ee1-83be-697d9b4a1a5a/apply"
# TARGET_URL = "https://job-boards.greenhouse.io/hackerrank/jobs/7907492"
REAL_DOMAIN = TARGET_URL.split("//")[-1].split("/")[0]

TEST_DOMAIN = REAL_DOMAIN + "#test"

# The combobox fields on this form, with the values the agent fills them
# with. Used by test_full_discovery.py (to force-clear their select_recipes
# before a full re-discovery) and test_selector_agent.py (to exercise
# selector_agent.resolve() directly for each one).
# TODO: populate with the actual combobox fields from the Kinaxis iCIMS form
# once a full discovery run completes and the form layout is known.
SELECT_FIELDS = []

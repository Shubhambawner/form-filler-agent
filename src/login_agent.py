"""Login / signup specialist agent.

Called by executor.py when the main agent signals `needs_login`.

The agent receives:
  - Stored credentials from credentials.json (if any) → log-in path
  - A signup-relevant subset of profile.json (name, email, phone) → signup path
  - A `get_full_profile` action it can invoke to receive the full profile on
    the next iteration when the signup form asks for more data

Terminal actions the model returns:
  {"action": "login_complete"}
      -- gate is cleared; the application/job-listing page is now visible.
  {"action": "signup_complete", "fields": {<name>: {"value": ..., "description": ...}, ...}}
      -- registration succeeded; `fields` contains EVERY field that was filled
         during signup (email, password, name, etc.) -- these are persisted to
         credentials.json for all future logins to this domain.
  {"action": "otp_required"}
      -- an OTP / email-verification / CAPTCHA challenge is blocking progress;
         caller surfaces this to the user.
  {"action": "get_full_profile"}
      -- the agent needs more data than the signup subset; the run loop injects
         the full profile JSON into the NEXT iteration's prompt.

Returns one of:
  {"status": "success"}
  {"status": "otp_required"}
  {"status": "failed", "reason": "..."}
"""

import json
from google.genai import types
from .agent import client, MODEL_NAME, PROFILE_DATA
from .select_strategies import NeedsSelectorAgent, RecipeFailed
from . import credentials as creds_store
from . import selector_agent

MAX_STEPS = 15

# Fields from profile.json that are relevant to account-creation forms.
# The agent gets only these by default; it can request more via get_full_profile.
_SIGNUP_KEYS = frozenset({
    "first_name", "last_name", "full_name",
    "email", "phone", "phone_country_code", "phone_number_without_code",
})
_SIGNUP_PROFILE = {
    k: v for k, v in PROFILE_DATA["personal_details"].items()
    if k in _SIGNUP_KEYS
}

# Password to use when creating a new account.  NOT stored in profile.json
# (which the main agent also sees) -- lives here so the main agent is never
# exposed to it.
_DEFAULT_PASSWORD = "Shubham@2024!"

_TERMINAL_ACTIONS = frozenset({"login_complete", "signup_complete", "otp_required", "get_full_profile"})


def _build_prompt(url: str, snapshot: str, creds: dict | None,
                  previous_actions: list, full_profile_data: str | None) -> str:
    if creds:
        auth_section = (
            "STORED CREDENTIALS FOR THIS DOMAIN:\n"
            + json.dumps(creds, indent=2)
            + "\nUse these to log in.  Do NOT create a new account."
        )
    else:
        auth_section = (
            "NO STORED CREDENTIALS -- perform a fresh account registration.\n\n"
            "PROFILE DATA FOR SIGNUP (name / email / phone):\n"
            + json.dumps(_SIGNUP_PROFILE, indent=2)
            + f"\n\nDefault password to use: {_DEFAULT_PASSWORD}\n\n"
            "If the site asks you to confirm the password, use the same value.\n"
            "If 'Continue as Guest' / 'Apply without an account' is available, "
            "prefer that path and return login_complete immediately.\n"
            "If you need more profile data (address, LinkedIn URL, etc.), "
            "return [{\"action\": \"get_full_profile\"}] and it will be injected "
            "into the next iteration."
        )

    prev_section = ""
    if previous_actions:
        prev_section = (
            "\n### ACTIONS ALREADY EXECUTED ###\n"
            + json.dumps(previous_actions, indent=2)
            + "\n"
        )

    profile_section = ""
    if full_profile_data:
        profile_section = (
            "\n### FULL PROFILE DATA (as requested) ###\n"
            + full_profile_data
            + "\n"
        )

    return f"""You are an authentication specialist agent for job application portals.
The browser is currently on a login, sign-up, or account-gate page.
Your job is to get past this gate as efficiently as possible.

{auth_section}

### TERMINAL ACTIONS ###
Return one of these as the SOLE element of your JSON array when the condition is met:

  {{"action": "login_complete"}}
      The gate is cleared; the application or job-listing page is visible.

  {{"action": "signup_complete", "fields": {{
      "<field_name>": {{"value": "<val>", "description": "<what this field represents>"}},
      ...
  }}}}
      Registration is complete.  Include EVERY field you filled during signup
      (email, password, full name, etc.) so they can be stored for future logins.

  {{"action": "otp_required"}}
      An OTP, SMS code, email-verification link, or CAPTCHA is blocking progress.
      Do NOT attempt to guess or fill it -- return this immediately.

  {{"action": "get_full_profile"}}
      You need more profile data than the signup subset above.  The full profile
      will be injected into your next iteration's prompt.

### RULES ###
1. ONLY reference elements that literally appear in the CURRENT SNAPSHOT.
2. After submitting a form, read the NEXT snapshot -- if the application /
   job-listing page is now visible, return login_complete.
3. If a verification code input appears at any point, return otp_required.
4. Do not repeat an action already in ACTIONS ALREADY EXECUTED unless the
   snapshot shows the field is still empty.

### ACTION SCHEMA (same as main agent) ###
Return a JSON array of action objects:
  {{"action": "fill"|"click"|"check"|"uncheck", "role": "<ARIA role>",
    "name": "<exact accessible name>", "value": "<text>"}}
OR exactly one terminal action as the sole array element.

### CURRENT PAGE SNAPSHOT for {url} ###
{snapshot}
{prev_section}{profile_section}
### REQUIRED OUTPUT ###
Return ONLY a valid JSON array.  No markdown, no explanations.
"""


async def run(browser, url: str, domain: str, logger, iteration_prefix: str = "login") -> dict:
    """Drive login or signup for `domain`.

    The browser should already be on or near the auth gate page.
    Returns {"status": "success"|"otp_required"|"failed", ...}.
    """
    creds = creds_store.load(domain)
    previous_actions = []
    full_profile_data = None  # set when the model requests get_full_profile

    for step in range(1, MAX_STEPS + 1):
        # If the previous batch requested the full profile, load it now so it
        # appears in this iteration's prompt, then clear it (one-shot).
        if previous_actions and previous_actions[-1].get("action") == "get_full_profile":
            full_profile_data = json.dumps(PROFILE_DATA, indent=2)

        snapshot = await browser.snapshot()
        prompt = _build_prompt(url, snapshot, creds, previous_actions, full_profile_data)
        full_profile_data = None  # consumed for this turn

        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )

        if logger:
            usage = response.usage_metadata
            logger.log_llm_call(
                f"{iteration_prefix}_step_{step:02d}",
                prompt, response.text,
                {
                    "model": MODEL_NAME,
                    "prompt_token_count": getattr(usage, "prompt_token_count", None),
                    "candidates_token_count": getattr(usage, "candidates_token_count", None),
                    "cached_content_token_count": getattr(usage, "cached_content_token_count", None),
                    "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
                    "tool_use_prompt_token_count": getattr(usage, "tool_use_prompt_token_count", None),
                    "total_token_count": getattr(usage, "total_token_count", None),
                },
            )

        try:
            batch = json.loads(response.text)
        except json.JSONDecodeError:
            print("[LoginAgent] Invalid JSON response; aborting.")
            break

        if not batch:
            break

        # Handle single-element terminal / control actions first.
        if len(batch) == 1:
            act = batch[0].get("action")
            if act == "login_complete":
                print("[LoginAgent] Auth gate cleared (login).")
                return {"status": "success"}
            if act == "signup_complete":
                fields = batch[0].get("fields", {})
                creds_store.save(domain, fields)
                print(f"[LoginAgent] Signup complete; stored {len(fields)} field(s) for {domain}.")
                return {"status": "success"}
            if act == "otp_required":
                print("[LoginAgent] OTP/verification required; cannot proceed.")
                return {"status": "otp_required"}
            if act == "get_full_profile":
                print("[LoginAgent] Full profile requested; will inject on next iteration.")
                previous_actions.append({"action": "get_full_profile"})
                continue  # no browser action -- just set up for the next turn

        # Execute browser actions; skip terminal/control ops that appear in a
        # mixed batch (shouldn't happen, but be defensive).
        select_idx = 0
        for action in batch:
            if action.get("action") in _TERMINAL_ACTIONS:
                if action.get("action") == "get_full_profile":
                    previous_actions.append({"action": "get_full_profile"})
                continue
            print(f"[LoginAgent] Executing: {action}")
            try:
                await browser.execute_action(action)
                previous_actions.append({**action, "expected_failure": False})
            except (NeedsSelectorAgent, RecipeFailed) as e:
                stale = None
                if isinstance(e, RecipeFailed):
                    stale = {"recipe": e.stale_recipe, "description": action.get("description"), "reason": str(e)}
                select_idx += 1
                result = await selector_agent.resolve(
                    browser, action, domain, logger=logger,
                    iteration=f"{iteration_prefix}_step_{step:02d}-{select_idx}", stale=stale,
                )
                if result["success"]:
                    previous_actions.append({
                        **action,
                        "recipe": result["recipe"],
                        "chosen_label": result["chosen_label"],
                        "description": result["description"],
                        "expected_failure": False,
                    })
                else:
                    print(f"[LoginAgent] Selector agent failed: {result['error']}")
                    previous_actions.append({**action, "expected_failure": True, "errorDetails": result["error"]})
            except Exception as e:
                print(f"[LoginAgent] Action failed: {e}")
                previous_actions.append({**action, "expected_failure": True, "errorDetails": str(e)})

    print(f"[LoginAgent] Could not complete auth in {MAX_STEPS} steps.")
    return {"status": "failed", "reason": f"not completed in {MAX_STEPS} steps"}

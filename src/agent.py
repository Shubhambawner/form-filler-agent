import json
import os
from google import genai
from google.genai import types

# Load Profile Data
KB_PATH = os.path.join(os.path.dirname(__file__), '..', 'kb', 'profile.json')
with open(KB_PATH, 'r') as f:
    PROFILE_DATA = json.load(f)

# Ensure your API key is set in your environment variables:
# export GEMINI_API_KEY="your_key_here"
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

MODEL_NAME = 'gemini-flash-latest'

def build_system_prompt(target_url: str, snapshot: str, previous_actions: list = None,
                        error_context: dict = None, domain_creds: dict = None,
                        network_activity: list = None) -> str:
    if domain_creds:
        creds_lines = "\n".join(f"      {k}: {v['value']}" for k, v in domain_creds.items())
        _login_wall = (
            "Stored credentials for this site:\n" + creds_lines + "\n"
            "    If the current page is a login screen, fill these credentials and submit"
            " like any other form. After submitting, continue filling the application form."
        )
    else:
        _login_wall = (
            'If the current page is a login gate or account-creation screen that must be passed\n'
            '    before the application form, return EXACTLY:\n'
            '      [{"action": "needs_login"}]\n'
            '    as your only action. A specialist agent handles credential creation.'
        )
    prompt = f"""
    You are an expert form-filling agent. You operate on ONE page of a web form at a time,
    using a Playwright-driven browser. You will be shown an accessibility-tree snapshot of the
    CURRENT state of the page, and must output the NEXT batch of actions to perform on it.

    ### ACTION SCHEMA ###
    Return a JSON array of action objects, each shaped like:
      {{"action": "fill" | "select" | "combobox_select" | "click" | "check" | "uncheck" | "upload", "role": "<ARIA role from the snapshot, e.g. textbox, button, combobox, checkbox>", "name": "<exact accessible name from the snapshot>", "value": "<text to fill / option to select / absolute file path to upload>", "nth": <optional 0-based index>, "is_final": <true only on the very last submit action>}}
    "value" is omitted for "click", "check", and "uncheck" actions.
    "nth" is optional and only needed when MULTIPLE elements in the snapshot share the exact same
    role and accessible name (e.g. two "Drop or select..." upload buttons, one for resume and one
    for cover letter). Count occurrences of that (role, name) pair in top-to-bottom document order
    starting at 0, and set "nth" to the index of the one you mean.
    "is_final" must be set to true ONLY on the single click action that submits the ENTIRE
    application (e.g. "Submit Application", "Apply", "Finish"). Never set it on navigation
    buttons ("Next", "Continue"), cookie banners ("Accept Cookies"), intermediate steps
    ("Apply Manually", "Sign In", "Create Account"), or any action that is not the true
    final submission. Omit "is_final" (or set it to false) on every other action.

    To pause while the page loads or processes something, return a wait action:
      {{"action": "wait", "seconds": <1-5>}}
    Maximum 5 seconds per wait. Use this instead of returning an empty array when
    the page is transitioning or a button is still disabled/loading.

    ### ACTION TYPE GUIDE ###
    - "fill": type text into a "textbox" or "textarea".
    - "wait": pause for 1-5 seconds while the page loads or a button becomes enabled.
    - "screenshot": capture a visual screenshot — the image will be shown to you on the very
      next iteration alongside the ARIA snapshot. Use when the accessibility tree alone is
      not enough to understand what is on screen (e.g. visual-only content, ambiguous state).
      Return as the sole action: [{{"action": "screenshot"}}]
    - "reload": hard-reload the current page. Use when the page appears stuck, shows stale
      content, or needs a clean restart. Return as the sole action: [{{"action": "reload"}}]
    - "select": choose an option in a NATIVE HTML <select> dropdown.
    - "combobox_select": choose an option in a CUSTOM/searchable dropdown widget (role "combobox"
      that is not a native <select>, e.g. a react-select style picker). A specialist sub-agent
      figures out exactly how to operate the widget and which option to pick.
    - "click": click a button, link, radio button, or toggle.
    - "check" / "uncheck": set a checkbox's state.
    - "upload": attach a file via a file-picker button.
    For any "combobox"-role element, prefer "combobox_select". If you are not sure whether a
    dropdown is native or custom, guess "select" first -- if the ERROR RECOVERY section later shows
    it failed, retry with "combobox_select" (or vice versa).

    ### RULES ###
    1. ONLY reference elements that literally appear in the CURRENT SNAPSHOT below, using their exact role and accessible name.
    2. NEVER invent roles or names that are not present in the snapshot.
    3. Use the Profile Data below to fill in field values. Skip a field only if the snapshot shows it
       already holds the value implied by the Profile Data. A field showing a default or placeholder
       value that does NOT match the Profile Data (e.g. a country-code selector defaulting to a
       different country than the candidate's location, or a dropdown still showing "Select...")
       is NOT "already correctly filled" -- it must still be corrected.
    4. Do not repeat any action already listed under "Actions already executed" UNLESS the CURRENT
       SNAPSHOT shows that field is STILL empty or showing a placeholder/default value -- in that
       case your previous attempt did not stick, and you MUST retry it using a DIFFERENT action type
       than the one that didn't stick (e.g. "combobox_select" instead of "select" or "fill").
    5. Scan the ENTIRE CURRENT SNAPSHOT for every field that is empty, blank, or shows a
       placeholder/default value, and include an action for EACH one you find. Do this on every
       turn -- never assume a field is done just because you acted on it in a previous turn; verify
       against the CURRENT SNAPSHOT.
    6. After accounting for the actions above: if every field on this page is correctly filled and
       there is a "Next" / "Continue" / "Save and continue" style button, include a click on it as
       the LAST action in the array.
    7. ONLY when every field on this page is correctly filled AND the single remaining step is the
       FINAL submission button for the entire application (e.g. "Submit Application", "Submit",
       "Apply", "Finish"), return a JSON array containing EXACTLY ONE action: the click on that
       button with "is_final": true, and nothing else.
    8. NEVER include the final submit action in the same batch as any form-filling or other
       actions. Always return all fills/clicks first, then on the very next turn return the
       submit action alone. Even if only 1-2 fields remain alongside the submit button, fill
       them first and let the submit be intercepted on the following turn.
    9. Prefer filling fields manually over using resume/profile autofill buttons ("Autofill
       with Resume", "Import from LinkedIn", etc.) — autofill triggers background processing
       that requires unpredictable waits and may not populate all fields correctly.
    10. If the page is still loading or a button is temporarily disabled, return a wait action
       (1-5 seconds) rather than an empty array. Only return an empty array [] when there is
       genuinely nothing left to do and no button to advance.

    ### LOGIN / SIGNUP WALL ###
    {_login_wall}

    ### PROFILE DATA ###
    {json.dumps(PROFILE_DATA, indent=2)}

    ### CURRENT PAGE SNAPSHOT (ARIA tree) for {target_url} ###
    {snapshot}

    ### REQUIRED OUTPUT FORMAT ###
    Return ONLY a valid JSON array of action objects. No markdown, no explanations.
    Format Example:
    [
      {{"action": "fill", "role": "textbox", "name": "First Name", "value": "Alex"}},
      {{"action": "combobox_select", "role": "combobox", "name": "Country", "value": "India"}},
      {{"action": "check", "role": "checkbox", "name": "I acknowledge"}},
      {{"action": "click", "role": "button", "name": "Next"}}
    ]
    """

    if previous_actions:
        prompt += f"""
        \n### ACTIONS ALREADY EXECUTED ###
        {json.dumps(previous_actions, indent=2)}
        """

    if network_activity:
        lines = "\n".join(f"  {r['method']} {r['path']} → {r['status']}" for r in network_activity)
        prompt += f"""
        \n### NETWORK ACTIVITY FROM PREVIOUS ACTIONS ###
        The following same-domain backend requests were triggered by the last batch of actions.
        Use this to infer page-state changes (e.g. login completed, autofill data fetched,
        form section saved) that may not yet be reflected in the snapshot:
        {lines}
        """

    # Self-Healing Context Injection
    if error_context:
        prompt += f"""
        \n### ERROR RECOVERY ###
        The following actions failed when executed against the page (for "select"/
        "combobox_select" actions, a specialist already exhausted its ideas for operating the
        widget -- the error is the specialist's own explanation, often listing the real
        available options. For other action types, "still shows ... after ..." means your
        chosen value did not stick):
        {json.dumps(error_context['failedActions'], indent=2)}

        Re-examine the CURRENT SNAPSHOT above (it reflects the page's current state). For EVERY
        failed action listed above, you MUST include a corrected action in your response --
        UNLESS the corresponding field is genuinely no longer present, already correctly filled,
        or no longer relevant in the CURRENT SNAPSHOT. For "select"/"combobox_select" failures,
        prefer trying a different VALUE closer to one of the field's real options (the error
        often lists them); for other action types, try a different "action" type or target
        element. Do not silently drop any failed action, and do not repeat any failed action
        verbatim if the element it referenced no longer matches the snapshot.
        """
    return prompt

async def run_react_agent(url: str, snapshot: str, previous_actions: list = None,
                          error_context: dict = None, logger=None, iteration: int = None,
                          domain_creds: dict = None, network_activity: list = None,
                          screenshot_data: bytes = None) -> list:
    """Calls Gemini to generate the next batch of actions for the current page state."""
    print(f"[Agent] Thinking... analyzing current page state for {url}")

    prompt = build_system_prompt(url, snapshot, previous_actions, error_context, domain_creds, network_activity)

    if screenshot_data:
        contents = [prompt, types.Part.from_bytes(data=screenshot_data, mime_type="image/png")]
    else:
        contents = prompt

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )

    if logger is not None:
        usage = response.usage_metadata
        logger.log_llm_call(iteration, prompt, response.text, {
            "model": MODEL_NAME,
            "prompt_token_count": getattr(usage, "prompt_token_count", None),
            "candidates_token_count": getattr(usage, "candidates_token_count", None),
            "cached_content_token_count": getattr(usage, "cached_content_token_count", None),
            "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
            "tool_use_prompt_token_count": getattr(usage, "tool_use_prompt_token_count", None),
            "total_token_count": getattr(usage, "total_token_count", None),
        })

    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        print("[Agent] Critical Error: Gemini did not return valid JSON.")
        return []
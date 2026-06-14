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

def build_system_prompt(target_url: str, snapshot: str, previous_actions: list = None, error_context: dict = None) -> str:
    prompt = f"""
    You are an expert form-filling agent. You operate on ONE page of a web form at a time,
    using a Playwright-driven browser. You will be shown an accessibility-tree snapshot of the
    CURRENT state of the page, and must output the NEXT batch of actions to perform on it.

    ### ACTION SCHEMA ###
    Return a JSON array of action objects, each shaped like:
      {{"action": "fill" | "click" | "select" | "check" | "uncheck" | "upload", "role": "<ARIA role from the snapshot, e.g. textbox, button, combobox, checkbox>", "name": "<exact accessible name from the snapshot>", "value": "<text to fill / option to select / absolute file path to upload>", "nth": <optional 0-based index>}}
    "value" is omitted for "click", "check", and "uncheck" actions.
    "nth" is optional and only needed when MULTIPLE elements in the snapshot share the exact same
    role and accessible name (e.g. two "Drop or select..." upload buttons, one for resume and one
    for cover letter). Count occurrences of that (role, name) pair in top-to-bottom document order
    starting at 0, and set "nth" to the index of the one you mean.

    ### RULES ###
    1. ONLY reference elements that literally appear in the CURRENT SNAPSHOT below, using their exact role and accessible name.
    2. NEVER invent roles or names that are not present in the snapshot.
    3. Use the Profile Data below to fill in field values. Skip a field only if the snapshot shows it
       already holds the value implied by the Profile Data. A field showing a default or placeholder
       value that does NOT match the Profile Data (e.g. a country-code selector defaulting to a
       different country than the candidate's location) is NOT "already correctly filled" -- it must
       still be corrected.
    4. Do not repeat any action already listed under "Actions already executed".
    5. If every visible field is filled and there is a "Next" / "Continue" / "Save and continue" style button, include a click on it as the LAST action in the array.
    6. If you see the FINAL submission button for the entire application (e.g. "Submit Application", "Submit", "Apply", "Finish"), include a click on it as the LAST action and nothing after it.
    7. If there is nothing left to do on this page and no button to advance, return an empty JSON array [].

    ### PROFILE DATA ###
    {json.dumps(PROFILE_DATA, indent=2)}

    ### CURRENT PAGE SNAPSHOT (ARIA tree) for {target_url} ###
    {snapshot}

    ### REQUIRED OUTPUT FORMAT ###
    Return ONLY a valid JSON array of action objects. No markdown, no explanations.
    Format Example:
    [
      {{"action": "fill", "role": "textbox", "name": "First Name", "value": "Alex"}},
      {{"action": "select", "role": "combobox", "name": "Country", "value": "India"}},
      {{"action": "check", "role": "checkbox", "name": "I acknowledge"}},
      {{"action": "click", "role": "button", "name": "Next"}}
    ]
    """

    if previous_actions:
        prompt += f"""
        \n### ACTIONS ALREADY EXECUTED ###
        {json.dumps(previous_actions, indent=2)}
        """

    # Self-Healing Context Injection
    if error_context:
        prompt += f"""
        \n### ERROR RECOVERY ###
        The following actions failed when executed against the page:
        {json.dumps(error_context['failedActions'], indent=2)}

        Re-examine the CURRENT SNAPSHOT above (it reflects the page's current state). For EVERY
        failed action listed above, you MUST include a corrected action in your response that
        achieves the same goal via a different approach (e.g. a different "action" type, such as
        "click" or "fill" instead of "select", or a different target element) -- UNLESS the
        corresponding field is genuinely no longer present, already correctly filled, or no
        longer relevant in the CURRENT SNAPSHOT. Do not silently drop any failed action, and do
        not repeat any failed action verbatim if the element it referenced no longer matches the
        snapshot.
        """
    return prompt

async def run_react_agent(url: str, snapshot: str, previous_actions: list = None, error_context: dict = None, logger=None, iteration: int = None) -> list:
    """Calls Gemini to generate the next batch of actions for the current page state."""
    print(f"[Agent] Thinking... analyzing current page state for {url}")

    prompt = build_system_prompt(url, snapshot, previous_actions, error_context)
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
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
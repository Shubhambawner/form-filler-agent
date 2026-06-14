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

def build_system_prompt(target_url: str, error_context: dict = None) -> str:
    prompt = f"""
    You are an expert Playwright automation agent. Your goal is to generate a sequence of Playwright MCP tool 
    calls to successfully fill out the form at {target_url}.
    
    ### CRITICAL RULES ###
    1. NEVER use 'wait' or 'sleep' actions. Playwright handles visibility and actionability automatically.
    2. Use highly specific locators (e.g., getByLabel('First Name'), getByRole('button', {{ name: 'Submit' }})).
    3. Output the exact values from the Profile Data below directly into the tool parameters.
    
    ### PROFILE DATA ###
    {json.dumps(PROFILE_DATA, indent=2)}
    
    ### REQUIRED OUTPUT FORMAT ###
    Return ONLY a valid JSON array of tool execution objects. No markdown, no explanations.
    Format Example:
    [
      {{"tool": "playwright_navigate", "parameters": {{"url": "{target_url}"}}}},
      {{"tool": "playwright_fill", "parameters": {{"selector": "getByLabel('First Name')", "value": "Alex"}}}},
      {{"tool": "playwright_click", "parameters": {{"selector": "getByRole('button', {{ name: 'Next' }})"}}}}
    ]
    """

    # Self-Healing Context Injection
    if error_context:
        prompt += f"""
        \n### CRITICAL ERROR RECOVERY ###
        You previously attempted this flow and it failed. 
        Failed at Step Index: {error_context['failedStepIndex']}
        Error Detail: {error_context['errorDetails']}
        Previous Flow Attempt: {json.dumps(error_context['previousFlow'])}
        
        Analyze the failure. The selector might be wrong, or a step was missed. 
        Generate a NEW, corrected JSON array from start to finish.
        """
    return prompt

async def run_react_agent(url: str, error_context: dict = None) -> list:
    """Calls Gemini to generate or heal the automation flow."""
    print(f"[Agent] Thinking... Generating flow for {url}")

    prompt = build_system_prompt(url, error_context)
    response = client.models.generate_content(
        model='gemini-flash-latest',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )

    try:
        flow_sequence = json.loads(response.text)
        return flow_sequence
    except json.JSONDecodeError:
        print("[Agent] Critical Error: Gemini did not return valid JSON.")
        return []
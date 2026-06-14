import re

def is_final_submit(tool_name: str, parameters: dict) -> bool:
    """
    Evaluates if an MCP tool call is likely the final form submission.
    """
    # Playwright MCP uses 'playwright_click' or 'playwright_fill'
    if tool_name not in ["playwright_click", "browser_click", "click"]:
        return False
    
    # Extract the target text or selector from the parameters
    # The exact key depends on the MCP schema, typically 'selector' or 'target'
    target_text = str(parameters).lower()
    
    # Ignore pagination or multi-step form navigation
    if re.search(r'next|continue|back', target_text):
        return False
        
    # Identify final submission intent
    return bool(re.search(r'submit|done|ok|confirm|finish|place order|register', target_text))

def mask_sensitive_data(mcp_sequence: list) -> list:
    """
    Optional utility to scrub actual profile values from logs if needed.
    (Can be implemented later for extensive logging requirements).
    """
    pass
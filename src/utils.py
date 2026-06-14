import re

def is_final_submit(action: dict) -> bool:
    """
    Evaluates if an action is likely the final form submission button click.
    """
    if action.get('action') != 'click' or action.get('role') != 'button':
        return False

    name = (action.get('name') or '').lower()

    # Ignore pagination or multi-step form navigation
    if re.search(r'next|continue|back', name):
        return False

    # Identify final submission intent
    return bool(re.search(r'submit|done|ok|confirm|finish|place order|register|apply', name))

def flow_has_final_submit(flow_sequence: list) -> bool:
    """Checks whether a flow sequence contains a final form submission step."""
    return any(is_final_submit(action) for action in flow_sequence)

def mask_sensitive_data(mcp_sequence: list) -> list:
    """
    Optional utility to scrub actual profile values from logs if needed.
    (Can be implemented later for extensive logging requirements).
    """
    pass
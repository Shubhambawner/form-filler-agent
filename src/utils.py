import re


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _values_match(actual: str, expected: str) -> bool:
    """Loose comparison used to verify an action actually took effect.
    Tolerates UI-applied formatting (e.g. phone numbers, currency) by also
    comparing with non-alphanumeric characters stripped."""
    a, e = _normalize(actual), _normalize(expected)
    if not a or not e:
        return False
    if e in a or a in e:
        return True
    a2, e2 = re.sub(r"[^a-z0-9]", "", a), re.sub(r"[^a-z0-9]", "", e)
    return bool(e2) and (e2 in a2 or a2 in e2)


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
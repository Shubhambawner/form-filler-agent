import re

from .utils import _normalize, _values_match


class NoMatchingOption(ValueError):
    """Raised when a widget has an enumerable, non-empty option list but none
    of them match the desired value. This is a VALUE problem, not a widget-
    mechanics problem -- no other strategy preset can conjure a non-existent
    option, so the selector agent should report this back to the main agent
    (which can pick a value closer to one of the real options) instead of
    burning attempts on other strategies."""


class NeedsSelectorAgent(Exception):
    """Raised by BrowserClient.execute_action for "select"/"combobox_select"
    actions that need the selector-agent specialist: a combobox_select with
    no recipe attached, or a "select" whose native_select handling failed."""

    def __init__(self, action):
        super().__init__(f"needs selector agent for {action.get('role')} '{action.get('name')}'")
        self.action = action


class RecipeFailed(Exception):
    """Raised by selector_agent.run_recipe when a recipe's ops or final
    verification fail. Carries the stale recipe/description so
    selector_agent.resolve can re-discover with full context."""

    def __init__(self, reason, stale_recipe=None, stale_description=None):
        super().__init__(reason)
        self.reason = reason
        self.stale_recipe = stale_recipe
        self.stale_description = stale_description


async def _option_texts(options) -> list:
    try:
        await options.first.wait_for(state="visible", timeout=2000)
        count = await options.count()
    except Exception:
        return []

    texts = []
    for i in range(count):
        try:
            texts.append(await options.nth(i).inner_text())
        except Exception:
            texts.append("")
    return texts


def _match_option(texts: list, value: str):
    norm_value = _normalize(value)
    for i, t in enumerate(texts):
        if _normalize(t) == norm_value:
            return i
    for i, t in enumerate(texts):
        nt = _normalize(t)
        if nt and (nt.startswith(norm_value) or norm_value.startswith(nt)):
            return i
    return None


async def _combobox_context(page, name: str, nth: int = None, window: int = 2) -> str:
    """Returns a few lines of the full-page snapshot around this combobox.
    Some custom widgets (e.g. react-select) reflect the selected value
    inline on the `combobox "<name>"` line itself, while others render it
    as a sibling node (e.g. a phone-country-code picker shows the dialing
    code as a separate `text: "+91"` line next to `combobox "Country"`).
    A small window catches both cases."""
    snap = await page.locator("body").aria_snapshot()
    lines = snap.splitlines()
    indices = [i for i, line in enumerate(lines) if "combobox" in line and f'"{name}"' in line]
    if not indices:
        return ""
    index = indices[nth] if nth is not None and nth < len(indices) else indices[-1]
    start, end = max(0, index - window), min(len(lines), index + window + 1)
    return "\n".join(lines[start:end])


def _label_fragment_present(context: str, label: str) -> bool:
    """Looser fallback check: does any sufficiently-distinctive token from
    the selected option's label (e.g. the "91" in "India +91") show up
    near the combobox? Used when the full label isn't rendered verbatim."""
    ctx = re.sub(r"[^a-z0-9]", "", _normalize(context))
    for token in re.findall(r"[A-Za-z0-9]+", label):
        if len(token) >= 2 and token.lower() in ctx:
            return True
    return False


async def compute_signature(locator) -> str:
    """Cheap structural fingerprint of a dropdown-like element, used as a
    cross-domain cache key (e.g. native <select> vs. an <input role=combobox
    aria-haspopup=listbox> vs. a <div role=combobox>)."""
    try:
        return await locator.evaluate(
            "el => [el.tagName.toLowerCase(), el.getAttribute('role') || '', "
            "el.getAttribute('aria-haspopup') || '', el.getAttribute('aria-autocomplete') || ''].join('|')"
        )
    except Exception:
        return "unknown"


async def _related_popup_snapshot(locator) -> str:
    """Finds an open listbox/menu "owned" by `locator`, even when it's
    rendered in a portal outside `locator`'s DOM ancestry (e.g. react-select
    appends its option list near <body>, not inside the combobox).

    Prefers the standard ARIA combobox-pattern relationship attributes
    (aria-controls / aria-owns / aria-activedescendant), which a widget sets
    to point at its popup by element id regardless of where that popup is
    rendered in the DOM -- this is the general, spec-based way two elements
    declare a relationship that DOM ancestry doesn't capture. Falls back to
    any visible listbox/menu anywhere on the page if no such attribute is
    set."""
    page = locator.page
    try:
        related_id = await locator.evaluate("""el => {
            const controls = el.getAttribute('aria-controls');
            if (controls) return controls;
            const owns = el.getAttribute('aria-owns');
            if (owns) return owns;
            const active = el.getAttribute('aria-activedescendant');
            if (active) {
                const activeEl = document.getElementById(active);
                const container = activeEl && activeEl.closest('[role="listbox"],[role="menu"],[role="grid"],[role="tree"]');
                if (container && container.id) return container.id;
            }
            return '';
        }""")
    except Exception:
        related_id = ""

    for ref_id in related_id.split():
        try:
            target = page.locator(f'[id="{ref_id}"]')
            if await target.count() > 0 and await target.first.is_visible():
                return await target.first.aria_snapshot()
        except Exception:
            continue

    try:
        popup = page.locator('[role="listbox"]:visible, [role="menu"]:visible').first
        if await popup.count() > 0:
            return await popup.aria_snapshot()
    except Exception:
        pass

    return ""


async def local_snapshot(locator) -> str:
    """Returns an accessibility-tree snapshot scoped to the area around
    `locator`, by walking up 2, then 3, then 4 ancestor levels and returning
    the richest one (most lines) that includes more than just the element
    itself (sibling labels, error text, the rest of a custom dropdown's
    markup). Falls back to the element's own snapshot if nothing richer is
    found.

    Also appends any open popup linked to `locator` via `_related_popup_snapshot`
    (see above), so a portal-rendered option list is visible on the very next
    turn after opening the widget, without requesting a full-page snapshot."""
    own = await locator.aria_snapshot()
    own_lines = len(own.strip().splitlines())

    best = own
    best_lines = own_lines
    for levels in (2, 3, 4):
        try:
            ancestor = locator.locator("xpath=" + "/".join([".."] * levels))
            snap = await ancestor.first.aria_snapshot()
        except Exception:
            continue
        snap_lines = len(snap.strip().splitlines())
        if snap_lines > best_lines:
            best, best_lines = snap, snap_lines

    popup_snap = await _related_popup_snapshot(locator)
    if popup_snap.strip() and popup_snap.strip() not in best:
        best = f"{best}\n\n(related open popup, linked via ARIA attributes or found open elsewhere on the page)\n{popup_snap}"

    return best


async def apply_op(page, locator, op: dict):
    """Executes one primitive UI op from a selector-agent recipe. Ops carry
    the literal values the discovering agent used (no `{value}` templating);
    raises on failure so the caller can react (mid-discovery: try something
    else; replay: surface RecipeFailed)."""
    kind = op.get("op")
    if kind == "click_target":
        await locator.click()
    elif kind == "type":
        await locator.page.keyboard.type(op["text"], delay=20)
    elif kind == "key":
        await locator.page.keyboard.press(op["key"])
    elif kind == "clear":
        await locator.page.keyboard.press("Control+A")
        await locator.page.keyboard.press("Delete")
    elif kind == "click_option":
        label = op["label"]
        option = locator.page.get_by_role("option", name=label)
        try:
            await option.first.wait_for(state="visible", timeout=2000)
            await option.first.click()
        except Exception:
            await locator.page.get_by_text(label, exact=False).first.click()
    else:
        raise ValueError(f"Unknown primitive op: {kind!r}")


async def native_select(page, locator, name, value, nth=None) -> str:
    """A real HTML <select> element."""
    await locator.select_option(value)
    actual = await locator.evaluate("el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''")
    if not _values_match(actual, value):
        raise AssertionError(f"'{name}' still shows {actual!r} after selecting {value!r}")
    return actual


async def listbox_click(page, locator, name, value, nth=None) -> str:
    """Opens a custom (non-native) dropdown widget, types `value` to filter
    its options, and clicks the best-matching option. Raises ValueError
    (listing the real options) if nothing matches `value`, or
    AssertionError if the click didn't visibly register a selection."""
    await locator.click()

    try:
        await locator.press_sequentially(value, delay=20)
        await page.wait_for_timeout(150)
    except Exception:
        pass

    options = page.get_by_role("option")
    texts = await _option_texts(options)
    index = _match_option(texts, value)

    if index is None:
        # The typed value matched nothing -- clear it so the full option
        # list is shown, then try matching against that instead.
        try:
            await locator.fill("")
            await page.wait_for_timeout(150)
        except Exception:
            pass
        texts = await _option_texts(options)
        index = _match_option(texts, value)

    if index is None:
        await page.keyboard.press("Escape")
        if texts:
            raise NoMatchingOption(f"no option matching '{value}'; available options are: {texts}")
        raise ValueError(f"no option matching '{value}'; available options are: {texts}")

    chosen_label = texts[index]
    await options.nth(index).click()

    # React re-renders the selected value asynchronously; poll briefly
    # rather than risk reading a stale snapshot. Verify against the
    # option's own label (what we actually clicked), not the caller's raw
    # `value`, since the two may legitimately differ (e.g. value="India"
    # but the clicked option is labelled "India +91").
    context = await _combobox_context(page, name, nth)
    for _ in range(8):
        if _values_match(context, chosen_label) or _label_fragment_present(context, chosen_label):
            return chosen_label
        await page.wait_for_timeout(100)
        context = await _combobox_context(page, name, nth)

    raise AssertionError(f"selected '{chosen_label}' for '{name}' but it is not reflected on the page: {context!r}")


async def type_and_enter(page, locator, name, value, nth=None) -> str:
    """Click the field, type `value`, and press Enter. Covers free-text or
    tag-style inputs that have no separate clickable option list."""
    await locator.click()
    await locator.fill(value)
    await page.wait_for_timeout(100)
    await page.keyboard.press("Enter")

    context = await _combobox_context(page, name, nth)
    for _ in range(8):
        if _values_match(context, value) or _label_fragment_present(context, value):
            return value
        try:
            actual = await locator.input_value()
            if _values_match(actual, value):
                return actual
        except Exception:
            pass
        await page.wait_for_timeout(100)
        context = await _combobox_context(page, name, nth)

    raise AssertionError(f"typed '{value}' into '{name}' but it is not reflected on the page: {context!r}")


async def keyboard_nav(page, locator, name, value, nth=None, max_steps=20) -> str:
    """Click to open the widget, then use ArrowDown/Enter to reach and
    select the matching option. Covers listboxes where clicking an option
    doesn't register but keyboard selection does."""
    await locator.click()
    await page.wait_for_timeout(100)

    options = page.get_by_role("option")
    texts = await _option_texts(options)
    target = _match_option(texts, value)
    if target is None:
        await page.keyboard.press("Escape")
        if texts:
            raise NoMatchingOption(f"no option matching '{value}'; available options are: {texts}")
        raise ValueError(f"no option matching '{value}'; available options are: {texts}")

    target_norm = _normalize(texts[target])
    for _ in range(max_steps):
        active = page.get_by_role("option", selected=True)
        try:
            current_texts = await _option_texts(active)
        except Exception:
            current_texts = []
        if current_texts and _normalize(current_texts[0]) == target_norm:
            break
        await page.keyboard.press("ArrowDown")
        await page.wait_for_timeout(50)
    else:
        await page.keyboard.press("Escape")
        raise AssertionError(f"could not navigate to option '{texts[target]}' for '{name}' via keyboard")

    chosen_label = texts[target]
    await page.keyboard.press("Enter")

    context = await _combobox_context(page, name, nth)
    for _ in range(8):
        if _values_match(context, chosen_label) or _label_fragment_present(context, chosen_label):
            return chosen_label
        await page.wait_for_timeout(100)
        context = await _combobox_context(page, name, nth)

    raise AssertionError(f"selected '{chosen_label}' for '{name}' via keyboard but it is not reflected on the page: {context!r}")


async def click_text_match(page, locator, name, value, nth=None) -> str:
    """Click to open the widget, then click the first visible element on
    the page whose text matches `value`. Covers custom dropdowns whose
    options aren't exposed via role="option"."""
    await locator.click()
    await page.wait_for_timeout(150)

    match = page.get_by_text(value, exact=False).first
    try:
        await match.wait_for(state="visible", timeout=2000)
    except Exception:
        await page.keyboard.press("Escape")
        raise ValueError(f"no visible element containing text '{value}' after opening '{name}'")

    chosen_label = (await match.inner_text()).strip()
    await match.click()

    context = await _combobox_context(page, name, nth)
    for _ in range(8):
        if (_values_match(context, chosen_label) or _label_fragment_present(context, chosen_label)
                or _values_match(context, value)):
            return chosen_label
        await page.wait_for_timeout(100)
        context = await _combobox_context(page, name, nth)

    raise AssertionError(f"clicked text '{chosen_label}' for '{name}' but it is not reflected on the page: {context!r}")

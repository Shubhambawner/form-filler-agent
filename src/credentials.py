"""Persistent credential store for job-portal accounts.

credentials.json structure (mirrors profile.json leaf shape, keyed by domain):
{
  "careers-kinaxis.icims.com": {
    "email":    {"value": "...", "description": "Email used during signup"},
    "password": {"value": "...", "description": "Password set during signup"},
    ...every other field the login agent filled during registration...
  }
}
"""
import os
import json

_PATH = os.path.join(os.path.dirname(__file__), '..', 'kb', 'credentials.json')


def load(domain: str) -> dict | None:
    """Return the stored field dict for `domain`, or None if not found."""
    if not os.path.exists(_PATH):
        return None
    with open(_PATH, encoding="utf-8") as f:
        return json.load(f).get(domain)


def save(domain: str, fields: dict):
    """Upsert `fields` for `domain`.

    `fields` must be shaped like {field_name: {"value": ..., "description": ...}}.
    Existing keys for this domain are overwritten; keys not in `fields` are
    left untouched, so partial updates (e.g. password-only reset) are safe.
    """
    all_creds = {}
    if os.path.exists(_PATH):
        with open(_PATH, encoding="utf-8") as f:
            all_creds = json.load(f)
    existing = all_creds.get(domain, {})
    existing.update(fields)
    all_creds[domain] = existing
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(all_creds, f, indent=2)

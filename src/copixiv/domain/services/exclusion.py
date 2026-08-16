"""Blocked-tag exclusion policy — pure domain rules (docs/MODULARITY.md §M4).

Novels carrying user-blocked (厌恶) tags are hidden from browsing, search
and counts (global toggle, default ON).  The infrastructure layer supplies
the data (the global setting value, the blocked tag names); the *decision*
lives here so repositories never embed business rules.

The runtime-setting key is owned by this module too — the system endpoint
and the read repository both resolve it through here (previously the
string was duplicated across layers).
"""

# Runtime-setting key for the global "exclude blocked-tag novels" toggle.
EXCLUDE_BLOCKED_SETTING_KEY = "exclude_blocked_tag_novels"

# Exclusion is ON when the settings row is missing.
DEFAULT_ACTIVE = True

_TRUTHY = ("1", "true", "yes", "on")


def resolve_active(
    explicit: bool | None,
    global_setting_value: str | None,
) -> bool:
    """Decide whether blocked-tag exclusion applies to a query.

    Args:
        explicit: The API ``exclude_blocked`` override; wins when given.
        global_setting_value: Raw value of the runtime setting row
            (``None`` when the row is missing → default ON).

    Reads are tiny single-row lookups with no caching, so toggles take
    effect immediately — this function must stay pure and cheap.
    """
    if explicit is not None:
        return explicit
    if global_setting_value is None:
        return DEFAULT_ACTIVE
    return global_setting_value.strip().lower() in _TRUTHY

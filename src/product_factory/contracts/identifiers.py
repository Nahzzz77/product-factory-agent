"""Portable protocol identifiers shared by contracts and storage."""

from __future__ import annotations

import re


_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_FORBIDDEN = re.compile(r'[\\/\x00-\x1f<>:"|?*]')


def is_portable_path_component(value: str) -> bool:
    """Return whether ``value`` is one cross-platform, filesystem-safe component."""
    if not isinstance(value, str) or not value or len(value) > 255:
        return False
    if value in {".", ".."} or value.endswith((".", " ")) or _FORBIDDEN.search(value):
        return False
    return value.split(".", 1)[0].upper() not in _WINDOWS_RESERVED

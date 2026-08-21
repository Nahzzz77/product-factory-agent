"""Portable protocol identifiers shared by contracts and storage."""

from __future__ import annotations

import re
import unicodedata


_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_FORBIDDEN = re.compile(r'[\\/\x00-\x1f<>:"|?*]')


def is_portable_path_component(value: str) -> bool:
    """Return whether ``value`` is one cross-platform, filesystem-safe component."""
    if not isinstance(value, str) or not value:
        return False
    try:
        if len(value.encode("utf-8")) > 255 or len(value.encode("utf-16-le")) // 2 > 255:
            return False
    except UnicodeEncodeError:
        return False
    # Keep the stored spelling deterministic on normalizing filesystems.
    if unicodedata.normalize("NFC", value) != value:
        return False
    if value in {".", ".."} or value.endswith((".", " ")) or _FORBIDDEN.search(value):
        return False
    # Windows interprets device stems after compatibility normalization, so
    # COM¹ and COM1 name the same forbidden device even though NFC preserves ¹.
    stem = unicodedata.normalize("NFKC", value.split(".", 1)[0]).upper()
    return stem not in _WINDOWS_RESERVED

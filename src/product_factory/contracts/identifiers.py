"""Portable protocol identifiers shared by contracts and storage."""

from __future__ import annotations

import re


# This deliberately conservative alphabet is representable by ordinary JSON
# Schema.  At most 85 code points are permitted; the allowed non-ASCII code
# points are all three-byte UTF-8 values, so that also implies the Windows
# 255-byte and 255-UTF-16-unit component limits without vendor extensions.
PORTABLE_COMPONENT_PATTERN = (
    r"^[A-Za-z0-9._\u3041-\u3096\u30a1-\u30fa\u30fc-\u30ff"
    r"\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7a3-]{1,85}$"
)
_PORTABLE_COMPONENT = re.compile(PORTABLE_COMPONENT_PATTERN)
PORTABLE_WINDOWS_RESERVED_PATTERN = (
    r"^(?:[Cc][Oo][Nn]|[Pp][Rr][Nn]|[Aa][Uu][Xx]|[Nn][Uu][Ll]|"
    r"[Cc][Oo][Nn][Ii][Nn]\$|[Cc][Oo][Nn][Oo][Uu][Tt]\$|"
    r"[Cc][Ll][Oo][Cc][Kk]\$|[Cc][Oo][Mm][1-9¹²³]|[Ll][Pp][Tt][1-9¹²³])(?:\..*)?$"
)
_WINDOWS_RESERVED = re.compile(PORTABLE_WINDOWS_RESERVED_PATTERN)


def is_portable_path_component(value: str) -> bool:
    """Return whether ``value`` is one cross-platform, filesystem-safe component."""
    if not isinstance(value, str) or not value:
        return False
    if _PORTABLE_COMPONENT.fullmatch(value) is None:
        return False
    if value in {".", ".."} or value.endswith("."):
        return False
    return _WINDOWS_RESERVED.fullmatch(value) is None

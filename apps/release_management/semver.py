"""
apps.release_management.semver
Architecture Decision: ADR-0038
Strict SemVer 2.0.0 parsing, validation, and comparison engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Official SemVer 2.0.0 regex pattern
SEMVER_REGEX = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


class InvalidSemVerError(ValueError):
    """Raised when a version string violates SemVer 2.0.0 specification."""


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: Optional[str] = None
    buildmetadata: Optional[str] = None

    @classmethod
    def parse(cls, version_str: str) -> SemVer:
        if not isinstance(version_str, str):
            raise InvalidSemVerError(f"Version must be a string, got {type(version_str).__name__}")
        cleaned = version_str.strip()
        # Allow optional leading 'v' or 'V' for flexibility, but validate strict format
        if cleaned.startswith(("v", "V")):
            cleaned = cleaned[1:]

        match = SEMVER_REGEX.match(cleaned)
        if not match:
            raise InvalidSemVerError(
                f"Version string '{version_str}' is not valid SemVer 2.0.0 (e.g. '1.2.3' or '1.0.0-rc.1')"
            )

        groups = match.groupdict()
        return cls(
            major=int(groups["major"]),
            minor=int(groups["minor"]),
            patch=int(groups["patch"]),
            prerelease=groups.get("prerelease"),
            buildmetadata=groups.get("buildmetadata"),
        )

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += f"-{self.prerelease}"
        if self.buildmetadata:
            base += f"+{self.buildmetadata}"
        return base

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            try:
                other = SemVer.parse(str(other))
            except InvalidSemVerError:
                return False
        return (self.major, self.minor, self.patch, self.prerelease) == (
            other.major,
            other.minor,
            other.patch,
            other.prerelease,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            other = SemVer.parse(str(other))

        # 1. Compare major, minor, patch numerically
        if (self.major, self.minor, self.patch) != (other.major, other.minor, other.patch):
            return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)

        # 2. Pre-release comparison rule: A normal version has greater precedence than a pre-release version
        if self.prerelease is None and other.prerelease is not None:
            return False
        if self.prerelease is not None and other.prerelease is None:
            return True
        if self.prerelease is None and other.prerelease is None:
            return False

        # Both have pre-release strings
        return _compare_prerelease(self.prerelease, other.prerelease) < 0

    def __le__(self, other: object) -> bool:
        return self == other or self < other

    def __gt__(self, other: object) -> bool:
        return not (self <= other)

    def __ge__(self, other: object) -> bool:
        return not (self < other)


def _compare_prerelease(pre1: str, pre2: str) -> int:
    """Compares two SemVer 2.0.0 pre-release strings identifier-by-identifier."""
    parts1 = pre1.split(".")
    parts2 = pre2.split(".")

    for p1, p2 in zip(parts1, parts2):
        is_num1 = p1.isdigit()
        is_num2 = p2.isdigit()

        if is_num1 and is_num2:
            n1, n2 = int(p1), int(p2)
            if n1 != n2:
                return -1 if n1 < n2 else 1
        elif is_num1 and not is_num2:
            # Numeric identifiers have lower precedence than non-numeric
            return -1
        elif not is_num1 and is_num2:
            return 1
        else:
            # Lexical comparison
            if p1 != p2:
                return -1 if p1 < p2 else 1

    # If all compared identifiers are equal, the longer list has higher precedence
    if len(parts1) < len(parts2):
        return -1
    elif len(parts1) > len(parts2):
        return 1
    return 0


def compare_semver(v1: str | SemVer, v2: str | SemVer) -> int:
    """
    Returns:
      -1 if v1 < v2
       0 if v1 == v2
       1 if v1 > v2
    """
    sv1 = v1 if isinstance(v1, SemVer) else SemVer.parse(v1)
    sv2 = v2 if isinstance(v2, SemVer) else SemVer.parse(v2)
    if sv1 < sv2:
        return -1
    elif sv1 > sv2:
        return 1
    return 0


def evaluate_client_compatibility(
    client_version_str: str,
    min_supported_version_str: str,
    target_version_str: str,
    is_revoked: bool = False,
) -> str:
    """
    Evaluates compatibility status according to ADR-0038.
    Returns: 'SUPPORTED', 'DEPRECATED', or 'UPGRADE_REQUIRED'.
    """
    if is_revoked:
        return "UPGRADE_REQUIRED"

    client = SemVer.parse(client_version_str)
    min_supported = SemVer.parse(min_supported_version_str)
    target = SemVer.parse(target_version_str)

    if client < min_supported:
        return "UPGRADE_REQUIRED"
    elif client < target:
        return "DEPRECATED"
    return "SUPPORTED"

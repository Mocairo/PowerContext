"""Credential detection and redaction for imported evidence."""

from __future__ import annotations

import re

# These patterns are byte-for-byte the `sensitive_content.patterns` entries of
# ``configs/security.yaml``. The file is documentation-grade configuration today
# (no config-injection layer exists yet), so ``tests/unit/test_import_security.py``
# pins both the literal pattern text and the detection behaviour.
PATTERNS: dict[str, re.Pattern[str]] = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer_token": re.compile(r"Authorization: Bearer [A-Za-z0-9._~+/=-]{16,}"),
    "api_secret": re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|secret[_-]?key)\s*[:=]\s*\S{8,}"
    ),
    "password_assignment": re.compile(r"(?i)password\s*[:=]\s*\S{8,}"),
}

# Matches of the form ``label <separator> value`` keep their label visible after
# redaction so a reader still knows which credential was removed; the remaining
# patterns have no label to keep and are replaced as a whole.
_ASSIGNMENT_PATTERNS = ("api_secret", "password_assignment")
_WHOLE_MATCH_PATTERNS = ("private_key", "bearer_token")

# ``password_assignment`` is written without a capture group in the config, so the
# label is recovered from the start of the match instead of ``group(1)``.
_LABEL_PREFIX = re.compile(r"[A-Za-z0-9_-]+")


def scan_text(text: str) -> list[str]:
    """Return the names of every credential pattern present in ``text``."""
    return [name for name, pattern in PATTERNS.items() if pattern.search(text)]


def _label_of(match: re.Match[str]) -> str:
    if match.lastindex:
        return match.group(1)
    prefix = _LABEL_PREFIX.match(match.group(0))
    return prefix.group(0) if prefix else ""


def redact_text(text: str) -> str:
    """Replace detected credential values with ``[REDACTED]`` before indexing."""
    result = text
    for name in _ASSIGNMENT_PATTERNS:
        result = PATTERNS[name].sub(lambda match: f"{_label_of(match)}=[REDACTED]", result)
    for name in _WHOLE_MATCH_PATTERNS:
        result = PATTERNS[name].sub("[REDACTED]", result)
    return result

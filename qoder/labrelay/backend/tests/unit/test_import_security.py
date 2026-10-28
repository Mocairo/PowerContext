import re
from pathlib import Path

import yaml

from labrelay.importers.security import PATTERNS, redact_text, scan_text

SECURITY_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "security.yaml"

POSITIVE_SAMPLES: list[tuple[str, str]] = [
    ("private_key", "-----BEGIN RSA PRIVATE KEY-----\nMIIBVwIDAQAB\n"),
    ("bearer_token", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"),
    ("api_secret", "api_key=super-secret-value"),
    ("api_secret", "SECRET_KEY: super_secret_value"),
    ("password_assignment", "password=another-secret-value"),
]

# Values that must never survive redaction, paired with the text carrying them.
SECRET_VALUES: list[tuple[str, str]] = [
    ("api_key=super-secret-value", "super-secret-value"),
    ("password=another-secret-value", "another-secret-value"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
]

NEGATIVE_SAMPLES: list[str] = [
    "# next\nrun lr sweep\n",
    "def train():\n    return 1  # nothing secret here\n",
    "learning_rate: 0.001\n",
]


def _sensitive_config() -> dict[str, object]:
    config = yaml.safe_load(SECURITY_CONFIG.read_text(encoding="utf-8"))
    return dict(config["sensitive_content"])


def _yaml_patterns() -> dict[str, str]:
    return {
        str(entry["name"]): str(entry["pattern"])
        for entry in _sensitive_config()["patterns"]  # type: ignore[arg-type]
    }


def test_scan_identifies_credentials_and_passwords() -> None:
    findings = scan_text(
        "api_key=super-secret-value\npassword=another-secret-value"
    )
    assert "api_secret" in findings
    assert "password_assignment" in findings


def test_redaction_replaces_matched_values_without_replacing_labels() -> None:
    assert (
        redact_text("api_key=super-secret-value")
        == "api_key=[REDACTED]"
    )


def test_scan_detects_private_keys_and_bearer_tokens() -> None:
    assert "private_key" in scan_text("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n")
    assert "bearer_token" in scan_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")


def test_scan_ignores_ordinary_technical_writing() -> None:
    for sample in NEGATIVE_SAMPLES:
        assert scan_text(sample) == []


def test_redaction_masks_every_detected_pattern() -> None:
    for name, sample in POSITIVE_SAMPLES:
        redacted = redact_text(sample)
        assert "[REDACTED]" in redacted, name
        assert redacted != sample, name


def test_redaction_removes_credential_values() -> None:
    for sample, secret in SECRET_VALUES:
        assert secret not in redact_text(sample)


def test_redaction_preserves_labels_and_leaves_clean_text_untouched() -> None:
    assert redact_text("password: hunter2hunter2") == "password=[REDACTED]"
    assert redact_text("PASSWORD=super-secret-value") == "PASSWORD=[REDACTED]"
    assert redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz") == "[REDACTED]"
    assert redact_text("# next\nrun lr sweep\n") == "# next\nrun lr sweep\n"


# --- consistency with configs/security.yaml ----------------------------------


def test_patterns_are_the_ones_declared_in_security_yaml() -> None:
    assert SECURITY_CONFIG.is_file(), f"missing {SECURITY_CONFIG}"

    assert set(PATTERNS) == set(_yaml_patterns())
    assert {name: pattern.pattern for name, pattern in PATTERNS.items()} == _yaml_patterns()


def test_detection_semantics_agree_with_security_yaml() -> None:
    configured = {name: re.compile(raw) for name, raw in _yaml_patterns().items()}

    for name, sample in POSITIVE_SAMPLES:
        assert configured[name].search(sample), name
        assert PATTERNS[name].search(sample), name
        assert name in scan_text(sample)

    for sample in NEGATIVE_SAMPLES:
        yaml_hits = [name for name, pattern in configured.items() if pattern.search(sample)]
        assert yaml_hits == []
        assert scan_text(sample) == []


def test_security_yaml_still_requires_restriction_and_export_scan() -> None:
    sensitive = _sensitive_config()
    assert sensitive["restrict_asset_when_detected"] is True
    assert sensitive["scan_before_public_export"] is True

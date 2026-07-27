from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
GIB = 1024**3


class FakeResource:
    RLIMIT_AS = 9
    RLIM_INFINITY = -1

    def __init__(self, limits: tuple[int, int]) -> None:
        self.limits = limits
        self.calls: list[tuple[int, tuple[int, int]]] = []

    def getrlimit(self, resource_id: int) -> tuple[int, int]:
        assert resource_id == self.RLIMIT_AS
        return self.limits

    def setrlimit(self, resource_id: int, limits: tuple[int, int]) -> None:
        self.calls.append((resource_id, limits))


def _workflow() -> dict[str, object]:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def test_pytest_has_thread_timeout_safety_net() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'addopts = "--timeout=120 --timeout-method=thread"' in pyproject


def test_default_address_space_limit_is_two_gib() -> None:
    from tests.resource_limits import apply_address_space_limit

    resource = FakeResource((-1, -1))
    applied = apply_address_space_limit({}, resource)
    assert applied == 2 * GIB
    assert resource.calls == [(resource.RLIMIT_AS, (2 * GIB, -1))]


def test_address_space_limit_can_be_disabled_by_environment() -> None:
    from tests.resource_limits import apply_address_space_limit

    resource = FakeResource((-1, -1))
    assert apply_address_space_limit({"PYTEST_RLIMIT_AS_GB": "0"}, resource) is None
    assert resource.calls == []


def test_address_space_limit_environment_override_and_hard_ceiling() -> None:
    from tests.resource_limits import apply_address_space_limit

    resource = FakeResource((-1, 3 * GIB))
    assert apply_address_space_limit({"PYTEST_RLIMIT_AS_GB": "1.5"}, resource) == int(
        1.5 * GIB
    )
    assert resource.calls == [(resource.RLIMIT_AS, (int(1.5 * GIB), 3 * GIB))]

    resource = FakeResource((GIB, GIB))
    assert apply_address_space_limit({"PYTEST_RLIMIT_AS_GB": "4"}, resource) == GIB
    assert resource.calls == []


def test_address_space_limit_reports_preexisting_lower_soft_limit() -> None:
    from tests.resource_limits import apply_address_space_limit

    resource = FakeResource((GIB, -1))
    assert apply_address_space_limit({"PYTEST_RLIMIT_AS_GB": "2"}, resource) == GIB
    assert resource.calls == []


def test_address_space_limit_is_portable_when_resource_is_unavailable(
    monkeypatch,
) -> None:
    from tests import resource_limits

    monkeypatch.setattr(resource_limits, "_load_resource_module", lambda: None)
    assert resource_limits.apply_address_space_limit({}) is None


@pytest.mark.parametrize("value", ["-1", "nan", "infinity", "not-a-number"])
def test_address_space_limit_rejects_invalid_override(value: str) -> None:
    from tests.resource_limits import apply_address_space_limit

    with pytest.raises(ValueError, match="PYTEST_RLIMIT_AS_GB"):
        apply_address_space_limit(
            {"PYTEST_RLIMIT_AS_GB": value}, FakeResource((-1, -1))
        )


def test_workflow_uses_supported_test_environments_and_immutable_actions() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert {
        "stable",
        "architecture-smoke",
        "ruff",
        "hassfest",
        "hacs",
        "beta-canary",
        "samsung-pin-canary",
    } <= jobs.keys()
    assert "minimum" not in jobs

    rendered = WORKFLOW_PATH.read_text()
    assert 'python-version: "3.14"' in rendered
    assert "requirements_test.txt" in rendered
    assert (
        "pytest-homeassistant-custom-component==0.13.347"
        in (ROOT / "requirements_test.txt").read_text()
    )
    assert not (ROOT / "requirements_test_min.txt").exists()
    content = (ROOT / "requirements_test.txt").read_text()
    assert "pytest-timeout==2.4.0" in content
    assert "pytest-cov==7.1.0" in content
    hacs = yaml.safe_load((ROOT / "hacs.json").read_text())
    assert hacs["homeassistant"] == "2026.7.3"
    assert "ubuntu-24.04-arm" in rendered
    assert "linux/amd64" in rendered
    assert "linux/arm64" in rendered
    assert "cryptography==48.0.1" in rendered
    assert "ruff check custom_components tests .github/scripts" in rendered
    assert "@master" not in rendered
    assert "@main" not in rendered
    for job in jobs.values():
        for step in job["steps"]:
            if uses := step.get("uses"):
                revision = uses.rsplit("@", 1)[-1]
                assert len(revision) == 40
                assert set(revision) <= set("0123456789abcdef")


def test_workflow_enforces_dependency_and_resource_safety_contracts() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    for name in ("stable", "beta-canary"):
        job = jobs[name]
        rendered = yaml.safe_dump(job)
        assert job["timeout-minutes"] <= 30
        assert "pip check" in rendered
        assert "pip freeze" in rendered
        assert "sha256sum dependency-closure-" in rendered
        assert "test_dependency_contract.py" in rendered
        assert "ulimit -v 2097152" in rendered
        assert "--timeout=120" in rendered
    smoke = yaml.safe_dump(jobs["architecture-smoke"])
    assert "--memory=2g" in smoke
    assert "--network=none" in smoke
    assert "import smartthings_local, cbor2" in smoke
    for name in ("stable", "beta-canary"):
        upload = jobs[name]["steps"][-1]
        assert upload["if"] == "always()"
        assert ".sha256" in upload["with"]["path"]


def test_workflow_canaries_are_scheduled_nonblocking_and_exact() -> None:
    workflow = _workflow()
    assert "schedule" in workflow[True]
    jobs = workflow["jobs"]
    beta = yaml.safe_dump(jobs["beta-canary"])
    pins = yaml.safe_dump(jobs["samsung-pin-canary"])
    assert jobs["beta-canary"]["continue-on-error"] is True
    assert jobs["samsung-pin-canary"]["continue-on-error"] is True
    assert "--pre --upgrade pytest-homeassistant-custom-component" in beta
    assert "smartthings-local==0.1.0" in beta
    assert "cbor2==6.1.3" in beta
    assert "homeassistant" in beta
    assert ".github/scripts/check_samsung_pins.py" in pins
    assert "git diff --exit-code" in pins


def test_workflow_uses_required_validation_actions_and_no_secrets() -> None:
    rendered = WORKFLOW_PATH.read_text()
    for action_and_version in (
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5",
        "home-assistant/actions/hassfest@e3fb68ebda13d88a0d695082f471ba2c83d025fb",
        "hacs/action@1ebf01c408f29afcb6406bd431bc98fd8cbb15aa",
    ):
        assert action_and_version in rendered
    assert "secrets." not in rendered
    assert "pull_request_target" not in rendered
    hacs = _workflow()["jobs"]["hacs"]
    assert hacs["steps"][-1]["with"]["comment"] is False
    # Brands are shipped in-tree at custom_components/<domain>/brand/, so the
    # HACS brands check must run rather than be ignored.
    assert "ignore" not in hacs["steps"][-1]["with"]


def test_pin_canary_reads_release_constants_without_importing_home_assistant() -> None:
    script = (ROOT / ".github" / "scripts" / "check_samsung_pins.py").read_text()
    assert "from custom_components" not in script
    assert "ast.parse" in script
    for name in (
        "BUNDLE_SHA256",
        "BUNDLE_URL",
        "SAMSUNG_IDENTITY_HOST",
        "SAMSUNG_IDENTITY_LEAF_SHA256",
        "SAMSUNG_IDENTITY_SPKI_SHA256",
    ):
        assert name in script
    assert "ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)" in script
    assert "context.check_hostname = False" in script
    assert "context.verify_mode = ssl.CERT_NONE" in script


def test_changelog_release_evidence_is_counts_only() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text()
    marker = "### Verification"
    assert marker in changelog
    verification = changelog.split(marker, 1)[1]
    assert "local automated tests:" in verification
    assert "coverage:" in verification
    assert "direct live AC identity/read/write/restoration matrix: passed" in (
        verification
    )
    assert "Home Assistant production-console smoke: pending" in verification
    forbidden = ("192.168.", "uuid:", "BEGIN PRIVATE KEY", "/power/vs/")
    assert not any(value in verification for value in forbidden)


def test_sanitized_live_matrix_records_acceptance_rejection_and_restoration() -> None:
    evidence = json.loads(
        (ROOT / "tests" / "fixtures" / "live_capability_matrix.json").read_text()
    )
    assert evidence["evidence_version"] == 1
    assert evidence["scope"] == "single-unit-sha256"
    assert evidence["transport"]["resource_count"] == 39
    assert evidence["transport"]["directory_descriptor"] == {
        "rt": ["x.com.samsung.devcol", "oic.wk.col"],
        "if": ["oic.if.baseline", "oic.if.ll", "oic.if.b"],
    }
    assert set(evidence["accepted"]["hvac_modes"]) == {
        "auto",
        "cool",
        "dry",
        "fan_only",
        "heat",
    }
    assert set(evidence["accepted"]["cool_fan_modes"]) == {
        "auto",
        "low",
        "medium",
        "high",
        "turbo",
    }
    assert set(evidence["accepted"]["cool_swing_modes"]) == {
        "fixed",
        "vertical",
        "horizontal",
        "both",
    }
    assert evidence["rejected"] == [
        {"control": "dry_comfort", "condition": "cool"},
        {"control": "auto_clean", "condition": "power_off"},
    ]
    assert evidence["restoration"]["passed"] is True
    serialized = json.dumps(evidence)
    assert not any(
        value in serialized
        for value in ("192.168.", "uuid:", "BEGIN PRIVATE KEY", "/power/vs/")
    )

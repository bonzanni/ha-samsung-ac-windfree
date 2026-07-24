from __future__ import annotations

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


@pytest.mark.parametrize("value", ["-1", "nan", "infinity", "not-a-number"])
def test_address_space_limit_rejects_invalid_override(value: str) -> None:
    from tests.resource_limits import apply_address_space_limit

    with pytest.raises(ValueError, match="PYTEST_RLIMIT_AS_GB"):
        apply_address_space_limit(
            {"PYTEST_RLIMIT_AS_GB": value}, FakeResource((-1, -1))
        )


def test_workflow_uses_pinned_supported_test_environments() -> None:
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


def test_workflow_enforces_dependency_and_resource_safety_contracts() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    for name in ("stable", "beta-canary"):
        job = jobs[name]
        rendered = yaml.safe_dump(job)
        assert job["timeout-minutes"] <= 30
        assert "pip check" in rendered
        assert "pip freeze" in rendered
        assert "test_dependency_contract.py" in rendered
        assert "ulimit -v 2097152" in rendered
        assert "--timeout=120" in rendered
    smoke = yaml.safe_dump(jobs["architecture-smoke"])
    assert "--memory=2g" in smoke
    assert "--network=none" in smoke
    assert "import smartthings_local, cbor2" in smoke


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
    for action in (
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "home-assistant/actions/hassfest@master",
        "hacs/action@main",
    ):
        assert action in rendered
    assert "secrets." not in rendered
    assert "pull_request_target" not in rendered
    hacs = _workflow()["jobs"]["hacs"]
    assert hacs["steps"][-1]["with"]["comment"] is False
    assert hacs["steps"][-1]["with"]["ignore"] == "brands"


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
    assert "live AC gate: pending" in verification
    forbidden = ("192.168.", "uuid:", "BEGIN PRIVATE KEY", "/power/vs/")
    assert not any(value in verification for value in forbidden)

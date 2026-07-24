from __future__ import annotations

import asyncio
import copy
import json
import logging
import traceback
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import AsyncMock, call

import pytest

from custom_components.samsung_ac_windfree.const import COMPATIBILITY
from custom_components.samsung_ac_windfree.coordinator import (
    COLD_PATHS,
    HOT_PATHS,
    RECONCILE_PATHS,
    WARM_PATHS,
    PollTier,
    WindFreeCoordinator,
)
from custom_components.samsung_ac_windfree.device import (
    HVAC_MODE_PATH,
    POWER_PATH,
    TEMPERATURE_PATH,
    CommandKind,
)
from custom_components.samsung_ac_windfree.models import (
    AuthenticationRejected,
    CommandRejected,
    HvacMode,
    PresetMode,
    UpdateSource,
)
from custom_components.samsung_ac_windfree.transport import TransportError


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class FakeTransportFactory:
    def __init__(self, resources: dict[str, dict[str, object]]) -> None:
        self.resources = copy.deepcopy(resources)
        self.resources["/device/0"].update(
            {
                path: copy.deepcopy(value)
                for path, value in resources.items()
                if path not in {"/oic/d", "/oic/p", "/device/0"}
            }
        )
        self.transports: list[AsyncMock] = []
        self.observe_error: BaseException | None = None
        self.next_close_side_effect: BaseException | list[object] | None = None
        self.block_generation: int | None = None
        self.seed_started = asyncio.Event()
        self.seed_release = asyncio.Event()
        self.reconnect = AsyncMock(side_effect=self._reconnect)
        self.discover = AsyncMock(side_effect=self._discover)

    @property
    def current(self) -> AsyncMock:
        return self.transports[-1]

    def create(self, *, generation: int, **_: object) -> AsyncMock:
        transport = AsyncMock()
        transport.generation = generation

        async def get(path: str) -> dict[str, object]:
            return await self._get_for(generation, path)

        transport.async_get.side_effect = get
        transport.async_post.side_effect = self._post
        if self.observe_error is not None:
            transport.async_observe.side_effect = self.observe_error
        if self.next_close_side_effect is not None:
            transport.async_close.side_effect = self.next_close_side_effect
            self.next_close_side_effect = None
        self.transports.append(transport)
        return transport

    async def _reconnect(self, *, generation: int, **_: object) -> AsyncMock:
        return self.create(generation=generation)

    async def _discover(self, *, generation: int, **_: object) -> tuple[int, AsyncMock]:
        return 49155, self.create(generation=generation)

    async def _get(self, path: str) -> dict[str, object]:
        return copy.deepcopy(self.resources[path])

    async def _get_for(self, generation: int, path: str) -> dict[str, object]:
        if generation == self.block_generation and path == "/device/0":
            self.seed_started.set()
            await self.seed_release.wait()
        return await self._get(path)

    async def _post(self, path: str, payload: object) -> None:
        self.resources[path] = copy.deepcopy(payload)
        self.resources["/device/0"][path] = copy.deepcopy(payload)


@pytest.fixture
def compatibility() -> dict[str, object]:
    return json.loads(
        (Path(__file__).parent / "fixtures" / "mode_compatibility.json").read_text()
    )


def test_live_compatibility_fixture_matches_production_contract(
    compatibility: dict[str, object],
) -> None:
    assert compatibility == {
        "always_allowed": list(COMPATIBILITY["always_allowed"]),
        "by_mode": {
            mode: list(controls) for mode, controls in COMPATIBILITY["by_mode"].items()
        },
    }


@pytest.fixture
async def coordinator(
    hass,
    credentials,
    resource_representations,
    compatibility,
):
    clock = FakeClock()
    factory = FakeTransportFactory(resource_representations)
    instance = WindFreeCoordinator(
        hass,
        host="device.invalid",
        port=49154,
        credentials=credentials,
        compatibility=compatibility,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        transport_factory=factory,
        observe_wait=0,
        start_scheduler=False,
    )
    await instance.async_start()
    yield instance
    await instance.async_shutdown()


async def test_start_seeds_identity_contract_observe_and_immutable_data(
    coordinator: WindFreeCoordinator,
) -> None:
    assert coordinator.data.available
    assert coordinator.data.generation == 1
    assert coordinator.data.identity is not None
    assert coordinator.data.update_source is UpdateSource.RECONCILE
    assert (
        tuple(
            call.args[0] for call in coordinator.transport.async_get.await_args_list[:3]
        )
        == RECONCILE_PATHS
    )
    coordinator.transport.async_observe.assert_awaited_once()
    assert set(coordinator.transport.async_observe.await_args.args[0]) == set(
        HOT_PATHS + WARM_PATHS
    )
    with pytest.raises(FrozenInstanceError):
        coordinator.data.available = False


async def test_health_snapshot_is_fixed_safe_and_counts_update_sources(
    coordinator: WindFreeCoordinator,
) -> None:
    initial = coordinator.health
    assert initial.generation == 1
    assert initial.reconcile_count >= 1
    assert initial.observe_count == 0
    assert not hasattr(initial, "host")
    assert not hasattr(initial, "resources")

    coordinator.handle_observe(
        generation=coordinator.generation,
        path=POWER_PATH,
        representation={"x.com.samsung.da.power": "On"},
    )

    health = coordinator.health
    assert health.observe_count == 1
    assert health.source is UpdateSource.OBSERVE
    assert health.hot_age_seconds >= 0
    assert health.latency_under_100ms >= 0


async def test_failed_exact_range_discovery_sets_and_recovery_clears_health(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    coordinator._stored_port_failures = 2
    factory.reconnect.side_effect = TransportError("connect")
    factory.discover.side_effect = ConnectionError(
        "transport_discovery_failed: private details"
    )

    await coordinator.async_run_reconnect_attempt()

    assert coordinator.health.port_range_exhausted
    assert coordinator.health.reconnect_attempts == 1

    factory.reconnect.side_effect = factory._reconnect
    await coordinator.async_run_reconnect_attempt()

    assert not coordinator.health.port_range_exhausted


async def test_old_generation_observe_is_ignored(
    coordinator: WindFreeCoordinator,
) -> None:
    before = coordinator.data
    coordinator.handle_observe(
        generation=coordinator.generation - 1,
        path=POWER_PATH,
        representation={"x.com.samsung.da.power": "Off"},
    )
    assert coordinator.data is before


async def test_current_observe_merges_one_resource_and_moves_deadline(
    coordinator: WindFreeCoordinator,
) -> None:
    before = coordinator.deadline_for(POWER_PATH)
    coordinator.handle_observe(
        generation=coordinator.generation,
        path=POWER_PATH,
        representation={"x.com.samsung.da.power": "On"},
    )
    assert coordinator.data.climate.power
    assert coordinator.data.update_source is UpdateSource.OBSERVE
    assert coordinator.deadline_for(POWER_PATH) > before


async def test_deadlines_are_staggered_and_priority_is_tier_then_deadline(
    coordinator: WindFreeCoordinator,
) -> None:
    assert len({coordinator.deadline_for(path) for path in HOT_PATHS}) == len(HOT_PATHS)
    coordinator.force_due(COLD_PATHS[0], due=1)
    coordinator.force_due(WARM_PATHS[0], due=2)
    coordinator.force_due(HOT_PATHS[0], due=3)
    assert coordinator.next_due().tier is PollTier.HOT
    coordinator.force_due(HOT_PATHS[0], due=50_000)
    assert coordinator.next_due().tier is PollTier.WARM


async def test_scheduler_prunes_superseded_heap_revisions(
    coordinator: WindFreeCoordinator,
) -> None:
    for revision in range(100):
        coordinator.force_due(HOT_PATHS[0], due=float(revision))
    assert len(coordinator._heap) <= len(coordinator._deadlines) * 2
    coordinator.next_due()
    assert len(coordinator._heap) < len(coordinator._deadlines) * 2


async def test_scheduler_compacts_thousands_of_future_revisions(
    coordinator: WindFreeCoordinator,
) -> None:
    latest: dict[str, float] = {}
    for revision in range(3000):
        path = HOT_PATHS[revision % len(HOT_PATHS)]
        deadline = 10_000.0 + revision
        latest[path] = deadline
        coordinator.force_due(path, due=deadline)

    coordinator.force_due(COLD_PATHS[0], due=0)
    coordinator.force_due(WARM_PATHS[0], due=0)
    coordinator.force_due(HOT_PATHS[0], due=0)
    latest[HOT_PATHS[0]] = 0
    assert len(coordinator._heap) <= len(coordinator._deadlines) * 2
    for path, deadline in latest.items():
        assert coordinator.deadline_for(path) == deadline
    selected = coordinator.next_due()
    assert selected.path == HOT_PATHS[0]
    assert selected.tier is PollTier.HOT
    assert selected.deadline == 0


async def test_run_scheduled_once_admits_atomic_device_tree_read(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.force_reconcile_due(0)
    coordinator.transport.async_get.reset_mock()
    await coordinator.async_run_scheduled_once()
    assert coordinator.transport.async_get.await_args_list == [
        call("/oic/d"),
        call("/oic/p"),
        call("/device/0"),
    ]


async def test_reconcile_is_due_every_five_minutes(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.force_reconcile_due(0)
    await coordinator.async_run_scheduled_once()
    assert coordinator.reconcile_deadline == pytest.approx(400.0)


async def test_hot_age_and_latency_statistics_are_monotonic_measurements(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator._monotonic.__self__.now += 7  # type: ignore[attr-defined]
    assert coordinator.stalest_hot_age >= 7
    before = sum(coordinator.latency_buckets.values())
    coordinator.transport.async_get.reset_mock()
    await coordinator.async_poll_path(HOT_PATHS[0])
    assert sum(coordinator.latency_buckets.values()) == before + 1


async def test_three_hot_failures_mark_unavailable_but_never_resweep(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    coordinator.transport.async_get.side_effect = TimeoutError
    await coordinator.async_run_hot_cycle()
    await coordinator.async_run_hot_cycle()
    await coordinator.async_run_hot_cycle()
    assert not coordinator.data.available
    factory.discover.assert_not_awaited()


async def test_three_reconnect_failures_trigger_exact_range_resweep(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    factory.reconnect.side_effect = ConnectionError
    factory.discover.side_effect = ConnectionError
    await coordinator.async_run_reconnect_attempt()
    await coordinator.async_run_reconnect_attempt()
    factory.discover.assert_not_awaited()
    await coordinator.async_run_reconnect_attempt()
    factory.discover.assert_awaited_once()


async def test_reconnect_backoff_is_two_to_sixty_seconds(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    factory.reconnect.side_effect = ConnectionError
    factory.discover.side_effect = ConnectionError
    delays = []
    for _ in range(7):
        await coordinator.async_run_reconnect_attempt()
        delays.append(coordinator.reconnect_delay)
    assert delays == [2, 4, 8, 16, 32, 60, 60]


async def test_fatal_auth_requires_same_signal_on_fresh_generation(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    factory.reconnect.side_effect = [
        TransportError("connect", fatal_alert="unknown_ca"),
        TransportError("connect", fatal_alert="bad_certificate"),
        TransportError("connect", fatal_alert="unknown_ca"),
    ]
    await coordinator.async_run_reconnect_attempt()
    await coordinator.async_run_reconnect_attempt()
    with pytest.raises(AuthenticationRejected):
        await coordinator.async_run_reconnect_attempt()


async def test_authorization_code_is_fatal_only_on_validated_path(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    factory.reconnect.side_effect = [
        TransportError("get", coap_code=129),
        TransportError("get", coap_code=129),
    ]
    await coordinator.async_run_reconnect_attempt()
    with pytest.raises(AuthenticationRejected):
        await coordinator.async_run_reconnect_attempt()


async def test_post_handshake_authorization_failure_is_confirmed_across_generations(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    original_get = factory._get

    async def unauthorized(path: str) -> dict[str, object]:
        if path == "/oic/d":
            raise TransportError("transport_get_rejected", coap_code=129)
        return await original_get(path)

    factory._get = unauthorized
    await coordinator.async_run_reconnect_attempt()
    with pytest.raises(AuthenticationRejected):
        await coordinator.async_run_reconnect_attempt()


async def test_three_short_authenticated_generations_set_competing_reason_then_clear(
    coordinator: WindFreeCoordinator,
) -> None:
    for _ in range(3):
        await coordinator.note_generation_authenticated()
        await coordinator.note_generation_dead()
    assert coordinator.connection_reason == "possible_competing_session"
    await coordinator.note_generation_authenticated()
    coordinator._monotonic.__self__.now += 11  # type: ignore[attr-defined]
    coordinator.note_generation_stable()
    assert coordinator.connection_reason is None


async def test_temperature_command_gets_fresh_aggregate_under_lock(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport.async_get.reset_mock()
    await coordinator.async_command(CommandKind.TEMPERATURE, 27.0)
    assert coordinator.transport.async_get.await_args_list[0].args == (
        TEMPERATURE_PATH,
    )
    coordinator.transport.async_post.assert_awaited_once()
    assert coordinator.data.climate.target_temperature == 27


async def test_changed_without_matching_readback_is_rejected(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport.async_post.return_value = None
    coordinator.transport.async_get.side_effect = None
    coordinator.transport.async_get.return_value = {"x.com.samsung.da.power": "Off"}
    with pytest.raises(CommandRejected):
        await coordinator.async_command(CommandKind.POWER, True)


async def test_rejected_aggregate_command_scrubs_payload_from_traceback(
    coordinator: WindFreeCoordinator,
) -> None:
    secret = "adversarial-aggregate-secret"
    aggregate = copy.deepcopy(coordinator.transport_factory.resources[TEMPERATURE_PATH])
    aggregate["unknown"] = secret
    coordinator.transport.async_post.side_effect = None
    coordinator.transport.async_post.return_value = None
    coordinator.transport.async_get.side_effect = None
    coordinator.transport.async_get.return_value = aggregate

    with pytest.raises(CommandRejected) as caught:
        await coordinator.async_command(CommandKind.TEMPERATURE, 27)

    coordinator_frames = [
        frame
        for frame, _line in traceback.walk_tb(caught.value.__traceback__)
        if frame.f_globals.get("__name__")
        == "custom_components.samsung_ac_windfree.coordinator"
    ]
    assert coordinator_frames
    for frame in coordinator_frames:
        assert all(secret not in repr(value) for value in frame.f_locals.values())


async def test_matching_observe_arriving_during_post_avoids_fallback_get(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator._observe_wait = 0.01

    async def post(path: str, payload: object) -> None:
        await coordinator.transport_factory._post(path, payload)
        coordinator.handle_observe(coordinator.generation, path, payload)

    coordinator.transport.async_post.side_effect = post
    coordinator.transport.async_get.reset_mock()
    await coordinator.async_command(CommandKind.POWER, True)
    coordinator.transport.async_get.assert_not_awaited()


async def test_matching_observe_is_cached_and_signaled_before_final_publication(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator._observe_wait = 0.01
    observe_merged = asyncio.Event()
    post_release = asyncio.Event()
    snapshots = []

    async def post(path: str, payload: object) -> None:
        await coordinator.transport_factory._post(path, payload)
        coordinator.handle_observe(coordinator.generation, path, payload)
        assert coordinator._resources[path] == payload
        assert coordinator._observe_events[path].is_set()
        observe_merged.set()
        await post_release.wait()

    coordinator.async_add_listener(lambda: snapshots.append(coordinator.data))
    coordinator.transport.async_post.side_effect = post
    coordinator.transport.async_get.reset_mock()
    operation = asyncio.create_task(coordinator.async_command(CommandKind.POWER, True))
    await observe_merged.wait()

    assert snapshots == []
    assert not coordinator.data.climate.power

    post_release.set()
    await operation
    coordinator.transport.async_get.assert_not_awaited()
    assert len(snapshots) == 1
    assert snapshots[0].climate.power
    assert snapshots[0].update_source is UpdateSource.COMMAND


async def test_foreground_command_prevents_poll_interleaving(
    coordinator: WindFreeCoordinator,
) -> None:
    post_started = asyncio.Event()
    post_release = asyncio.Event()
    original_post = coordinator.transport_factory._post

    async def blocking_post(path: str, payload: object) -> None:
        await original_post(path, payload)
        post_started.set()
        await post_release.wait()

    coordinator.transport.async_post.side_effect = blocking_post
    command = asyncio.create_task(coordinator.async_command(CommandKind.POWER, True))
    await post_started.wait()
    coordinator.transport.async_get.reset_mock()
    poll = asyncio.create_task(coordinator.async_poll_path(HVAC_MODE_PATH))
    try:
        await asyncio.sleep(0)
        coordinator.transport.async_get.assert_not_awaited()
    finally:
        post_release.set()
        await asyncio.gather(command, poll)


async def test_related_paths_are_refreshed_before_command_publication(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport.async_get.reset_mock()
    await coordinator.async_command(CommandKind.HVAC_MODE, HvacMode.COOL)
    assert [
        call.args[0] for call in coordinator.transport.async_get.await_args_list
    ] == [
        HVAC_MODE_PATH,
        TEMPERATURE_PATH,
        "/wind/strength/vs/0",
        "/wind/direction/vs/0",
        "/mode/convenient/vs/0",
    ]
    assert coordinator.data.update_source is UpdateSource.COMMAND


async def test_off_to_mode_is_one_serial_mode_then_power_operation(
    coordinator: WindFreeCoordinator,
) -> None:
    post_times: list[tuple[str, float]] = []
    original_post = coordinator.transport_factory._post

    async def post(path: str, payload: object) -> None:
        post_times.append((path, coordinator._monotonic()))
        await original_post(path, payload)

    coordinator.transport.async_post.side_effect = post
    coordinator.transport.async_post.reset_mock()
    await coordinator.async_set_hvac_mode(HvacMode.HEAT)
    assert [
        call.args[0] for call in coordinator.transport.async_post.await_args_list
    ] == [HVAC_MODE_PATH, POWER_PATH]
    assert post_times[1][1] - post_times[0][1] == 2.0
    assert coordinator.data.climate.power
    assert coordinator.data.climate.mode is HvacMode.HEAT


async def test_cancelled_off_to_mode_retains_cooldown_before_next_power(
    coordinator: WindFreeCoordinator,
) -> None:
    sleep_started = asyncio.Event()
    sleep_release = asyncio.Event()

    async def blocking_sleep(_delay: float) -> None:
        sleep_started.set()
        await sleep_release.wait()

    coordinator._sleep = blocking_sleep
    coordinator.transport.async_post.reset_mock()
    operation = asyncio.create_task(coordinator.async_set_hvac_mode(HvacMode.HEAT))
    await asyncio.wait_for(sleep_started.wait(), timeout=1)
    assert [
        call.args[0] for call in coordinator.transport.async_post.await_args_list
    ] == [HVAC_MODE_PATH]

    operation.cancel("caller_cancelled")
    with pytest.raises(asyncio.CancelledError, match="caller_cancelled"):
        await operation

    followup_sleeps: list[float] = []
    clock = coordinator._monotonic.__self__

    async def followup_sleep(delay: float) -> None:
        followup_sleeps.append(delay)
        clock.now += delay

    coordinator._sleep = followup_sleep
    coordinator.transport.async_post.reset_mock()
    await coordinator.async_turn_on()

    assert followup_sleeps == [2.0]
    assert [
        call.args[0] for call in coordinator.transport.async_post.await_args_list
    ] == [POWER_PATH]


async def test_mode_settle_rechecks_deadline_after_an_early_wakeup(
    coordinator: WindFreeCoordinator,
) -> None:
    now = 100.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    async def early_once_sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay - 0.25 if len(sleeps) == 1 else delay

    coordinator._monotonic = monotonic
    coordinator._sleep = early_once_sleep
    coordinator._mode_settle_until = 102.0

    await coordinator._async_wait_for_mode_settle()

    assert sleeps == [2.0, 0.25]
    assert now == 102.0


async def test_mode_settle_applies_before_temperature_write(
    coordinator: WindFreeCoordinator,
) -> None:
    post_times: list[tuple[str, float]] = []
    original_post = coordinator.transport_factory._post

    async def post(path: str, payload: object) -> None:
        post_times.append((path, coordinator._monotonic()))
        await original_post(path, payload)

    coordinator.transport.async_post.side_effect = post

    await coordinator.async_command(CommandKind.HVAC_MODE, HvacMode.HEAT)
    await coordinator.async_command(CommandKind.TEMPERATURE, 27.0)

    assert [path for path, _time in post_times] == [
        HVAC_MODE_PATH,
        TEMPERATURE_PATH,
    ]
    assert post_times[1][1] - post_times[0][1] == 2.0


async def test_mode_settle_restarts_after_mode_verification(
    coordinator: WindFreeCoordinator,
) -> None:
    post_times: list[tuple[str, float]] = []
    original_get = coordinator.transport_factory._get
    original_post = coordinator.transport_factory._post
    mode_posted = False
    verification_delayed = False

    async def get(path: str) -> dict[str, object]:
        nonlocal verification_delayed
        if path == HVAC_MODE_PATH and mode_posted and not verification_delayed:
            verification_delayed = True
            await coordinator._sleep(1.0)
        return await original_get(path)

    async def post(path: str, payload: object) -> None:
        nonlocal mode_posted
        post_times.append((path, coordinator._monotonic()))
        await original_post(path, payload)
        if path == HVAC_MODE_PATH:
            mode_posted = True

    coordinator.transport.async_get.side_effect = get
    coordinator.transport.async_post.side_effect = post

    await coordinator.async_command(CommandKind.HVAC_MODE, HvacMode.HEAT)
    await coordinator.async_command(CommandKind.TEMPERATURE, 27.0)

    assert verification_delayed
    assert [path for path, _time in post_times] == [
        HVAC_MODE_PATH,
        TEMPERATURE_PATH,
    ]
    assert post_times[1][1] - post_times[0][1] == 3.0


async def test_power_failure_retains_verified_remembered_mode(
    coordinator: WindFreeCoordinator,
) -> None:
    calls = 0

    async def post(path: str, payload: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError
        await coordinator.transport_factory._post(path, payload)

    coordinator.transport.async_post.side_effect = post
    with pytest.raises(CommandRejected, match="command_failed"):
        await coordinator.async_set_hvac_mode(HvacMode.HEAT)
    assert not coordinator.data.climate.power
    assert coordinator.data.climate.mode is HvacMode.HEAT


async def test_power_on_mode_coercion_refreshes_and_rejects_logical_operation(
    coordinator: WindFreeCoordinator,
) -> None:
    original_post = coordinator.transport_factory._post

    async def post(path: str, payload: object) -> None:
        await original_post(path, payload)
        if path == POWER_PATH:
            coerced = {"x.com.samsung.da.modes": ["Cool"]}
            coordinator.transport_factory.resources[HVAC_MODE_PATH] = coerced
            coordinator.transport_factory.resources["/device/0"][HVAC_MODE_PATH] = (
                coerced
            )

    coordinator.transport.async_post.side_effect = post
    with pytest.raises(CommandRejected):
        await coordinator.async_set_hvac_mode(HvacMode.HEAT)
    assert coordinator.data.climate.power
    assert coordinator.data.climate.mode is HvacMode.COOL


async def test_turn_off_changes_power_only_and_turn_on_uses_remembered_mode(
    coordinator: WindFreeCoordinator,
) -> None:
    await coordinator.async_set_hvac_mode(HvacMode.HEAT)
    coordinator.transport.async_post.reset_mock()
    await coordinator.async_turn_off()
    assert [
        call.args[0] for call in coordinator.transport.async_post.await_args_list
    ] == [POWER_PATH]
    coordinator.transport.async_post.reset_mock()
    await coordinator.async_turn_on()
    assert [
        call.args[0] for call in coordinator.transport.async_post.await_args_list
    ] == [POWER_PATH]
    assert coordinator.data.climate.mode is HvacMode.HEAT


async def test_mode_incompatible_command_is_rejected_without_io(
    coordinator: WindFreeCoordinator,
) -> None:
    await coordinator.async_set_hvac_mode(HvacMode.AUTO)
    coordinator.transport.reset_mock()
    with pytest.raises(CommandRejected):
        await coordinator.async_command(CommandKind.TEMPERATURE, 27)
    coordinator.transport.async_get.assert_not_awaited()
    coordinator.transport.async_post.assert_not_awaited()


async def test_preset_values_are_mode_specific_and_fail_before_io(
    coordinator: WindFreeCoordinator,
) -> None:
    with pytest.raises(CommandRejected, match="command_incompatible"):
        await coordinator.async_command(CommandKind.PRESET, PresetMode.DRY_COMFORT)
    coordinator.transport.async_post.assert_not_awaited()

    await coordinator.async_set_hvac_mode(HvacMode.DRY)
    coordinator.transport.reset_mock()
    await coordinator.async_command(CommandKind.PRESET, PresetMode.DRY_COMFORT)
    coordinator.transport.async_post.assert_awaited_once()


async def test_auto_clean_is_rejected_without_io_while_power_is_off(
    coordinator: WindFreeCoordinator,
) -> None:
    assert not coordinator.data.climate.power

    with pytest.raises(CommandRejected, match="command_incompatible"):
        await coordinator.async_command(CommandKind.AUTO_CLEAN, False)

    coordinator.transport.async_post.assert_not_awaited()


async def test_identity_drift_disables_all_writes(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport_factory.resources["/oic/p"]["mnos"] = "TizenRT 3.0"
    await coordinator.async_reconcile()
    assert coordinator.identity_drift
    assert coordinator.health.unsupported_identity_after_update
    assert not coordinator.health.resource_contract_changed
    with pytest.raises(CommandRejected):
        await coordinator.async_command(CommandKind.POWER, True)


async def test_resource_drift_disables_only_affected_path(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport_factory.resources["/device/0"][POWER_PATH] = {}
    await coordinator.async_reconcile()
    assert POWER_PATH in coordinator.disabled_write_paths
    assert coordinator.health.resource_contract_changed
    assert not coordinator.health.unsupported_identity_after_update
    with pytest.raises(CommandRejected):
        await coordinator.async_command(CommandKind.POWER, True)


async def test_shutdown_closes_exact_generation_rejects_commands_and_no_resurrection(
    coordinator: WindFreeCoordinator,
) -> None:
    old_transport = coordinator.transport
    old_generation = coordinator.generation
    await coordinator.async_shutdown()
    old_transport.async_close.assert_awaited_once()
    with pytest.raises(CommandRejected):
        await coordinator.async_command(CommandKind.POWER, True)
    await coordinator.async_run_reconnect_attempt()
    assert coordinator.generation == old_generation


async def test_completed_shutdown_is_terminal_and_start_cannot_resurrect(
    coordinator: WindFreeCoordinator,
) -> None:
    generation = coordinator.generation
    transports = len(coordinator.transport_factory.transports)
    await coordinator.async_shutdown()

    with pytest.raises(RuntimeError, match="coordinator_shutdown") as caught:
        await coordinator.async_start()

    assert caught.value.__cause__ is None
    assert coordinator.generation == generation
    assert len(coordinator.transport_factory.transports) == transports
    await coordinator.async_shutdown()
    assert not coordinator.data.available


async def test_command_cancellation_propagates_without_speculative_publication(
    coordinator: WindFreeCoordinator,
) -> None:
    before = coordinator.data
    coordinator.transport.async_post.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await coordinator.async_command(CommandKind.POWER, True)
    assert coordinator.data is before


async def test_repeated_caller_cancellation_retains_command_until_shutdown_join(
    coordinator: WindFreeCoordinator,
) -> None:
    post_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    publications: list[bool] = []

    async def post(_path: str, _payload: object) -> None:
        post_started.set()
        while not cleanup_release.is_set():
            try:
                await cleanup_release.wait()
            except asyncio.CancelledError:
                cleanup_started.set()

    coordinator.transport.async_post.side_effect = post
    coordinator.async_add_listener(
        lambda: publications.append(coordinator.data.available)
    )
    caller = asyncio.create_task(coordinator.async_command(CommandKind.POWER, True))
    await post_started.wait()
    caller.cancel("first-command-cancel")
    await cleanup_started.wait()
    caller.cancel("second-command-cancel")
    try:
        with pytest.raises(asyncio.CancelledError) as caught:
            await caller
        assert caught.value.args == ("second-command-cancel",)

        assert coordinator._command_tasks
        shutdown = asyncio.create_task(coordinator.async_shutdown())
        await asyncio.sleep(0)
        assert not shutdown.done()
        cleanup_release.set()
        await shutdown

        assert not coordinator._command_tasks
        assert not coordinator._command_completions
        assert not coordinator.data.available
        assert True not in publications
    finally:
        cleanup_release.set()


@pytest.mark.parametrize("operation_name", ["simple", "off_to_mode", "turn_on"])
@pytest.mark.parametrize("blocked_stage", ["post", "observe"])
async def test_shutdown_joins_every_inflight_command_without_resurrection(
    coordinator: WindFreeCoordinator,
    monkeypatch: pytest.MonkeyPatch,
    operation_name: str,
    blocked_stage: str,
) -> None:
    blocked = asyncio.Event()
    never = asyncio.Event()
    publications: list[bool] = []
    coordinator.async_add_listener(
        lambda: publications.append(coordinator.data.available)
    )

    if blocked_stage == "post":

        async def post(_path: str, _payload: object) -> None:
            blocked.set()
            await never.wait()

        coordinator.transport.async_post.side_effect = post
    else:
        coordinator.transport.async_post.side_effect = (
            coordinator.transport_factory._post
        )

        async def wait_for_observe(*_args: object) -> bool:
            blocked.set()
            await never.wait()
            return False

        monkeypatch.setattr(coordinator, "_wait_for_observe", wait_for_observe)

    if operation_name == "simple":
        operation = asyncio.create_task(
            coordinator.async_command(CommandKind.POWER, True)
        )
    elif operation_name == "off_to_mode":
        operation = asyncio.create_task(coordinator.async_set_hvac_mode(HvacMode.HEAT))
    else:
        operation = asyncio.create_task(coordinator.async_turn_on())

    try:
        await blocked.wait()
        await coordinator.async_shutdown()

        assert operation.done()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert not coordinator.data.available
        assert True not in publications
        assert not coordinator._command_tasks
        assert not coordinator._command_completions
        await asyncio.sleep(0)
        assert not coordinator.data.available
    finally:
        never.set()
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize(
    "operation_name",
    ["poll", "reconcile", "hot", "scheduled"],
)
async def test_shutdown_joins_every_inflight_public_io_without_resurrection(
    coordinator: WindFreeCoordinator,
    operation_name: str,
) -> None:
    blocked = asyncio.Event()
    never = asyncio.Event()
    publications: list[bool] = []
    original_get = coordinator.transport_factory._get

    async def get(path: str) -> dict[str, object]:
        blocked.set()
        await never.wait()
        return await original_get(path)

    coordinator.transport.async_get.side_effect = get
    coordinator.async_add_listener(
        lambda: publications.append(coordinator.data.available)
    )
    if operation_name == "poll":
        operation = asyncio.create_task(coordinator.async_poll_path(HOT_PATHS[0]))
    elif operation_name == "reconcile":
        operation = asyncio.create_task(coordinator.async_reconcile())
    elif operation_name == "hot":
        operation = asyncio.create_task(coordinator.async_run_hot_cycle())
    else:
        coordinator.force_due(HOT_PATHS[0], due=0)
        operation = asyncio.create_task(coordinator.async_run_scheduled_once())

    try:
        await blocked.wait()
        coordinator.handle_observe(
            coordinator.generation,
            POWER_PATH,
            {"x.com.samsung.da.power": "On"},
        )
        assert coordinator.data.climate.power
        await coordinator.async_shutdown()
        resources = dict(coordinator._resources)

        assert operation.done()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert not coordinator.data.available
        assert publications[-1] is False
        assert not coordinator._operation_tasks
        assert not coordinator._operation_completions
        never.set()
        await asyncio.sleep(0)
        assert coordinator._resources == resources
        assert not coordinator.data.available
        assert coordinator.last_update_success
    finally:
        never.set()
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


async def test_shutdown_joins_blocked_public_reconnect_attempt(
    coordinator: WindFreeCoordinator,
) -> None:
    reconnect_started = asyncio.Event()
    never = asyncio.Event()

    async def reconnect(**_kwargs: object) -> AsyncMock:
        reconnect_started.set()
        await never.wait()
        raise AssertionError("unreachable")

    coordinator.transport_factory.reconnect.side_effect = reconnect
    operation = asyncio.create_task(coordinator.async_run_reconnect_attempt())
    try:
        await reconnect_started.wait()
        await coordinator.async_shutdown()

        assert operation.done()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert len(coordinator.transport_factory.transports) == 1
        assert not coordinator._operation_tasks
        assert not coordinator._operation_completions
        assert not coordinator.data.available
    finally:
        never.set()
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


async def test_registered_child_can_request_shutdown_without_join_cycle(
    coordinator: WindFreeCoordinator,
) -> None:
    original_get = coordinator.transport_factory._get
    publications: list[bool] = []
    shutdown_returned = asyncio.Event()

    async def get(path: str) -> dict[str, object]:
        await coordinator.async_shutdown()
        shutdown_returned.set()
        return await original_get(path)

    coordinator.transport.async_get.side_effect = get
    coordinator.async_add_listener(
        lambda: publications.append(coordinator.data.available)
    )
    operation = asyncio.create_task(coordinator.async_poll_path(HOT_PATHS[0]))

    try:
        await asyncio.wait_for(shutdown_returned.wait(), timeout=0.2)
    finally:
        operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await asyncio.wait_for(coordinator.async_shutdown(), timeout=0.2)

    assert not coordinator._operation_tasks
    assert not coordinator._operation_completions
    assert not coordinator.data.available
    assert coordinator.last_update_success
    assert True not in publications
    await coordinator.async_shutdown()


async def test_start_observe_failure_never_publishes_available(
    hass,
    credentials,
    resource_representations,
    compatibility,
) -> None:
    factory = FakeTransportFactory(resource_representations)
    factory.observe_error = TransportError("transport_observe_failed")
    instance = WindFreeCoordinator(
        hass,
        host="device.invalid",
        port=49154,
        credentials=credentials,
        compatibility=compatibility,
        transport_factory=factory,
        start_scheduler=False,
    )
    publications: list[bool] = []
    instance.async_add_listener(lambda: publications.append(instance.data.available))

    with pytest.raises(TransportError):
        await instance.async_start()

    assert True not in publications
    assert not instance.data.available
    assert not instance.last_update_success
    factory.current.async_close.assert_awaited_once()


async def test_reconnect_seed_is_not_exposed_and_blocks_commands(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    old_transport = coordinator.transport
    old_generation = coordinator.generation
    factory.block_generation = old_generation + 1
    reconnect = asyncio.create_task(coordinator.async_run_reconnect_attempt())
    await factory.seed_started.wait()

    assert coordinator.transport is old_transport
    assert coordinator.generation == old_generation
    command = asyncio.create_task(coordinator.async_command(CommandKind.POWER, True))
    await asyncio.sleep(0)
    coordinator.transport.async_post.assert_not_awaited()

    factory.seed_release.set()
    await reconnect
    await command
    assert coordinator.generation == old_generation + 1


async def test_reconnect_observe_failure_keeps_unavailable_and_retryable(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    coordinator._publish_unavailable("connection_failed")
    factory.observe_error = TransportError("transport_observe_failed")

    await coordinator.async_run_reconnect_attempt()

    assert not coordinator.data.available
    assert not coordinator.last_update_success
    assert factory.current.async_close.await_count == 1
    assert coordinator.reconnect_delay == 2


async def test_off_to_mode_has_no_intermediate_available_publication(
    coordinator: WindFreeCoordinator,
) -> None:
    power_started = asyncio.Event()
    power_release = asyncio.Event()
    original_post = coordinator.transport_factory._post

    async def post(path: str, payload: object) -> None:
        await original_post(path, payload)
        coordinator.handle_observe(coordinator.generation, path, payload)
        if path == POWER_PATH:
            power_started.set()
            await power_release.wait()

    snapshots = []
    coordinator.async_add_listener(lambda: snapshots.append(coordinator.data))
    coordinator.transport.async_post.side_effect = post
    operation = asyncio.create_task(coordinator.async_set_hvac_mode(HvacMode.HEAT))
    await power_started.wait()
    assert snapshots == []
    power_release.set()
    await operation
    assert len(snapshots) == 1
    assert snapshots[0].climate.power
    assert snapshots[0].climate.mode is HvacMode.HEAT


async def test_turn_on_rejects_and_publishes_authoritative_mode_coercion(
    coordinator: WindFreeCoordinator,
) -> None:
    await coordinator.async_command(CommandKind.HVAC_MODE, HvacMode.HEAT)
    original_post = coordinator.transport_factory._post

    async def post(path: str, payload: object) -> None:
        await original_post(path, payload)
        if path == POWER_PATH:
            coerced = {"x.com.samsung.da.modes": ["Cool"]}
            coordinator.transport_factory.resources[HVAC_MODE_PATH] = coerced

    coordinator.transport.async_post.side_effect = post
    with pytest.raises(CommandRejected):
        await coordinator.async_turn_on()
    assert coordinator.data.climate.power
    assert coordinator.data.climate.mode is HvacMode.COOL


async def test_clean_shutdown_publishes_lifecycle_unavailable_without_logs(
    coordinator: WindFreeCoordinator,
    caplog,
) -> None:
    assert coordinator.last_update_success
    caplog.clear()

    with caplog.at_level(
        logging.INFO,
        logger="custom_components.samsung_ac_windfree.coordinator",
    ):
        await coordinator.async_shutdown()
        coordinator.handle_observe(
            coordinator.generation,
            POWER_PATH,
            {"x.com.samsung.da.power": "On"},
        )
        await coordinator.async_shutdown()

    assert coordinator.last_update_success
    assert not coordinator.data.available
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert not any(
        record.message == "Samsung WindFree connection recovered"
        for record in caplog.records
    )


def test_unavailable_recovery_logging_is_once_per_transition_and_private(
    coordinator: WindFreeCoordinator,
    caplog,
) -> None:
    host = "device.invalid"
    recovery_message = "Samsung WindFree connection recovered"
    caplog.clear()

    with caplog.at_level(
        logging.INFO,
        logger="custom_components.samsung_ac_windfree.coordinator",
    ):
        for _ in range(2):
            coordinator._publish_unavailable("connection_failed")
        coordinator._publish(replace(coordinator.data, available=True))
        coordinator._publish(coordinator.data)
        coordinator._publish_unavailable("connection_failed")
        coordinator._publish(replace(coordinator.data, available=True))

    recoveries = [
        record for record in caplog.records if record.message == recovery_message
    ]
    failures = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and record.message
        == "Error requesting Samsung WindFree data: connection_failed"
    ]
    assert len(recoveries) == 2
    assert len(failures) == 2
    assert all(record.levelno == logging.INFO for record in recoveries)
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert host not in rendered
    assert coordinator._credentials.client_key_pem not in rendered
    assert coordinator.data.identity.device_id not in rendered


async def test_scheduled_hot_failures_advance_deadline_and_trigger_supervision(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    factory.reconnect.side_effect = ConnectionError
    factory.discover.side_effect = ConnectionError
    coordinator.transport.async_get.side_effect = TimeoutError
    for _ in range(3):
        path = coordinator.next_due().path
        coordinator.force_due(path, due=0)
        before = coordinator.deadline_for(path)
        await coordinator.async_run_scheduled_once()
        assert coordinator.deadline_for(path) > before
    assert not coordinator.data.available
    assert coordinator.transport_factory.reconnect.await_count >= 1
    reconnect = coordinator._reconnect_task
    assert reconnect is not None
    for _ in range(100):
        coordinator._ensure_reconnect_task()
    assert coordinator._reconnect_task is reconnect
    assert (
        len(
            [
                task
                for task in asyncio.all_tasks()
                if task.get_name() == "windfree reconnect" and not task.done()
            ]
        )
        == 1
    )


async def test_scheduled_failure_does_not_starve_warm_or_reconcile(
    coordinator: WindFreeCoordinator,
) -> None:
    failing_hot = HOT_PATHS[0]
    original_get = coordinator.transport_factory._get

    async def get(path: str) -> dict[str, object]:
        if path == failing_hot:
            raise TimeoutError
        return await original_get(path)

    coordinator.transport_factory._get = get
    coordinator.force_due(failing_hot, due=0)
    coordinator.force_due(WARM_PATHS[0], due=0)
    await coordinator.async_run_scheduled_once()
    await coordinator.async_run_scheduled_once()
    assert WARM_PATHS[0] in [
        call.args[0] for call in coordinator.transport.async_get.await_args_list
    ]
    coordinator.force_reconcile_due(0)
    await coordinator.async_run_scheduled_once()
    assert coordinator.reconcile_deadline > 0


async def test_background_fatal_auth_confirmation_is_exposed(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport.async_get.side_effect = TransportError(
        "transport_get_failed", fatal_alert="unknown_ca"
    )
    coordinator.force_due(HOT_PATHS[0], due=0)
    await coordinator.async_run_scheduled_once()
    for _ in range(20):
        await asyncio.sleep(0)
        if coordinator.generation == 2 and coordinator.data.available:
            break
    coordinator.transport.async_get.side_effect = TransportError(
        "transport_get_failed", fatal_alert="unknown_ca"
    )
    coordinator.force_due(HOT_PATHS[0], due=0)
    await coordinator.async_run_scheduled_once()
    assert coordinator.authentication_rejected
    assert coordinator.connection_reason == "authentication_rejected"
    assert not coordinator.last_update_success


async def test_transient_scheduled_failure_never_sets_authentication_rejected(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport.async_get.side_effect = TimeoutError
    coordinator.force_due(HOT_PATHS[0], due=0)
    await coordinator.async_run_scheduled_once()
    coordinator._generation += 1
    coordinator.force_due(HOT_PATHS[0], due=0)
    await coordinator.async_run_scheduled_once()
    assert not coordinator.authentication_rejected


@pytest.mark.parametrize(
    "error",
    [
        TransportError(
            "transport_connect_failed",
            coap_code=129,
        ),
        TransportError(
            "transport_connect_failed",
            fatal_alert="handshake_failure",
        ),
        TimeoutError(),
    ],
)
async def test_unallowed_reconnect_signals_remain_transient(
    coordinator: WindFreeCoordinator,
    error: BaseException,
) -> None:
    coordinator.transport_factory.reconnect.side_effect = [error, error]
    await coordinator.async_run_reconnect_attempt()
    await coordinator.async_run_reconnect_attempt()
    assert not coordinator.authentication_rejected
    assert coordinator.connection_reason != "authentication_rejected"


async def test_shutdown_cancellation_is_retained_idempotent_and_exact(
    coordinator: WindFreeCoordinator,
) -> None:
    close_started = asyncio.Event()
    close_release = asyncio.Event()

    async def close() -> None:
        close_started.set()
        await close_release.wait()

    coordinator.transport.async_close.side_effect = close
    shutdown = asyncio.create_task(coordinator.async_shutdown())
    await close_started.wait()
    shutdown.cancel("exact-shutdown-cancel")
    await asyncio.sleep(0)
    close_release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await shutdown
    assert caught.value.args == ("exact-shutdown-cancel",)
    await coordinator.async_shutdown()
    assert coordinator.transport_factory.current.async_close.await_count == 1


async def test_shutdown_close_failure_retains_transport_for_retry(
    coordinator: WindFreeCoordinator,
) -> None:
    active = coordinator.transport
    active.async_close.side_effect = [
        TransportError("transport_close_failed"),
        None,
    ]
    with pytest.raises(TransportError):
        await coordinator.async_shutdown()
    await coordinator.async_shutdown()
    assert active.async_close.await_count == 2
    assert not coordinator.data.available
    assert coordinator.last_update_success


async def test_shutdown_close_cancellation_retains_transport_for_retry(
    coordinator: WindFreeCoordinator,
) -> None:
    active = coordinator.transport
    active.async_close.side_effect = [
        asyncio.CancelledError("transport-close-cancelled"),
        None,
    ]
    with pytest.raises(asyncio.CancelledError):
        await coordinator.async_shutdown()

    await coordinator.async_shutdown()

    assert active.async_close.await_count == 2
    assert not coordinator.data.available
    assert coordinator.last_update_success


async def test_shutdown_retries_only_the_exact_transport_that_failed_to_close(
    coordinator: WindFreeCoordinator,
) -> None:
    active = coordinator.transport
    pending = AsyncMock()
    pending.async_close.side_effect = [
        TransportError("transport_close_failed"),
        None,
    ]
    coordinator._pending_transport = pending

    with pytest.raises(TransportError):
        await coordinator.async_shutdown()
    await coordinator.async_shutdown()

    active.async_close.assert_awaited_once()
    assert pending.async_close.await_count == 2
    assert coordinator._transport is None
    assert coordinator._pending_transport is None


@pytest.mark.parametrize(
    "close_error",
    [
        TransportError("transport_close_failed"),
        asyncio.CancelledError("transport_close_cancelled"),
    ],
)
async def test_disconnect_retains_failed_close_and_still_schedules_reconnect(
    coordinator: WindFreeCoordinator,
    close_error: BaseException,
) -> None:
    active = coordinator.transport
    active.async_close.side_effect = [close_error, None]
    coordinator.transport_factory.reconnect.side_effect = ConnectionError
    coordinator.transport_factory.discover.side_effect = ConnectionError
    coordinator.transport.async_get.side_effect = TimeoutError

    for path in HOT_PATHS[:3]:
        coordinator.force_due(path, due=0)
        await coordinator.async_run_scheduled_once()

    assert not coordinator.data.available
    for _ in range(20):
        await asyncio.sleep(0)
        if active.async_close.await_count == 2:
            break
    assert active.async_close.await_count == 2
    assert coordinator._reconnect_task is not None
    await coordinator.async_shutdown()
    assert active.async_close.await_count == 2


@pytest.mark.parametrize(
    "close_error",
    [
        TransportError("transport_close_failed"),
        asyncio.CancelledError("transport_close_cancelled"),
    ],
)
async def test_startup_retains_candidate_when_failure_cleanup_cannot_close(
    hass,
    credentials,
    resource_representations,
    compatibility,
    close_error: BaseException,
) -> None:
    factory = FakeTransportFactory(resource_representations)
    factory.observe_error = TransportError("transport_observe_failed")
    factory.next_close_side_effect = [close_error, None]
    instance = WindFreeCoordinator(
        hass,
        host="device.invalid",
        port=49154,
        credentials=credentials,
        compatibility=compatibility,
        transport_factory=factory,
        start_scheduler=False,
    )

    with pytest.raises(TransportError, match="transport_observe_failed"):
        await instance.async_start()

    candidate = factory.current
    assert candidate.async_close.await_count == 1
    await instance.async_shutdown()
    assert candidate.async_close.await_count == 2


async def test_repeated_startup_failures_retain_every_unclosed_candidate(
    hass,
    credentials,
    resource_representations,
    compatibility,
) -> None:
    factory = FakeTransportFactory(resource_representations)
    factory.observe_error = TransportError("transport_observe_failed")
    instance = WindFreeCoordinator(
        hass,
        host="device.invalid",
        port=49154,
        credentials=credentials,
        compatibility=compatibility,
        transport_factory=factory,
        start_scheduler=False,
    )
    candidates = []
    for _ in range(2):
        factory.next_close_side_effect = [
            TransportError("transport_close_failed"),
            None,
        ]
        with pytest.raises(TransportError, match="transport_observe_failed"):
            await instance.async_start()
        candidates.append(factory.current)

    await instance.async_shutdown()
    assert [candidate.async_close.await_count for candidate in candidates] == [2, 2]


@pytest.mark.parametrize(
    "close_error",
    [
        TransportError("transport_close_failed"),
        asyncio.CancelledError("transport_close_cancelled"),
    ],
)
async def test_reconnect_retains_candidate_when_failure_cleanup_cannot_close(
    coordinator: WindFreeCoordinator,
    close_error: BaseException,
) -> None:
    factory = coordinator.transport_factory
    factory.observe_error = TransportError("transport_observe_failed")
    factory.next_close_side_effect = [close_error, None]

    await coordinator.async_run_reconnect_attempt()

    candidate = factory.current
    assert candidate is not coordinator.transport
    assert candidate.async_close.await_count == 1
    await coordinator.async_shutdown()
    assert candidate.async_close.await_count == 2


@pytest.mark.parametrize(
    "close_error",
    [
        TransportError("transport_close_failed"),
        asyncio.CancelledError("transport_close_cancelled"),
    ],
)
async def test_activation_never_exposes_candidate_before_old_close_succeeds(
    coordinator: WindFreeCoordinator,
    close_error: BaseException,
) -> None:
    old = coordinator.transport
    generation = coordinator.generation
    old.async_close.side_effect = [close_error, None]

    await coordinator.async_run_reconnect_attempt()

    candidate = coordinator.transport_factory.current
    assert candidate is not old
    with pytest.raises(RuntimeError, match="coordinator_not_started"):
        _ = coordinator.transport
    assert coordinator.generation == generation
    assert coordinator.data.generation == generation
    candidate.async_close.assert_awaited_once()
    await coordinator.async_shutdown()
    assert old.async_close.await_count == 2


async def test_runtime_close_backpressure_bounds_failed_generation_ownership(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    unclosed = coordinator.transport
    unclosed.async_close.side_effect = TransportError("transport_close_failed")
    await coordinator._async_disconnect_locked("connection_failed")

    try:
        for _ in range(2000):
            await coordinator.async_run_reconnect_attempt()

        assert len(factory.transports) == 1
        assert coordinator._pending_transport is None
        assert coordinator._retired_transports == [unclosed]
        assert len(coordinator._operation_tasks) <= 1
        assert len(coordinator._operation_completions) <= 1

        unclosed.async_close.side_effect = None
        await coordinator.async_run_reconnect_attempt()
        assert coordinator.generation == 2
        assert coordinator.data.available
        assert not coordinator._retired_transports
    finally:
        unclosed.async_close.side_effect = None


async def test_concurrent_reconnects_cannot_bypass_close_backpressure(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    old = coordinator.transport
    old.async_close.side_effect = TransportError("transport_close_failed")
    reconnect_started = asyncio.Event()
    reconnect_release = asyncio.Event()

    async def reconnect(*, generation: int, **_: object) -> AsyncMock:
        reconnect_started.set()
        await reconnect_release.wait()
        return factory.create(generation=generation)

    factory.reconnect.side_effect = reconnect
    attempts = [
        asyncio.create_task(coordinator.async_run_reconnect_attempt())
        for _ in range(100)
    ]
    try:
        await reconnect_started.wait()
        await asyncio.sleep(0)
        assert factory.reconnect.await_count == 1
        reconnect_release.set()
        await asyncio.gather(*attempts)
        assert len(factory.transports) == 2
        assert coordinator._retired_transports == [old]
        assert coordinator._pending_transport is None
        assert not coordinator._operation_tasks
    finally:
        reconnect_release.set()
        await asyncio.gather(*attempts, return_exceptions=True)
        old.async_close.side_effect = None


async def test_priority_handoff_survives_cancel_after_grant(
    coordinator: WindFreeCoordinator,
) -> None:
    admission = coordinator._admission
    await admission.acquire(0)
    first_entered = asyncio.Event()
    successor_entered = asyncio.Event()

    async def waiter(entered: asyncio.Event) -> None:
        async with admission.hold(0):
            entered.set()

    first = asyncio.create_task(waiter(first_entered))
    successor = asyncio.create_task(waiter(successor_entered))
    try:
        await asyncio.sleep(0)
        admission.release()
        first.cancel("cancel-after-grant")

        with pytest.raises(asyncio.CancelledError) as caught:
            await first
        assert caught.value.args == ("cancel-after-grant",)
        await asyncio.wait_for(successor_entered.wait(), timeout=0.1)
        await successor
        assert not admission._held
        assert not admission._waiters
    finally:
        for task in (first, successor):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, successor, return_exceptions=True)


async def test_priority_admission_reorders_waiters_command_hot_then_reconcile(
    coordinator: WindFreeCoordinator,
) -> None:
    order: list[str] = []
    original_get = coordinator.transport_factory._get
    original_post = coordinator.transport_factory._post

    async def get(path: str) -> dict[str, object]:
        order.append(f"get:{path}")
        return await original_get(path)

    async def post(path: str, payload: object) -> None:
        order.append(f"post:{path}")
        await original_post(path, payload)

    coordinator.transport_factory._get = get
    coordinator.transport.async_post.side_effect = post
    await coordinator._admission.acquire(0)
    reconcile = asyncio.create_task(coordinator.async_reconcile())
    await asyncio.sleep(0)
    hot = asyncio.create_task(coordinator.async_poll_path(HOT_PATHS[0]))
    await asyncio.sleep(0)
    command = asyncio.create_task(coordinator.async_command(CommandKind.POWER, True))
    await asyncio.sleep(0)
    coordinator._admission.release()
    await asyncio.gather(reconcile, hot, command)

    assert order[0] == f"post:{POWER_PATH}"
    assert order.index(f"get:{HOT_PATHS[0]}") < order.index("get:/oic/d")


async def test_scheduler_rechecks_deadline_and_priority_after_admission(
    coordinator: WindFreeCoordinator,
) -> None:
    warm = WARM_PATHS[0]
    hot = HOT_PATHS[0]
    coordinator.transport.async_get.reset_mock()
    await coordinator._admission.acquire(0)
    coordinator.force_due(warm, due=0)
    queued_warm = asyncio.create_task(coordinator.async_run_scheduled_once())
    await asyncio.sleep(0)
    coordinator.handle_observe(
        coordinator.generation,
        warm,
        coordinator.transport_factory.resources[warm],
    )
    coordinator.force_due(hot, due=0)
    queued_hot = asyncio.create_task(coordinator.async_run_scheduled_once())
    await asyncio.sleep(0)
    coordinator._admission.release()
    await asyncio.gather(queued_warm, queued_hot)

    calls = [call.args[0] for call in coordinator.transport.async_get.await_args_list]
    assert calls == [hot]


async def test_real_scheduler_processes_failure_warm_and_reconcile(
    hass,
    credentials,
    resource_representations,
    compatibility,
) -> None:
    clock = FakeClock()
    factory = FakeTransportFactory(resource_representations)
    instance = WindFreeCoordinator(
        hass,
        host="device.invalid",
        port=49154,
        credentials=credentials,
        compatibility=compatibility,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        transport_factory=factory,
        observe_wait=0,
        start_scheduler=True,
    )
    await instance.async_start()
    original_get = factory._get

    async def get(path: str) -> dict[str, object]:
        if path == HOT_PATHS[0]:
            raise TimeoutError
        return await original_get(path)

    factory._get = get
    instance.force_due(HOT_PATHS[0], due=0)
    instance.force_due(WARM_PATHS[0], due=0)
    instance.force_reconcile_due(0)
    try:
        for _ in range(20):
            await asyncio.sleep(0)
            called = [
                call.args[0] for call in instance.transport.async_get.await_args_list
            ]
            if WARM_PATHS[0] in called and "/oic/d" in called[3:]:
                break
        assert instance.data.failure_count == 1
        assert WARM_PATHS[0] in called
        assert "/oic/d" in called[3:]
    finally:
        await instance.async_shutdown()


async def test_cancelled_start_cleans_candidate_and_never_resurrects(
    hass,
    credentials,
    resource_representations,
    compatibility,
) -> None:
    factory = FakeTransportFactory(resource_representations)
    factory.block_generation = 1
    instance = WindFreeCoordinator(
        hass,
        host="device.invalid",
        port=49154,
        credentials=credentials,
        compatibility=compatibility,
        transport_factory=factory,
        start_scheduler=False,
    )
    start = asyncio.create_task(instance.async_start())
    await factory.seed_started.wait()
    start.cancel("exact-start-cancel")
    with pytest.raises(asyncio.CancelledError) as caught:
        await start
    assert caught.value.args == ("exact-start-cancel",)
    await instance.async_shutdown()
    factory.seed_release.set()
    await asyncio.sleep(0)
    assert not instance.data.available
    assert factory.current.async_close.await_count == 1


class _SecretCommandValue:
    def __repr__(self) -> str:
        return "adversarial-public-command-secret"


async def test_public_builder_error_scrubs_command_from_all_traceback_frames(
    coordinator: WindFreeCoordinator,
) -> None:
    secret = "adversarial-public-command-secret"
    with pytest.raises(CommandRejected) as caught:
        await coordinator.async_command(
            CommandKind.TEMPERATURE,
            _SecretCommandValue(),
        )
    for frame, _line in traceback.walk_tb(caught.value.__traceback__):
        if frame.f_globals.get("__name__") != (
            "custom_components.samsung_ac_windfree.coordinator"
        ):
            continue
        assert all(secret not in repr(value) for value in frame.f_locals.values())


async def test_aggregate_command_cancellation_is_exact_and_traceback_redacted(
    coordinator: WindFreeCoordinator,
) -> None:
    secret = "adversarial-cancel-aggregate-secret"
    aggregate = copy.deepcopy(coordinator.transport_factory.resources[TEMPERATURE_PATH])
    aggregate["unknown"] = secret
    coordinator.transport_factory.resources[TEMPERATURE_PATH] = aggregate
    post_started = asyncio.Event()

    async def post(_path: str, _payload: object) -> None:
        post_started.set()
        await asyncio.Event().wait()

    coordinator.transport.async_post.side_effect = post
    command = asyncio.create_task(
        coordinator.async_command(CommandKind.TEMPERATURE, 27)
    )
    await post_started.wait()
    command.cancel("exact-command-cancel")
    with pytest.raises(asyncio.CancelledError) as caught:
        await command
    assert caught.value.args == ("exact-command-cancel",)
    for frame, _line in traceback.walk_tb(caught.value.__traceback__):
        if frame.f_globals.get("__name__") != (
            "custom_components.samsung_ac_windfree.coordinator"
        ):
            continue
        assert all(secret not in repr(value) for value in frame.f_locals.values())


async def test_real_scheduler_three_hot_failures_close_and_reconnect(
    hass,
    credentials,
    resource_representations,
    compatibility,
) -> None:
    clock = FakeClock()
    factory = FakeTransportFactory(resource_representations)
    factory.reconnect.side_effect = ConnectionError
    factory.discover.side_effect = ConnectionError
    instance = WindFreeCoordinator(
        hass,
        host="device.invalid",
        port=49154,
        credentials=credentials,
        compatibility=compatibility,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        transport_factory=factory,
        observe_wait=0,
        start_scheduler=True,
    )
    await instance.async_start()
    active = instance.transport

    async def get(path: str) -> dict[str, object]:
        if path in HOT_PATHS:
            raise TimeoutError
        return await factory._get(path)

    factory._get = get
    for path in HOT_PATHS[:3]:
        instance.force_due(path, due=0)
    try:
        for _ in range(30):
            await asyncio.sleep(0)
            if active.async_close.await_count and factory.reconnect.await_count:
                break
        assert not instance.data.available
        assert not instance.last_update_success
        active.async_close.assert_awaited_once()
        assert factory.reconnect.await_count >= 1
    finally:
        await instance.async_shutdown()


async def test_discovery_fatal_auth_confirmation_is_exposed_and_raised(
    coordinator: WindFreeCoordinator,
) -> None:
    factory = coordinator.transport_factory
    factory.reconnect.side_effect = ConnectionError
    factory.discover.side_effect = TransportError(
        "transport_connect_failed",
        fatal_alert="unknown_ca",
    )
    for _ in range(2):
        coordinator._stored_port_failures = 2
        if coordinator.authentication_rejected:
            break
        try:
            await coordinator.async_run_reconnect_attempt()
        except AuthenticationRejected:
            pass
    assert coordinator.authentication_rejected
    assert coordinator.connection_reason == "authentication_rejected"


async def test_reconcile_fatal_auth_confirmation_is_exposed(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport.async_get.side_effect = TransportError(
        "transport_get_failed",
        fatal_alert="access_denied",
    )
    await coordinator.async_reconcile()
    for _ in range(20):
        await asyncio.sleep(0)
        if coordinator.generation == 2 and coordinator.data.available:
            break
    coordinator.transport.async_get.side_effect = TransportError(
        "transport_get_failed",
        fatal_alert="access_denied",
    )
    await coordinator.async_reconcile()
    assert coordinator.authentication_rejected
    assert not coordinator.last_update_success


async def test_command_fatal_auth_confirmation_is_exposed(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport.async_post.side_effect = TransportError(
        "transport_post_rejected",
        coap_code=131,
    )
    with pytest.raises(CommandRejected):
        await coordinator.async_command(CommandKind.POWER, True)
    for _ in range(20):
        await asyncio.sleep(0)
        if coordinator.generation == 2 and coordinator.data.available:
            break
    coordinator.transport.async_post.side_effect = TransportError(
        "transport_post_rejected",
        coap_code=131,
    )
    with pytest.raises(CommandRejected):
        await coordinator.async_command(CommandKind.POWER, True)
    assert coordinator.authentication_rejected
    assert not coordinator.last_update_success


async def test_reconcile_fatal_auth_forces_fresh_generation_before_confirmation(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport.async_get.side_effect = TransportError(
        "transport_get_failed",
        fatal_alert="access_denied",
    )

    await coordinator.async_reconcile()
    for _ in range(20):
        await asyncio.sleep(0)
        if coordinator.generation == 2 and coordinator.data.available:
            break

    assert coordinator.generation == 2
    assert coordinator.data.available
    assert not coordinator.authentication_rejected
    await coordinator.async_poll_path(HOT_PATHS[0])
    coordinator.transport.async_get.side_effect = TransportError(
        "transport_get_failed",
        fatal_alert="access_denied",
    )
    await coordinator.async_reconcile()
    assert coordinator.authentication_rejected
    assert not coordinator.data.available


async def test_command_fatal_auth_forces_fresh_generation_before_confirmation(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport.async_post.side_effect = TransportError(
        "transport_post_rejected",
        coap_code=131,
    )

    with pytest.raises(CommandRejected):
        await coordinator.async_command(CommandKind.POWER, True)
    for _ in range(20):
        await asyncio.sleep(0)
        if coordinator.generation == 2 and coordinator.data.available:
            break

    assert coordinator.generation == 2
    assert coordinator.data.available
    assert not coordinator.authentication_rejected
    await coordinator.async_poll_path(HOT_PATHS[0])
    coordinator.transport.async_post.side_effect = TransportError(
        "transport_post_rejected",
        coap_code=131,
    )
    with pytest.raises(CommandRejected):
        await coordinator.async_command(CommandKind.POWER, True)
    assert coordinator.authentication_rejected
    assert not coordinator.data.available


async def test_command_fatal_is_handled_before_waiting_candidate_activation(
    coordinator: WindFreeCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = coordinator.transport_factory
    old = coordinator.transport
    post_started = asyncio.Event()
    failure_release = asyncio.Event()
    fatal = TransportError("transport_post_rejected", coap_code=131)
    monkeypatch.setattr(coordinator, "_ensure_reconnect_task", lambda: None)

    async def post(_path: str, _payload: object) -> None:
        post_started.set()
        await failure_release.wait()
        raise fatal

    old.async_post.side_effect = post
    command = asyncio.create_task(coordinator.async_command(CommandKind.POWER, True))
    await post_started.wait()
    reconnect = asyncio.create_task(coordinator.async_run_reconnect_attempt())
    await asyncio.sleep(0)
    candidate = factory.current
    assert candidate is not old

    failure_release.set()
    with pytest.raises(CommandRejected):
        await command
    await reconnect

    assert coordinator._fatal_signals[("coap", 131)] == 1
    assert coordinator.generation == 2
    assert coordinator.transport is candidate
    assert coordinator.data.available
    assert not coordinator.authentication_rejected
    candidate.async_close.assert_not_awaited()

    candidate.async_post.side_effect = fatal
    with pytest.raises(CommandRejected):
        await coordinator.async_command(CommandKind.POWER, True)
    assert coordinator.authentication_rejected
    assert not coordinator.data.available
    candidate.async_close.assert_awaited_once()


async def test_nonfatal_reconcile_error_keeps_current_generation_connected(
    coordinator: WindFreeCoordinator,
) -> None:
    active = coordinator.transport
    coordinator.transport.async_get.side_effect = TransportError(
        "transport_get_failed",
        fatal_alert="handshake_failure",
    )
    await coordinator.async_reconcile()
    assert coordinator.transport is active
    assert coordinator.generation == 1
    assert not coordinator.authentication_rejected


@pytest.mark.parametrize("operation_name", ["poll", "reconcile", "hot"])
async def test_queued_public_io_attributes_fatal_to_activated_generation(
    coordinator: WindFreeCoordinator,
    operation_name: str,
) -> None:
    factory = coordinator.transport_factory
    alert = TransportError(
        "transport_get_failed",
        fatal_alert="access_denied",
    )

    async def invoke() -> None:
        if operation_name == "poll":
            await coordinator.async_poll_path(HOT_PATHS[0])
        elif operation_name == "reconcile":
            await coordinator.async_reconcile()
        else:
            await coordinator.async_run_hot_cycle()

    await coordinator._admission.acquire(0)
    reconnect = asyncio.create_task(coordinator.async_run_reconnect_attempt())
    await asyncio.sleep(0)
    candidate = factory.current

    async def observe(*_args: object) -> None:
        candidate.async_get.side_effect = alert

    candidate.async_observe.side_effect = observe
    operation = asyncio.create_task(invoke())
    await asyncio.sleep(0)
    coordinator._admission.release()
    await asyncio.gather(reconnect, operation)

    assert coordinator._fatal_signals[("dtls", "access_denied")] == 2
    for _ in range(30):
        await asyncio.sleep(0)
        if coordinator.generation == 3 and coordinator.data.available:
            break
    assert coordinator.generation == 3
    assert not coordinator.authentication_rejected

    coordinator.transport.async_get.side_effect = alert
    await invoke()
    assert coordinator.authentication_rejected
    assert not coordinator.data.available


@pytest.mark.parametrize("stage", ["post", "wait", "get", "related"])
async def test_every_public_command_failure_scrubs_internal_secrets(
    coordinator: WindFreeCoordinator,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    secret = f"adversarial-{stage}-command-secret"
    original_get = coordinator.transport_factory._get
    coordinator.transport.async_post.side_effect = coordinator.transport_factory._post
    if stage == "post":
        coordinator.transport.async_post.side_effect = RuntimeError(secret)
    elif stage == "wait":
        monkeypatch.setattr(
            coordinator,
            "_wait_for_observe",
            AsyncMock(side_effect=RuntimeError(secret)),
        )
    elif stage == "get":
        coordinator.transport.async_post.side_effect = None
        coordinator.transport.async_post.return_value = None
        coordinator.transport.async_get.side_effect = RuntimeError(secret)
    else:
        calls = 0

        async def get(path: str) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError(secret)
            return await original_get(path)

        coordinator.transport.async_get.side_effect = get

    kind = CommandKind.HVAC_MODE if stage == "related" else CommandKind.POWER
    value = HvacMode.COOL if stage == "related" else True
    with pytest.raises(CommandRejected) as caught:
        await coordinator.async_command(kind, value)

    assert caught.value.__cause__ is None
    assert secret not in repr(caught.value)
    forbidden = (
        secret,
        "device.invalid",
        coordinator._credentials.client_key_pem,
        coordinator._credentials.client_chain_pem,
    )
    for frame, _line in traceback.walk_tb(caught.value.__traceback__):
        if frame.f_globals.get("__name__") != (
            "custom_components.samsung_ac_windfree.coordinator"
        ):
            continue
        rendered = tuple(repr(value) for value in frame.f_locals.values())
        assert all(marker not in item for marker in forbidden for item in rendered)

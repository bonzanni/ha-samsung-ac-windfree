from __future__ import annotations

import asyncio
import copy
import json
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock, call

import pytest

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
        self.reconnect = AsyncMock(side_effect=self._reconnect)
        self.discover = AsyncMock(side_effect=self._discover)

    @property
    def current(self) -> AsyncMock:
        return self.transports[-1]

    def create(self, *, generation: int, **_: object) -> AsyncMock:
        transport = AsyncMock()
        transport.generation = generation
        transport.async_get.side_effect = self._get
        transport.async_post.side_effect = self._post
        self.transports.append(transport)
        return transport

    async def _reconnect(self, *, generation: int, **_: object) -> AsyncMock:
        return self.create(generation=generation)

    async def _discover(self, *, generation: int, **_: object) -> tuple[int, AsyncMock]:
        return 49155, self.create(generation=generation)

    async def _get(self, path: str) -> dict[str, object]:
        return copy.deepcopy(self.resources[path])

    async def _post(self, path: str, payload: object) -> None:
        self.resources[path] = copy.deepcopy(payload)
        self.resources["/device/0"][path] = copy.deepcopy(payload)


@pytest.fixture
def compatibility() -> dict[str, object]:
    return json.loads(
        (Path(__file__).parent / "fixtures" / "mode_compatibility.json").read_text()
    )


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
    assert len(coordinator._heap) > 100
    coordinator.next_due()
    assert len(coordinator._heap) < len(coordinator._deadlines) * 2


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
    coordinator.transport.async_post.reset_mock()
    await coordinator.async_set_hvac_mode(HvacMode.HEAT)
    assert [
        call.args[0] for call in coordinator.transport.async_post.await_args_list
    ] == [HVAC_MODE_PATH, POWER_PATH]
    assert coordinator.data.climate.power
    assert coordinator.data.climate.mode is HvacMode.HEAT


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
    with pytest.raises(TimeoutError):
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


async def test_identity_drift_disables_all_writes(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport_factory.resources["/oic/d"]["mnmo"] = "other"
    await coordinator.async_reconcile()
    assert coordinator.identity_drift
    with pytest.raises(CommandRejected):
        await coordinator.async_command(CommandKind.POWER, True)


async def test_resource_drift_disables_only_affected_path(
    coordinator: WindFreeCoordinator,
) -> None:
    coordinator.transport_factory.resources["/device/0"][POWER_PATH] = {}
    await coordinator.async_reconcile()
    assert POWER_PATH in coordinator.disabled_write_paths
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


async def test_command_cancellation_propagates_without_speculative_publication(
    coordinator: WindFreeCoordinator,
) -> None:
    before = coordinator.data
    coordinator.transport.async_post.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await coordinator.async_command(CommandKind.POWER, True)
    assert coordinator.data is before

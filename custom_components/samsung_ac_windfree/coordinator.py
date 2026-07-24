"""Authoritative local state, scheduling, and session supervision."""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Protocol

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .device import (
    ALARMS_PATH,
    AUTO_CLEAN_PATH,
    CURRENT_LIMIT_PATH,
    DISPLAY_LIGHT_PATH,
    ENERGY_PATH,
    FAN_PATH,
    FILTER_PATH,
    HUMIDITY_PATH,
    HVAC_MODE_PATH,
    POWER_PATH,
    PRESET_PATH,
    SWING_PATH,
    TEMPERATURE_PATH,
    CommandKind,
    DeviceCommand,
    build_command,
    parse_device_state,
    parse_identity,
    validate_contract,
    verify_command,
)
from .models import (
    AuthenticationRejected,
    CapabilityMismatch,
    CommandRejected,
    Credentials,
    HvacMode,
    UnsupportedDevice,
    UpdateSource,
    WindFreeData,
)
from .transport import (
    TransportError,
    WindFreeTransport,
    async_discover_transport,
)

_LOGGER = logging.getLogger(__name__)

HOT_PATHS = (
    POWER_PATH,
    HVAC_MODE_PATH,
    TEMPERATURE_PATH,
    FAN_PATH,
    SWING_PATH,
    PRESET_PATH,
)
WARM_PATHS = (
    HUMIDITY_PATH,
    ENERGY_PATH,
    ALARMS_PATH,
    DISPLAY_LIGHT_PATH,
    AUTO_CLEAN_PATH,
)
COLD_PATHS = (FILTER_PATH, CURRENT_LIMIT_PATH)
RECONCILE_PATHS = ("/oic/d", "/oic/p", "/device/0")

_OBSERVE_PATHS = HOT_PATHS + WARM_PATHS
_ALL_WRITE_PATHS = frozenset(
    {
        POWER_PATH,
        HVAC_MODE_PATH,
        TEMPERATURE_PATH,
        FAN_PATH,
        SWING_PATH,
        PRESET_PATH,
        DISPLAY_LIGHT_PATH,
        AUTO_CLEAN_PATH,
    }
)
_PERIODS = {1: 5.0, 2: 30.0, 3: 300.0}
_RECONCILE_PERIOD = 300.0
_SHORT_GENERATION = 10.0
_FATAL_COAP_CODES = frozenset({129, 131})
_FATAL_ALERTS = frozenset(
    {
        "bad_certificate",
        "unsupported_certificate",
        "certificate_expired",
        "certificate_unknown",
        "unknown_ca",
        "access_denied",
    }
)
_AUTHORIZED_COAP_OPERATIONS = frozenset(
    {
        "get",
        "post",
        "transport_get_rejected",
        "transport_post_rejected",
    }
)


class PollTier(IntEnum):
    """Admission priority after foreground commands."""

    HOT = 1
    WARM = 2
    COLD = 3
    RECONCILE = 4


@dataclass(frozen=True, slots=True)
class ScheduledResource:
    """One current scheduler admission candidate."""

    tier: PollTier
    path: str
    deadline: float


@dataclass(order=True, frozen=True, slots=True)
class _HeapItem:
    deadline: float
    sequence: int
    tier: PollTier
    path: str
    revision: int


@dataclass(frozen=True, slots=True)
class _SeededGeneration:
    transport: WindFreeTransport
    generation: int
    resources: Mapping[str, Mapping[str, object]]
    data: WindFreeData
    authenticated_at: float


@dataclass(frozen=True, slots=True)
class _CommandOutcome:
    error: str | None = None
    cancelled: bool = False


class _PriorityAdmission:
    """Cancellation-safe non-preemptive admission with strict waiter priority."""

    def __init__(self) -> None:
        self._held = False
        self._sequence = 0
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []

    async def acquire(self, priority: int) -> None:
        if not self._held and not self._waiters:
            self._held = True
            return
        self._sequence += 1
        waiter = asyncio.get_running_loop().create_future()
        heapq.heappush(
            self._waiters,
            (priority, self._sequence, waiter),
        )
        try:
            await waiter
        except BaseException:
            waiter.cancel()
            raise

    def release(self) -> None:
        while self._waiters:
            _priority, _sequence, waiter = heapq.heappop(self._waiters)
            if waiter.cancelled():
                continue
            waiter.set_result(None)
            return
        self._held = False

    @asynccontextmanager
    async def hold(self, priority: int):
        await self.acquire(priority)
        try:
            yield
        finally:
            self.release()


class TransportFactory(Protocol):
    """Injectable generation factory used by deterministic tests."""

    def create(
        self,
        *,
        hass: HomeAssistant,
        host: str,
        port: int,
        credentials: Credentials,
        generation: int,
    ) -> WindFreeTransport: ...

    async def reconnect(
        self,
        *,
        hass: HomeAssistant,
        host: str,
        port: int,
        credentials: Credentials,
        generation: int,
    ) -> WindFreeTransport: ...

    async def discover(
        self,
        *,
        hass: HomeAssistant,
        host: str,
        credentials: Credentials,
        generation: int,
    ) -> tuple[int, WindFreeTransport]: ...


class _DefaultTransportFactory:
    def create(
        self,
        *,
        hass: HomeAssistant,
        host: str,
        port: int,
        credentials: Credentials,
        generation: int,
    ) -> WindFreeTransport:
        return WindFreeTransport(
            hass,
            host,
            port,
            credentials,
            generation=generation,
        )

    async def reconnect(
        self,
        *,
        hass: HomeAssistant,
        host: str,
        port: int,
        credentials: Credentials,
        generation: int,
    ) -> WindFreeTransport:
        transport = self.create(
            hass=hass,
            host=host,
            port=port,
            credentials=credentials,
            generation=generation,
        )
        await transport.async_connect()
        return transport

    async def discover(
        self,
        *,
        hass: HomeAssistant,
        host: str,
        credentials: Credentials,
        generation: int,
    ) -> tuple[int, WindFreeTransport]:
        return await async_discover_transport(
            hass,
            host,
            credentials,
            generation=generation,
        )


class WindFreeCoordinator(DataUpdateCoordinator[WindFreeData]):
    """Own exactly one generation and publish immutable typed snapshots."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        host: str,
        port: int,
        credentials: Credentials,
        compatibility: Mapping[str, object],
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport_factory: TransportFactory | None = None,
        observe_wait: float = 0.35,
        start_scheduler: bool = True,
    ) -> None:
        super().__init__(hass, _LOGGER, name="Samsung WindFree")
        self.data = WindFreeData.empty()
        self._host = host
        self._port = port
        self._credentials = credentials
        self._compatibility = compatibility
        self._monotonic = monotonic
        self._sleep = sleep
        self._transport_factory = transport_factory or _DefaultTransportFactory()
        self._observe_wait = observe_wait
        self._start_scheduler = start_scheduler
        self._transport: WindFreeTransport | None = None
        self._generation = 0
        self._generation_attempt = 0
        self._resources: dict[str, Mapping[str, object]] = {}
        self._shutting_down = False
        self._started = False
        self._admission = _PriorityAdmission()
        self._lifecycle_epoch = 0
        self._startup_task: asyncio.Task[None] | None = None
        self._pending_transport: WindFreeTransport | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._observe_events: dict[str, asyncio.Event] = {}
        self._command_active = False
        self._deadlines: dict[str, float] = {}
        self._deadline_revisions: dict[str, int] = {}
        self._last_updates: dict[str, float] = {}
        self._heap: list[_HeapItem] = []
        self._sequence = 0
        self._reconcile_deadline = 0.0
        self._hot_index = 0
        self._hot_failures = 0
        self._stored_port_failures = 0
        self._reconnect_delay = 0
        self._fatal_signals: dict[tuple[str, object], int] = {}
        self._authentication_rejected = False
        self._authenticated_at: float | None = None
        self._short_generations = 0
        self._connection_reason: str | None = None
        self._identity_drift = False
        self._disabled_write_paths: set[str] = set()
        self._latency_buckets = {
            "under_100ms": 0,
            "under_500ms": 0,
            "under_1s": 0,
            "at_least_1s": 0,
        }

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def transport(self) -> WindFreeTransport:
        if self._transport is None:
            raise RuntimeError("coordinator_not_started")
        return self._transport

    @property
    def transport_factory(self) -> TransportFactory:
        return self._transport_factory

    @property
    def reconnect_delay(self) -> int:
        return self._reconnect_delay

    @property
    def connection_reason(self) -> str | None:
        return self._connection_reason

    @property
    def authentication_rejected(self) -> bool:
        return self._authentication_rejected

    @property
    def identity_drift(self) -> bool:
        return self._identity_drift

    @property
    def disabled_write_paths(self) -> frozenset[str]:
        return frozenset(self._disabled_write_paths)

    @property
    def latency_buckets(self) -> Mapping[str, int]:
        return dict(self._latency_buckets)

    @property
    def reconcile_deadline(self) -> float:
        return self._reconcile_deadline

    @property
    def stalest_hot_age(self) -> float:
        now = self._monotonic()
        if not HOT_PATHS:
            return 0.0
        return max(
            max(0.0, now - self._last_updates.get(path, now)) for path in HOT_PATHS
        )

    def _publish(self, data: WindFreeData) -> None:
        self.async_set_updated_data(data)

    def _publish_unavailable(self, reason: str) -> None:
        """Retain typed data while applying coordinator failure semantics."""
        self.data = replace(self.data, available=False)
        self.async_set_update_error(UpdateFailed(reason))

    def _tier_for(self, path: str) -> PollTier:
        if path in HOT_PATHS:
            return PollTier.HOT
        if path in WARM_PATHS:
            return PollTier.WARM
        return PollTier.COLD

    def _schedule(self, path: str, deadline: float) -> None:
        revision = self._deadline_revisions.get(path, 0) + 1
        self._deadline_revisions[path] = revision
        self._deadlines[path] = deadline
        self._sequence += 1
        heapq.heappush(
            self._heap,
            _HeapItem(
                deadline,
                self._sequence,
                self._tier_for(path),
                path,
                revision,
            ),
        )

    def _initialize_deadlines(self) -> None:
        now = self._monotonic()
        all_tiers = (
            (PollTier.HOT, HOT_PATHS, 5.0),
            (PollTier.WARM, WARM_PATHS, 30.0),
            (PollTier.COLD, COLD_PATHS, 300.0),
        )
        for _tier, paths, period in all_tiers:
            for index, path in enumerate(paths, start=1):
                self._last_updates[path] = now
                self._schedule(path, now + period * index / len(paths))
        self._reconcile_deadline = now + _RECONCILE_PERIOD

    def deadline_for(self, path: str) -> float:
        return self._deadlines[path]

    def force_due(self, path: str, *, due: float) -> None:
        self._schedule(path, due)

    def force_reconcile_due(self, due: float) -> None:
        self._reconcile_deadline = due

    def next_due(self) -> ScheduledResource:
        now = self._monotonic()
        due: list[_HeapItem] = []
        while self._heap and self._heap[0].deadline <= now:
            item = heapq.heappop(self._heap)
            if self._deadline_revisions.get(item.path) == item.revision:
                due.append(item)
        if due:
            selected = min(
                due,
                key=lambda item: (item.tier, item.deadline, item.sequence),
            )
            for item in due:
                heapq.heappush(self._heap, item)
            return ScheduledResource(
                selected.tier,
                selected.path,
                selected.deadline,
            )
        while self._heap:
            item = self._heap[0]
            if self._deadline_revisions.get(item.path) == item.revision:
                return ScheduledResource(item.tier, item.path, item.deadline)
            heapq.heappop(self._heap)
        raise RuntimeError("scheduler_has_no_resources")

    async def _async_read_from(
        self,
        transport: WindFreeTransport,
        path: str,
    ) -> Mapping[str, object]:
        started = self._monotonic()
        try:
            return await transport.async_get(path)
        finally:
            latency = max(0.0, self._monotonic() - started)
            if latency < 0.1:
                bucket = "under_100ms"
            elif latency < 0.5:
                bucket = "under_500ms"
            elif latency < 1:
                bucket = "under_1s"
            else:
                bucket = "at_least_1s"
            self._latency_buckets[bucket] += 1

    async def _async_read(self, path: str) -> Mapping[str, object]:
        return await self._async_read_from(self.transport, path)

    def _merge_resource(
        self,
        path: str,
        representation: Mapping[str, object],
        source: UpdateSource,
        *,
        publish: bool = True,
    ) -> None:
        self._resources[path] = representation
        now = self._monotonic()
        self._last_updates[path] = now
        if path in self._deadlines:
            tier = self._tier_for(path)
            self._schedule(path, now + _PERIODS[int(tier)])
        if publish:
            parsed = parse_device_state(self._resources, self.data)
            self._publish(
                replace(
                    parsed,
                    available=True,
                    update_source=source,
                    generation=self._generation,
                    failure_count=self._hot_failures,
                )
            )

    async def _async_seed_generation(
        self,
        transport: WindFreeTransport,
        generation: int,
    ) -> _SeededGeneration:
        identity_payloads = {
            path: await self._async_read_from(transport, path)
            for path in RECONCILE_PATHS
        }
        tree = identity_payloads["/device/0"]
        resources = {
            path: value
            for path, value in tree.items()
            if isinstance(path, str) and isinstance(value, Mapping)
        }
        identity = parse_identity(
            identity_payloads["/oic/d"],
            identity_payloads["/oic/p"],
            tree,
        )
        contract = validate_contract(identity, resources, self._compatibility)
        parsed = parse_device_state(resources, self.data)
        data = replace(
            parsed,
            available=True,
            identity=identity,
            contract=contract,
            update_source=UpdateSource.RECONCILE,
            generation=generation,
            failure_count=0,
        )
        await transport.async_observe(_OBSERVE_PATHS, self.handle_observe)
        authenticated_at = self._monotonic()
        return _SeededGeneration(
            transport=transport,
            generation=generation,
            resources=dict(resources),
            data=data,
            authenticated_at=authenticated_at,
        )

    async def _activate_seeded(
        self,
        seeded: _SeededGeneration,
        *,
        port: int | None = None,
    ) -> None:
        old = self._transport
        self._transport = seeded.transport
        self._generation = seeded.generation
        self._resources = dict(seeded.resources)
        self._authenticated_at = seeded.authenticated_at
        if port is not None:
            self._port = port
        self._hot_failures = 0
        self._stored_port_failures = 0
        self._reconnect_delay = 0
        self._started = True
        self._publish(seeded.data)
        if old is not None and old is not seeded.transport:
            await old.async_close()

    async def async_start(self) -> None:
        """Start one session generation and seed authoritative state."""
        if self._started:
            return
        self._lifecycle_epoch += 1
        epoch = self._lifecycle_epoch
        self._shutting_down = False
        generation = self._generation + 1
        transport = self._transport_factory.create(
            hass=self.hass,
            host=self._host,
            port=self._port,
            credentials=self._credentials,
            generation=generation,
        )
        self._pending_transport = transport
        current = asyncio.current_task()
        self._startup_task = current
        try:
            await transport.async_connect()
            async with self._admission.hold(0):
                seeded = await self._async_seed_generation(
                    transport,
                    generation,
                )
                if self._shutting_down or epoch != self._lifecycle_epoch:
                    raise asyncio.CancelledError
                await self._activate_seeded(seeded)
        except BaseException:
            await transport.async_close()
            if not self._shutting_down:
                self._publish_unavailable("startup_failed")
            raise
        finally:
            if self._pending_transport is transport:
                self._pending_transport = None
            if self._startup_task is current:
                self._startup_task = None
        if epoch == self._lifecycle_epoch and not self._shutting_down:
            self._initialize_deadlines()
            if self._start_scheduler:
                self._scheduler_task = self.hass.async_create_task(
                    self._async_scheduler_loop(),
                    "windfree scheduler",
                )

    async def async_shutdown(self) -> None:
        """Retain one cancellation-safe shutdown operation until cleanup ends."""
        task = self._shutdown_task
        if task is None:
            task = self.hass.async_create_task(
                self._async_shutdown_impl(),
                "windfree shutdown",
            )
            self._shutdown_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            caller_cancelled = current is not None and current.cancelling() > 0
            if not caller_cancelled:
                if self._shutdown_task is task:
                    self._shutdown_task = None
                task.result()
            try:
                await asyncio.shield(task)
            except BaseException:
                if self._shutdown_task is task:
                    self._shutdown_task = None
            raise
        except Exception:
            if self._shutdown_task is task:
                self._shutdown_task = None
            raise

    async def _async_shutdown_impl(self) -> None:
        self._shutting_down = True
        self._lifecycle_epoch += 1
        startup = self._startup_task
        tasks = tuple(
            task
            for task in (startup, self._scheduler_task, self._reconnect_task)
            if task is not None and task is not asyncio.current_task()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._startup_task = None
        self._scheduler_task = None
        self._reconnect_task = None
        transport = self._transport
        pending = self._pending_transport
        self._started = False
        self._publish_unavailable("coordinator_shutdown")
        if transport is not None:
            await transport.async_close()
            if self._transport is transport:
                self._transport = None
            if self._pending_transport is transport:
                self._pending_transport = None
        if pending is not None and pending is not transport:
            await pending.async_close()
            if self._pending_transport is pending:
                self._pending_transport = None

    async def _async_scheduler_loop(self) -> None:
        while not self._shutting_down:
            try:
                candidate = self.next_due()
                delay = max(
                    0.0,
                    min(candidate.deadline, self._reconcile_deadline)
                    - self._monotonic(),
                )
                if delay:
                    await self._sleep(delay)
                await self.async_run_scheduled_once()
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._sleep(0.5)

    async def async_run_scheduled_once(self) -> None:
        """Admit one logical poll, preserving Block2 atomicity in transport."""
        if self._shutting_down or self._transport is None:
            return
        candidate = self.next_due()
        now = self._monotonic()
        if candidate.deadline > now and self._reconcile_deadline > now:
            return
        priority = (
            int(candidate.tier)
            if candidate.deadline <= now
            else int(PollTier.RECONCILE)
        )
        async with self._admission.hold(priority):
            if self._shutting_down or self._transport is None:
                return
            candidate = self.next_due()
            now = self._monotonic()
            if candidate.deadline <= now:
                await self._async_poll_with_failure_locked(
                    candidate.path,
                    candidate.tier,
                    self._generation,
                    reconnect=True,
                )
            elif self._reconcile_deadline <= now:
                await self._async_reconcile_with_failure_locked(self._generation)

    async def _async_poll_path_locked(self, path: str) -> None:
        representation = await self._async_read(path)
        if path in HOT_PATHS:
            self._hot_failures = 0
            self.note_generation_stable()
        self._merge_resource(path, representation, UpdateSource.POLL)

    async def async_poll_path(self, path: str) -> None:
        tier = self._tier_for(path)
        generation = self._generation
        async with self._admission.hold(int(tier)):
            if self._shutting_down or self._transport is None:
                return
            await self._async_poll_with_failure_locked(
                path,
                tier,
                generation,
                reconnect=True,
            )

    async def _async_poll_with_failure_locked(
        self,
        path: str,
        tier: PollTier,
        generation: int,
        *,
        reconnect: bool,
    ) -> None:
        try:
            await self._async_poll_path_locked(path)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._async_request_failure_locked(
                path,
                tier,
                generation,
                error,
                reconnect=reconnect,
            )
            return

    async def _async_request_failure_locked(
        self,
        path: str,
        tier: PollTier,
        generation: int,
        error: BaseException,
        *,
        reconnect: bool,
    ) -> None:
        now = self._monotonic()
        retry = min(_PERIODS[int(tier)], float(2 ** min(self._hot_failures, 5)))
        self._schedule(path, now + max(1.0, retry))
        classification = self._fatal_classification(error)
        error = None
        if self._record_fatal_signal(classification, generation):
            self._authentication_rejected = True
            self._connection_reason = "authentication_rejected"
            await self._async_disconnect_locked("authentication_rejected")
            return
        if tier is not PollTier.HOT:
            return
        self._hot_failures += 1
        if self._hot_failures < 3:
            self._publish(
                replace(
                    self.data,
                    available=True,
                    failure_count=self._hot_failures,
                )
            )
            return
        await self.note_generation_dead()
        await self._async_disconnect_locked("connection_failed")
        if reconnect and not self._authentication_rejected:
            self._ensure_reconnect_task()

    async def _async_disconnect_locked(self, reason: str) -> None:
        transport = self._transport
        self._transport = None
        self._started = False
        self._publish_unavailable(reason)
        if transport is not None:
            await transport.async_close()

    def _ensure_reconnect_task(self) -> None:
        if self._shutting_down or (
            self._reconnect_task is not None and not self._reconnect_task.done()
        ):
            return
        self._reconnect_task = self.hass.async_create_task(
            self._async_reconnect_loop(),
            "windfree reconnect",
        )

    async def async_run_hot_cycle(self) -> None:
        """Run one hot health poll and apply the three-failure threshold."""
        path = HOT_PATHS[self._hot_index % len(HOT_PATHS)]
        self._hot_index += 1
        try:
            async with self._admission.hold(int(PollTier.HOT)):
                await self._async_poll_path_locked(path)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            async with self._admission.hold(int(PollTier.HOT)):
                await self._async_request_failure_locked(
                    path,
                    PollTier.HOT,
                    self._generation,
                    error,
                    reconnect=False,
                )
            return

    async def async_reconcile(self) -> None:
        """Revalidate exact identity and the model-specific write contract."""
        generation = self._generation
        async with self._admission.hold(int(PollTier.RECONCILE)):
            if self._shutting_down or self._transport is None:
                return
            await self._async_reconcile_with_failure_locked(generation)

    async def _async_reconcile_with_failure_locked(
        self,
        generation: int,
    ) -> None:
        try:
            await self._async_reconcile_locked()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._reconcile_deadline = self._monotonic() + 5.0
            classification = self._fatal_classification(error)
            error = None
            if self._record_fatal_signal(classification, generation):
                self._authentication_rejected = True
                self._connection_reason = "authentication_rejected"
                await self._async_disconnect_locked("authentication_rejected")

    async def _async_reconcile_locked(self) -> None:
        payloads = {path: await self._async_read(path) for path in RECONCILE_PATHS}
        tree = payloads["/device/0"]
        new_resources = {
            path: value
            for path, value in tree.items()
            if isinstance(path, str) and isinstance(value, Mapping)
        }
        prior_resources = dict(self._resources)
        self._resources.update(new_resources)
        try:
            identity = parse_identity(payloads["/oic/d"], payloads["/oic/p"], tree)
        except UnsupportedDevice:
            self._identity_drift = True
            self._disabled_write_paths = set(_ALL_WRITE_PATHS)
            parsed = parse_device_state(self._resources, self.data)
            self._publish(
                replace(
                    parsed,
                    available=True,
                    contract=replace(self.data.contract, writable_paths=frozenset()),
                    update_source=UpdateSource.RECONCILE,
                )
            )
            self._reconcile_deadline = self._monotonic() + _RECONCILE_PERIOD
            return
        try:
            contract = validate_contract(identity, new_resources, self._compatibility)
        except CapabilityMismatch:
            disabled: set[str] = set()
            for path in _ALL_WRITE_PATHS:
                if path not in new_resources:
                    disabled.add(path)
                    continue
                probe = dict(new_resources)
                if path in prior_resources:
                    probe[path] = prior_resources[path]
                try:
                    validate_contract(identity, probe, self._compatibility)
                except CapabilityMismatch:
                    continue
                disabled.add(path)
            if not disabled:
                disabled = set(_ALL_WRITE_PATHS)
            self._disabled_write_paths = disabled
            contract = replace(
                self.data.contract,
                writable_paths=self.data.contract.writable_paths - disabled,
            )
        else:
            self._disabled_write_paths.clear()
            self._identity_drift = False
        parsed = parse_device_state(self._resources, self.data)
        self._publish(
            replace(
                parsed,
                available=True,
                identity=identity,
                contract=contract,
                update_source=UpdateSource.RECONCILE,
                generation=self._generation,
            )
        )
        self._reconcile_deadline = self._monotonic() + _RECONCILE_PERIOD

    def handle_observe(
        self,
        generation: int,
        path: str,
        representation: Mapping[str, object],
    ) -> None:
        """Merge only current-generation, subscribed notifications."""
        if (
            self._shutting_down
            or generation != self._generation
            or path not in _OBSERVE_PATHS
        ):
            return
        self._merge_resource(
            path,
            representation,
            UpdateSource.OBSERVE,
            publish=not self._command_active,
        )
        event = self._observe_events.get(path)
        if event is not None:
            event.set()

    def _fatal_classification(self, error: BaseException) -> tuple[str, object] | None:
        if not isinstance(error, TransportError):
            return None
        if error.fatal_alert in _FATAL_ALERTS:
            return ("dtls", error.fatal_alert)
        if (
            error.coap_code in _FATAL_COAP_CODES
            and error.operation in _AUTHORIZED_COAP_OPERATIONS
        ):
            return ("coap", error.coap_code)
        return None

    def _record_fatal_signal(
        self,
        classification: tuple[str, object] | None,
        generation: int,
    ) -> bool:
        if classification is None:
            return False
        prior_generation = self._fatal_signals.get(classification)
        confirmed = prior_generation is not None and generation != prior_generation
        self._fatal_signals[classification] = generation
        return confirmed

    async def _async_reconnect_loop(self) -> None:
        while (
            not self._shutting_down
            and not self.data.available
            and not self._authentication_rejected
        ):
            if self._reconnect_delay:
                await self._sleep(float(self._reconnect_delay))
            try:
                await self.async_run_reconnect_attempt()
            except AuthenticationRejected:
                return
            except asyncio.CancelledError:
                raise
            if self.data.available:
                return
            await asyncio.sleep(0)

    async def _async_accept_candidate(
        self,
        generation: int,
        transport: WindFreeTransport,
        *,
        port: int | None = None,
    ) -> None:
        epoch = self._lifecycle_epoch
        self._pending_transport = transport
        try:
            async with self._admission.hold(0):
                seeded = await self._async_seed_generation(
                    transport,
                    generation,
                )
                if self._shutting_down or epoch != self._lifecycle_epoch:
                    raise asyncio.CancelledError
                await self._activate_seeded(seeded, port=port)
        except BaseException:
            await transport.async_close()
            raise
        finally:
            if self._pending_transport is transport:
                self._pending_transport = None

    async def _async_reconnect_failure(
        self,
        generation: int,
        error: BaseException,
        *,
        advance_backoff: bool = True,
    ) -> None:
        classification = self._fatal_classification(error)
        error = None
        if self._record_fatal_signal(classification, generation):
            self._authentication_rejected = True
            self._connection_reason = "authentication_rejected"
            self._publish_unavailable("authentication_rejected")
            return
        if advance_backoff:
            self._stored_port_failures += 1
            self._reconnect_delay = (
                2 if self._reconnect_delay == 0 else min(60, self._reconnect_delay * 2)
            )
        if self._transport is None:
            self._publish_unavailable("connection_failed")

    async def async_run_reconnect_attempt(self) -> None:
        """Attempt one stored-port generation, then bounded-range discovery."""
        if self._shutting_down or self._authentication_rejected:
            return
        self._generation_attempt = max(
            self._generation_attempt,
            self._generation,
        )
        self._generation_attempt += 1
        generation = self._generation_attempt
        transport: WindFreeTransport | None = None
        discovered_port: int | None = None
        try:
            transport = await self._transport_factory.reconnect(
                hass=self.hass,
                host=self._host,
                port=self._port,
                credentials=self._credentials,
                generation=generation,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._async_reconnect_failure(generation, error)
            if self._authentication_rejected:
                raise AuthenticationRejected("authentication_rejected") from None
            if self._stored_port_failures % 3 != 0:
                return
            try:
                self._generation_attempt += 1
                generation = self._generation_attempt
                discovered_port, transport = await self._transport_factory.discover(
                    hass=self.hass,
                    host=self._host,
                    credentials=self._credentials,
                    generation=generation,
                )
            except asyncio.CancelledError:
                raise
            except Exception as discovery_error:
                await self._async_reconnect_failure(
                    generation,
                    discovery_error,
                    advance_backoff=False,
                )
                if self._authentication_rejected:
                    raise AuthenticationRejected("authentication_rejected") from None
                return
        if transport is None:
            return
        try:
            await self._async_accept_candidate(
                generation,
                transport,
                port=discovered_port,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._async_reconnect_failure(generation, error)
            if self._authentication_rejected:
                raise AuthenticationRejected("authentication_rejected") from None

    async def note_generation_authenticated(self) -> None:
        self._authenticated_at = self._monotonic()

    async def note_generation_dead(self) -> None:
        if (
            self._authenticated_at is not None
            and self._monotonic() - self._authenticated_at < _SHORT_GENERATION
        ):
            self._short_generations += 1
            if self._short_generations >= 3:
                self._connection_reason = "possible_competing_session"
        self._authenticated_at = None

    def note_generation_stable(self) -> None:
        if (
            self._authenticated_at is not None
            and self._monotonic() - self._authenticated_at >= _SHORT_GENERATION
        ):
            self._short_generations = 0
            self._connection_reason = None
            self._fatal_signals.clear()

    def _validate_command_allowed(self, kind: CommandKind) -> None:
        if self._shutting_down or self._transport is None:
            raise CommandRejected("command_unavailable")
        probe = build_command(
            kind,
            {
                CommandKind.POWER: False,
                CommandKind.HVAC_MODE: self.data.climate.mode,
                CommandKind.TEMPERATURE: self.data.climate.target_temperature,
                CommandKind.FAN: self.data.climate.fan_mode,
                CommandKind.SWING: self.data.climate.swing_mode,
                CommandKind.PRESET: self.data.climate.preset_mode,
                CommandKind.DISPLAY_LIGHT: False,
                CommandKind.AUTO_CLEAN: False,
            }[kind],
            fresh_aggregate=self._resources.get(TEMPERATURE_PATH),
        )
        if self._identity_drift or probe.path not in self.data.contract.writable_paths:
            raise CommandRejected("command_unavailable")
        if kind.value in {"temperature", "fan", "swing", "preset"} and (
            kind.value
            not in self.data.contract.mode_controls.get(
                self.data.climate.mode, frozenset()
            )
        ):
            raise CommandRejected("command_incompatible")

    async def _wait_for_observe(
        self,
        command: DeviceCommand,
        event: asyncio.Event,
    ) -> bool:
        try:
            if event.is_set() and verify_command(command, self._resources):
                return True
            if self._observe_wait <= 0:
                return False
            async with asyncio.timeout(self._observe_wait):
                while True:
                    await event.wait()
                    if verify_command(command, self._resources):
                        return True
                    event.clear()
        except TimeoutError:
            return False

    async def _async_command_locked(
        self,
        kind: CommandKind,
        value: object,
        *,
        publish: bool = True,
        previous: WindFreeData | None = None,
    ) -> WindFreeData:
        self._validate_command_allowed(kind)
        authoritative: Mapping[str, object] | None = None
        fresh = (
            await self._async_read(TEMPERATURE_PATH)
            if kind is CommandKind.TEMPERATURE
            else None
        )
        command = build_command(kind, value, fresh_aggregate=fresh)
        event = asyncio.Event()
        self._observe_events[command.path] = event
        try:
            await self.transport.async_post(command.path, command.payload)
            observed = await self._wait_for_observe(command, event)
        finally:
            self._observe_events.pop(command.path, None)
        if not observed:
            authoritative = await self._async_read(command.path)
            self._resources[command.path] = authoritative
        if not verify_command(command, self._resources):
            rejected = parse_device_state(
                self._resources,
                previous or self.data,
            )
            if publish:
                self._publish(
                    replace(
                        rejected,
                        available=True,
                        update_source=UpdateSource.COMMAND,
                        generation=self._generation,
                    )
                )
            del authoritative, command, event, fresh, observed
            raise CommandRejected("command_rejected")
        for path in command.related_paths:
            if path == command.path:
                continue
            self._resources[path] = await self._async_read(path)
        parsed = parse_device_state(self._resources, previous or self.data)
        result = replace(
            parsed,
            available=True,
            update_source=UpdateSource.COMMAND,
            generation=self._generation,
        )
        if publish:
            self._publish(result)
        return result

    async def _async_set_hvac_mode_locked(self, mode: HvacMode) -> None:
        if self.data.climate.power:
            await self._async_command_locked(CommandKind.HVAC_MODE, mode)
            return
        published = self.data
        working = published
        try:
            working = await self._async_command_locked(
                CommandKind.HVAC_MODE,
                mode,
                publish=False,
                previous=working,
            )
            working = await self._async_command_locked(
                CommandKind.POWER,
                True,
                publish=False,
                previous=working,
            )
            authoritative_mode = await self._async_read(HVAC_MODE_PATH)
            self._resources[HVAC_MODE_PATH] = authoritative_mode
            mode_command = build_command(CommandKind.HVAC_MODE, mode)
            if not verify_command(mode_command, self._resources):
                for path in (
                    TEMPERATURE_PATH,
                    FAN_PATH,
                    SWING_PATH,
                    PRESET_PATH,
                ):
                    self._resources[path] = await self._async_read(path)
                working = parse_device_state(self._resources, working)
                raise CommandRejected("command_rejected")
            working = parse_device_state(self._resources, working)
            self._publish(replace(working, update_source=UpdateSource.COMMAND))
        except BaseException:
            authoritative = parse_device_state(self._resources, working)
            if authoritative != published:
                self._publish(
                    replace(
                        authoritative,
                        update_source=UpdateSource.COMMAND,
                    )
                )
            raise

    async def _async_turn_on_locked(self) -> None:
        remembered = self.data.climate.mode
        working = await self._async_command_locked(
            CommandKind.POWER,
            True,
            publish=False,
            previous=self.data,
        )
        authoritative_mode = await self._async_read(HVAC_MODE_PATH)
        self._resources[HVAC_MODE_PATH] = authoritative_mode
        mode_command = build_command(CommandKind.HVAC_MODE, remembered)
        if not verify_command(mode_command, self._resources):
            for path in (
                TEMPERATURE_PATH,
                FAN_PATH,
                SWING_PATH,
                PRESET_PATH,
            ):
                self._resources[path] = await self._async_read(path)
            parsed = parse_device_state(self._resources, working)
            self._publish(
                replace(
                    parsed,
                    available=True,
                    update_source=UpdateSource.COMMAND,
                    generation=self._generation,
                )
            )
            raise CommandRejected("command_rejected")
        working = parse_device_state(self._resources, working)
        self._publish(replace(working, update_source=UpdateSource.COMMAND))

    async def _async_command_worker(
        self,
        operation: str,
        kind: CommandKind | None,
        value: object,
    ) -> _CommandOutcome:
        try:
            async with self._admission.hold(0):
                self._command_active = True
                try:
                    if operation == "command":
                        assert kind is not None
                        await self._async_command_locked(kind, value)
                    elif operation == "set_mode":
                        await self._async_set_hvac_mode_locked(value)
                    elif operation == "turn_on":
                        await self._async_turn_on_locked()
                    else:
                        await self._async_command_locked(
                            CommandKind.POWER,
                            False,
                        )
                finally:
                    self._command_active = False
            return _CommandOutcome()
        except asyncio.CancelledError:
            return _CommandOutcome(cancelled=True)
        except Exception as error:
            classification = self._fatal_classification(error)
            category = (
                str(error)
                if isinstance(error, CommandRejected)
                and str(error)
                in {
                    "command_unavailable",
                    "command_incompatible",
                    "command_rejected",
                }
                else "command_failed"
            )
            error = None
            if self._record_fatal_signal(classification, self._generation):
                self._authentication_rejected = True
                self._connection_reason = "authentication_rejected"
                async with self._admission.hold(0):
                    await self._async_disconnect_locked("authentication_rejected")
            return _CommandOutcome(error=category)

    async def _async_public_command(
        self,
        operation: str,
        kind: CommandKind | None = None,
        value: object = None,
    ) -> None:
        task = self.hass.async_create_task(
            self._async_command_worker(operation, kind, value),
            "windfree command",
        )
        try:
            outcome = await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            task.cancel(*cancelled.args)
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            del operation, kind, value, task, cancelled
            raise
        if outcome.cancelled:
            del operation, kind, value, outcome, task
            raise asyncio.CancelledError
        if outcome.error is not None:
            error = outcome.error
            del operation, kind, value, outcome, task
            raise CommandRejected(error) from None

    async def async_command(self, kind: CommandKind, value: object) -> None:
        """Serialize, authoritatively verify, and then publish one command."""
        try:
            await self._async_public_command("command", kind, value)
        except BaseException:
            del kind, value
            raise

    async def async_set_hvac_mode(self, mode: HvacMode) -> None:
        """Set remembered mode and power on as one serialized operation."""
        try:
            await self._async_public_command("set_mode", value=mode)
        except BaseException:
            del mode
            raise

    async def async_turn_on(self) -> None:
        await self._async_public_command("turn_on")

    async def async_turn_off(self) -> None:
        await self._async_public_command("turn_off")

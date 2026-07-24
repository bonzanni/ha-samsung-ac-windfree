"""Authoritative local state, scheduling, and session supervision."""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Protocol

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

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
        self._resources: dict[str, Mapping[str, object]] = {}
        self._shutting_down = False
        self._started = False
        self._operation_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._observe_events: dict[str, asyncio.Event] = {}
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

    async def _async_read(self, path: str) -> Mapping[str, object]:
        started = self._monotonic()
        try:
            return await self.transport.async_get(path)
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

    def _merge_resource(
        self,
        path: str,
        representation: Mapping[str, object],
        source: UpdateSource,
    ) -> None:
        self._resources[path] = representation
        now = self._monotonic()
        self._last_updates[path] = now
        if path in self._deadlines:
            tier = self._tier_for(path)
            self._schedule(path, now + _PERIODS[int(tier)])
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

    async def _async_seed_generation(self) -> None:
        identity_payloads = {
            path: await self._async_read(path) for path in RECONCILE_PATHS
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
        self._resources = dict(resources)
        parsed = parse_device_state(self._resources, self.data)
        self._publish(
            replace(
                parsed,
                available=True,
                identity=identity,
                contract=contract,
                update_source=UpdateSource.RECONCILE,
                generation=self._generation,
                failure_count=0,
            )
        )
        await self.transport.async_observe(_OBSERVE_PATHS, self.handle_observe)
        await self.note_generation_authenticated()

    async def async_start(self) -> None:
        """Start one session generation and seed authoritative state."""
        if self._started:
            return
        self._shutting_down = False
        self._generation += 1
        transport = self._transport_factory.create(
            hass=self.hass,
            host=self._host,
            port=self._port,
            credentials=self._credentials,
            generation=self._generation,
        )
        self._transport = transport
        try:
            await transport.async_connect()
            await self._async_seed_generation()
        except BaseException:
            await transport.async_close()
            self._transport = None
            raise
        self._started = True
        self._initialize_deadlines()
        if self._start_scheduler:
            self._scheduler_task = self.hass.async_create_task(
                self._async_scheduler_loop(),
                "windfree scheduler",
            )

    async def async_shutdown(self) -> None:
        """Stop all owned tasks and the exact active generation."""
        if self._shutting_down:
            return
        self._shutting_down = True
        tasks = tuple(
            task
            for task in (self._scheduler_task, self._reconnect_task)
            if task is not None and task is not asyncio.current_task()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._scheduler_task = None
        self._reconnect_task = None
        transport = self._transport
        self._transport = None
        self._started = False
        if transport is not None:
            await transport.async_close()
        self._publish(replace(self.data, available=False))

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
        if candidate.deadline <= now:
            await self.async_poll_path(candidate.path)
            return
        if self._reconcile_deadline <= now:
            await self.async_reconcile()
            return

    async def _async_poll_path_locked(self, path: str) -> None:
        representation = await self._async_read(path)
        self._merge_resource(path, representation, UpdateSource.POLL)

    async def async_poll_path(self, path: str) -> None:
        async with self._operation_lock:
            await self._async_poll_path_locked(path)

    async def async_run_hot_cycle(self) -> None:
        """Run one hot health poll and apply the three-failure threshold."""
        path = HOT_PATHS[self._hot_index % len(HOT_PATHS)]
        self._hot_index += 1
        try:
            async with self._operation_lock:
                await self._async_poll_path_locked(path)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._hot_failures += 1
            self._publish(
                replace(
                    self.data,
                    available=self._hot_failures < 3,
                    failure_count=self._hot_failures,
                )
            )
            if self._hot_failures == 3 and self._transport is not None:
                await self.note_generation_dead()
                transport = self._transport
                self._transport = None
                await transport.async_close()
                if self._start_scheduler and not self._shutting_down:
                    self._reconnect_task = self.hass.async_create_task(
                        self._async_reconnect_loop(),
                        "windfree reconnect",
                    )
            return
        self._hot_failures = 0
        self.note_generation_stable()
        self._publish(replace(self.data, available=True, failure_count=0))

    async def async_reconcile(self) -> None:
        """Revalidate exact identity and the model-specific write contract."""
        async with self._operation_lock:
            await self._async_reconcile_locked()

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
        self._merge_resource(path, representation, UpdateSource.OBSERVE)
        event = self._observe_events.get(path)
        if event is not None:
            event.set()

    def _fatal_classification(self, error: BaseException) -> tuple[str, object] | None:
        if not isinstance(error, TransportError):
            return None
        if error.fatal_alert is not None:
            return ("dtls", error.fatal_alert)
        if error.coap_code in _FATAL_COAP_CODES:
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
        while not self._shutting_down and not self.data.available:
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

    async def _accept_reconnected_transport(
        self,
        generation: int,
        transport: WindFreeTransport,
    ) -> None:
        if self._shutting_down:
            await transport.async_close()
            return
        old = self._transport
        self._transport = transport
        self._generation = generation
        if old is not None and old is not transport:
            await old.async_close()
        try:
            await self._async_seed_generation()
        except BaseException:
            self._transport = None
            await transport.async_close()
            raise
        self._hot_failures = 0
        self._stored_port_failures = 0
        self._reconnect_delay = 0
        self._started = True

    async def async_run_reconnect_attempt(self) -> None:
        """Attempt one stored-port generation, then bounded-range discovery."""
        if self._shutting_down:
            return
        generation = self._generation + 1
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
            classification = self._fatal_classification(error)
            if self._record_fatal_signal(classification, generation):
                error = None
                raise AuthenticationRejected("authentication_rejected") from None
            error = None
            self._generation = generation
            self._stored_port_failures += 1
            self._reconnect_delay = (
                2 if self._reconnect_delay == 0 else min(60, self._reconnect_delay * 2)
            )
            if self._stored_port_failures % 3 != 0:
                return
            try:
                discovery_generation = generation + 1
                port, transport = await self._transport_factory.discover(
                    hass=self.hass,
                    host=self._host,
                    credentials=self._credentials,
                    generation=discovery_generation,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return
            self._port = port
            generation = discovery_generation
        try:
            await self._accept_reconnected_transport(generation, transport)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            classification = self._fatal_classification(error)
            if self._record_fatal_signal(classification, generation):
                error = None
                raise AuthenticationRejected("authentication_rejected") from None
            error = None
            self._generation = generation
            self._stored_port_failures += 1
            self._reconnect_delay = (
                2 if self._reconnect_delay == 0 else min(60, self._reconnect_delay * 2)
            )
            return

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
    ) -> None:
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
            self._merge_resource(
                command.path,
                self._resources.get(command.path, {}),
                UpdateSource.COMMAND,
            )
            del authoritative, command, event, fresh, observed
            raise CommandRejected("command_rejected")
        for path in command.related_paths:
            if path == command.path:
                continue
            self._resources[path] = await self._async_read(path)
        parsed = parse_device_state(self._resources, self.data)
        self._publish(
            replace(
                parsed,
                available=True,
                update_source=UpdateSource.COMMAND,
                generation=self._generation,
            )
        )

    async def async_command(self, kind: CommandKind, value: object) -> None:
        """Serialize, authoritatively verify, and then publish one command."""
        async with self._operation_lock:
            await self._async_command_locked(kind, value)

    async def async_set_hvac_mode(self, mode: HvacMode) -> None:
        """Set remembered mode and power on as one serialized operation."""
        async with self._operation_lock:
            if self.data.climate.power:
                await self._async_command_locked(CommandKind.HVAC_MODE, mode)
                return
            await self._async_command_locked(CommandKind.HVAC_MODE, mode)
            await self._async_command_locked(CommandKind.POWER, True)
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
                parsed = parse_device_state(self._resources, self.data)
                self._publish(
                    replace(
                        parsed,
                        available=True,
                        update_source=UpdateSource.COMMAND,
                        generation=self._generation,
                    )
                )
                raise CommandRejected("command_rejected")

    async def async_turn_on(self) -> None:
        async with self._operation_lock:
            await self._async_command_locked(CommandKind.POWER, True)

    async def async_turn_off(self) -> None:
        async with self._operation_lock:
            await self._async_command_locked(CommandKind.POWER, False)

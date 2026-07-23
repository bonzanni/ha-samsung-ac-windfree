# Samsung WindFree Local Integration Design

Date: 2026-07-23
Status: Approved for implementation planning

## Objective

Build a Home Assistant custom integration in this repository for the 2025
Samsung WindFree Comfort room-air-conditioner firmware family represented by
model `AR60F12C1AWNEU`.

Normal operation must be fully local. Home Assistant communicates directly
with the air conditioner over authenticated OCF/CoAP-DTLS and never uses a
Samsung account, SmartThings API, webhook, MQTT bridge, or Samsung command
service.

Initial setup may contact two public HTTPS endpoints once to bootstrap a client
certificate without user-supplied credentials. After a successful setup, the
integration must operate and restart without internet access.

## Scope

Version 1 supports only the live-verified 2025 Tizen Lite/RT OCF firmware
family:

- Consumer model: `AR60F12C1AWNEU`
- OCF device type: `oic.d.airconditioner`
- Firmware description prefix: `TP1X_DA-AC-RAC-01001`
- Platform: Tizen Lite / TizenRT 4.0
- OCF endpoint: DTLS 1.2 over a discovered UDP port in `49152` through `49160`
- Live target port observed during research: `49154`

The integration must reject unsupported device types and firmware families
with a translated config-flow error.

### Non-goals

- Samsung AC protocols on TCP `2878` or `8888`
- Older ARTIK or non-Tizen Samsung AC families
- SmartThings cloud fallback
- Generic Samsung appliance support
- Multi-zone or multi-indoor-unit controllers
- F1/F2 NASA or Non-NASA hardware-bus support
- Features without a safe, verified local contract

## Research Baseline

The design is based on:

- Samsung's official 2025 WindFree manual for `AR60F12C1AWNEU`
- The `smartthings-local` OCF/CoAP-DTLS protocol project
- The LocalThings Home Assistant integration as architectural prior art
- Older `samsungrac` and Samsung OCF community protocol research
- Home Assistant's current climate entity and integration-quality guidance
- Sanitized live testing against the authorized target unit

The legacy ports `2878/tcp` and `8888/tcp` actively refused connections on the
target. UDP `49154` completed DTLS 1.2 with a Samsung OCF appliance certificate.
An authenticated local GET of `/oic/sec/acl` returned CoAP `2.05`.

## Security and Certificate Bootstrap

Samsung does not expose a supported per-device local pairing flow for this
firmware. Local access relies on a publicly exposed Samsung `REMOVED_IDENTITY`
intermediate signing key whose identity is trusted by the appliance's factory
OCF access-control list.

This is an authentication workaround and must be documented as such.

### Zero-input config flow

The user supplies only the air conditioner's host or IP address.

On the first setup:

1. Download the public REMOVED_IDENTITY bundle from:
   `REMOVED_BUNDLE_URL`.
2. Require the downloaded bytes to match SHA-256
   `REMOVED_BUNDLE_SHA256`.
   A mismatch stops setup. The integration never silently accepts replacement
   signing material.
3. Parse exactly one private key and four certificates.
4. Verify the REMOVED_IDENTITY certificate and private key have the same public modulus.
5. Require REMOVED_IDENTITY certificate SHA-256 fingerprint
   `REMOVED_SIGNING_DIGEST`,
   then validate the certificate chain and expected Samsung subjects.
6. Open a TLS connection to `REMOVED_IDENTITY_HOST:443`. Samsung
   serves an OCF-private certificate chain that the normal system trust store
   rejects, so do not claim ordinary PKI validation. Instead require both the
   leaf-certificate SHA-256 fingerprint
   `REMOVED_IDENTITY_LEAF_DIGEST`
   and its SPKI SHA-256
   `REMOVED_IDENTITY_SPKI_DIGEST`.
   Also require the expected Samsung issuer and a
   `CN=*.REMOVED_HOST.com` subject before extracting the `uuid:<UUID>`
   identity.
7. Generate a fresh RSA-2048 private key in memory.
8. Mint a ten-year client leaf certificate containing that UUID in the
   subject and SAN, signed by REMOVED_IDENTITY with the legacy SHA-1 signature required by
   the device trust chain.
9. Sweep UDP ports `49152` through `49160`, establish authenticated DTLS, and
   require successful reads of `/oic/d`, `/oic/p`, and `/device/0`.
10. Verify the device type, model, firmware family, and live capability
    contract before creating the config entry.
11. Persist only the generated client private key, client certificate chain,
    discovered port, host, and sanitized identity metadata.
12. Drop all references to the downloaded universal CA key and bundle.

The bundle digest and Samsung identity-certificate pins are release-time
security pins. Supporting changed bootstrap material requires a reviewed
integration release that changes the affected pins explicitly.

The integration must not download or regenerate certificate material during
ordinary startup. A stored client-certificate authentication failure starts a
reauthentication flow. Reauthentication repeats the pinned bootstrap only
after explicit user confirmation in the Home Assistant UI.

Certificates, private keys, UUIDs, serials, MAC addresses, SSIDs, IP addresses,
and raw payloads must never appear in logs or diagnostics.

## Dependency Boundary

Pin `smartthings-local==0.1.0` and use it only for:

- DTLS session construction and certificate-chain loading
- CoAP encoding, parsing, Block2 reads, and response correlation
- OBSERVE registration and deregistration
- Request pacing primitives

The integration owns:

- Certificate-bootstrap policy and security validation
- Supported-model validation
- Resource-to-Home-Assistant mappings
- State cache and polling policy
- Write verification and related-resource reconciliation
- Reconnect supervision
- Home Assistant entities, config flow, diagnostics, and translations

No MQTT bridge or generic appliance registry is used.

## Architecture

### `WindFreeTransport`

A narrow adapter around one `DtlsCoapSession`.

Responsibilities:

- Connect, close, and expose cancellation-safe GET, POST, and OBSERVE methods
- Run blocking protocol calls through Home Assistant's executor
- Marshal reader-thread callbacks into the Home Assistant event loop with
  `loop.call_soon_threadsafe`
- Enforce a maximum of two requests per second
- Ensure only one active session and one foreground write per device
- Redact protocol errors before they reach logs or UI

### `WindFreeSessionSupervisor`

Owns the connection generation and reconnect lifecycle.

Responsibilities:

- Establish the one permitted DTLS session
- Register and refresh OBSERVE subscriptions
- Reconnect with exponential backoff from 2 to 60 seconds
- Replace state only with data from the current connection generation
- Prevent in-flight reconnect tasks from resurrecting a session after unload
- Detect likely competing-client/session conflicts and expose a sanitized
  diagnostic reason

### `WindFreeCoordinator`

The single authoritative state cache exposed to entities.

Responsibilities:

- Bootstrap from `/oic/d`, `/oic/p`, and `/device/0`
- Merge per-resource OBSERVE notifications
- Schedule local polling and full reconciliation
- Serialize and verify commands
- Publish immutable, typed `WindFreeData`
- Track availability, latency, update source, failure counts, and resource
  coverage

Entity properties read only coordinator memory and perform no I/O.

### Model and mapping layer

Typed parsers isolate Samsung field names from Home Assistant platforms:

- Identity and firmware parser
- Climate-state parser
- Temperature aggregate read/modify/write helper
- Filter parser
- Energy parser
- Alarm parser
- Diagnostics redactor

Unknown fields are ignored and never exposed as arbitrary entity attributes.

## Update Strategy

OCF OBSERVE is a freshness accelerator, not the sole source of truth. Upstream
research shows that some Samsung firmware stops emitting local OBSERVE
notifications when its cloud connection is blocked, even though local reads
and writes continue.

Polling tiers:

- Hot, every 5 seconds: power, operating mode, temperatures, fan, airflow, and
  special mode
- Warm, every 30 seconds: humidity, energy, alarms, display light, and
  auto-clean
- Cold, every 5 minutes: filter and current-limit diagnostics
- Full `/device/0` reconciliation every 5 minutes

Polling requests are distributed through the two-request-per-second limiter
rather than sent as a burst.

OBSERVE subscriptions cover all hot and warm resources. Any notification
updates the cache immediately and resets that resource's next poll deadline.
Polling remains enabled so internet-blocked operation has a worst-case core
state delay of about five seconds.

## Command Semantics

All writes use this sequence:

1. Acquire the coordinator operation lock.
2. Capture the authoritative original value when a read/modify/write payload
   is required.
3. Send the smallest verified CBOR representation.
4. Treat CoAP `2.04 Changed` only as acknowledgement.
5. Wait briefly for a matching OBSERVE update.
6. GET the target resource if OBSERVE did not prove the requested state.
7. Publish only the authoritative resulting state.
8. Raise a translated `HomeAssistantError` when the device retained or coerced
   another value.
9. Refresh related resources after commands whose firmware rules can change
   fan, airflow, temperature, or preset state.

Commands are never reported as successful solely because POST returned `2.04`.

## Sanitized Live Protocol Contract

All observations below were captured on the target model family. Identifiers
and raw production payloads are intentionally omitted.

| Capability | Resource and request shape | Live result |
| --- | --- | --- |
| Device discovery | GET `/oic/res`, `/oic/d`, `/oic/p`, `/device/0` | `2.05`; device type and model family confirmed |
| Standard power path | POST `/power/0` with `{"value": false}` | `4.04`; must not be used |
| Power | POST `/power/vs/0` with `{"x.com.samsung.da.power": "On"}` | `2.04`, OBSERVE, authoritative read-back `On`; restored `Off` |
| HVAC mode | POST `/mode/vs/0` with `{"x.com.samsung.da.modes": ["<mode>"]}` | Auto, Cool, Dry, Fan, and Heat each persisted; restored Cool |
| Temperatures | GET `/temperatures/vs/0` aggregate `items` | Current, desired, min 16, max 30, step 1, Celsius |
| Target temperature | Read/modify/write the aggregate `items` list with desired changed | 26 to 27 °C persisted, notified, and restored to 26 °C |
| Fan | POST `/wind/strength/vs/0` with scalar mode code | Auto to Low persisted and restored; supported codes 0 through 4 map to Auto through Turbo |
| Airflow | POST `/wind/direction/vs/0` with scalar direction | Horizontal persisted and restored; Fix, vertical, horizontal, and both advertised |
| Special mode | POST `/mode/convenient/vs/0` with scalar mode | Quiet, Smart, Speed, Nano, NanoSleep, Sleep, and DryComfort each persisted; restored Off |
| Display light | POST `/light/vs/0` with `{"mode": "On\|Off"}` | Both values persisted, notified, and restored |
| Auto-clean setting | POST `/option/autoclean/vs/0` with setting status | Both values persisted, notified, and restored |
| Air purification | POST advertised On value | Returned `2.04` but remained Off; exclude |
| Mute once | POST advertised On value | Returned `2.04` but remained Off; exclude |
| Humidity | GET `/humidity/vs/0` | Primary field stayed zero; alternate five-percent field returned plausible changing room humidity |
| Filter | GET `/filter/airdustfilter/vs/0` | Capacity, usage, desired interval, and wash status available |
| Energy | GET `/energy/consumption/vs/0` | Cumulative Wh available; instantaneous W field absent |
| Alarms | GET `/alarms/vs/0` | Stateful device and filter alarm records available |
| Current limit | GET `/electriccurrent/vs/0` | Enabled state and levels 3 through 9 readable; units and safe write semantics unknown |
| Push | OBSERVE on hot and warm resources | Immediate notifications observed for power, mode, temperature, fan, airflow, preset, light, auto-clean, and energy changes |
| Restoration | Final authoritative reads | Power Off, mode Cool, target 26 °C, fan Auto, airflow Fix, preset Off, display light On, auto-clean On |

## Home Assistant Entity Model

### Climate

One primary `climate` entity with no entity-name suffix.

HVAC mappings:

| Samsung | Home Assistant |
| --- | --- |
| Power Off | `HVACMode.OFF` |
| Auto / AI Auto | `HVACMode.AUTO` |
| Cool | `HVACMode.COOL` |
| Dry | `HVACMode.DRY` |
| Fan | `HVACMode.FAN_ONLY` |
| Heat | `HVACMode.HEAT` |

The integration must use `HVACMode.AUTO`, not `HEAT_COOL`, because the official
manual defines Auto as learned AI behavior rather than a user-controlled
heating/cooling range.

Climate attributes and features:

- Current temperature
- Target temperature
- Target temperature range 16 through 30 °C with 1 °C steps
- Current humidity from `x.com.samsung.da.fivepercentHumidity`
- Explicit TURN_ON and TURN_OFF features
- Fan modes Auto, Low, Medium, High, and Turbo
- Combined swing modes Fixed, Vertical, Horizontal, and Both
- Presets None, Quiet, Smart, Boost/MAX, WindFree, WindFree Sleep, Good Sleep,
  and Dry Comfort

The AC exposes airflow as one mutually exclusive enum. Therefore all choices
belong in `swing_modes`; the integration must not claim independent horizontal
and vertical controls.

Preset mappings:

| Samsung | Home Assistant value |
| --- | --- |
| Off | `none` |
| Quiet | `quiet` |
| Smart | `smart` |
| Speed | `boost` |
| Nano | `windfree` |
| NanoSleep | `windfree_sleep` |
| Sleep | `sleep` |
| DryComfort | `dry_comfort` |

The integration does not expose `hvac_action`. The firmware reports selected
mode but no authoritative compressor/action state.

### Additional entities

| Platform | Key | Default | Source |
| --- | --- | --- | --- |
| Switch | `auto_clean` | Enabled | `/option/autoclean/vs/0` |
| Switch | `display_light` | Enabled | `/light/vs/0` |
| Sensor | `filter_usage` | Enabled | Used/capacity as percent |
| Sensor | `filter_status` | Enabled | Normal, wash, or replace |
| Binary sensor | `filter_attention` | Enabled | Filter status or active filter alarm |
| Sensor | `energy_consumption` | Enabled | Cumulative Wh converted to kWh, `total_increasing` |
| Binary sensor | `problem` | Enabled | Any active non-filter device alarm |
| Sensor | `active_alarm` | Diagnostic | Active alarm code |
| Binary sensor | `current_limit_enabled` | Diagnostic, disabled | Read-only current-limit state |
| Sensor | `current_limit_level` | Diagnostic, disabled | Read-only opaque level |

Device information carries the consumer model, manufacturer, firmware, and
hardware platform. Firmware fields do not become standalone entities.

### Excluded capabilities

- Air purification and mute-once: acknowledged but rejected by live firmware
- Instantaneous power: unit advertised but value absent
- Freeze Wash: official-manual feature without a discovered local resource
- Eco and AI Energy: no clean local resource contract
- Timer and reservation rules: opaque encoded blobs
- Self-diagnosis: not live-tested and can start a physical diagnostic routine
- Current-limit writes: levels have no confirmed units or safety semantics
- Motion and presence: empty resources; official manual says motion detection
  is unavailable on this model
- Remote-temperature and welcome-cooling plumbing: unset internal resources

## Availability and Error Handling

- A single failed request does not mark the device unavailable.
- Three consecutive hot-tier failures or a dead session mark entities
  unavailable and start reconnect supervision.
- Last-known state may be retained internally for diagnostics but is not
  published as current while unavailable.
- Recovery logs once, performs a complete refresh, and restores entities
  together.
- Authentication failures start reauthentication instead of infinite
  reconnects.
- Unsupported/coerced writes raise a translated action error containing only
  the requested feature, never payload or identity data.
- Shutdown deregisters OBSERVE tokens, closes the exact session, joins
  protocol work within bounded deadlines, and prevents task resurrection.

## Config Flow

User step:

- Required host/IP only

Validation:

- Resolve and reach host
- Perform the pinned one-time bootstrap
- Sweep OCF ports
- Authenticate locally
- Read identity and resource tree
- Reject non-air-conditioners and unsupported model families
- Set the OCF device identifier as the unique ID

Reconfigure:

- Accept a new host
- Authenticate with stored client credentials
- Require the same unique device ID
- Rediscover the UDP port

Reauthentication:

- Explain that local credentials are no longer accepted
- On confirmation, repeat the pinned bootstrap
- Require the same unique device ID
- Replace only the per-installation leaf certificate and key

No YAML configuration is supported.

## Diagnostics and Privacy

Diagnostics are an explicit allowlist and perform zero device I/O.

Allowed:

- Integration and dependency versions
- Supported model-family label
- Connection state and generation
- Last update success and source
- Poll and OBSERVE health
- Failure and reconnect counts
- Sanitized request latency buckets
- Known resource coverage
- Certificate validity dates without subject, issuer details, or fingerprints
- Entity support flags

Forbidden:

- Host, IP, MAC, SSID
- Device UUID, config-entry ID, serial, OTN identifiers
- Any certificate or key material
- Certificate subjects, SANs, or fingerprints
- Raw payloads or arbitrary configuration dictionaries
- Alarm timestamps or opaque Samsung blobs

## Project Layout

```text
custom_components/samsung_ac_windfree/
  __init__.py
  binary_sensor.py
  climate.py
  config_flow.py
  const.py
  coordinator.py
  device.py
  diagnostics.py
  entity.py
  manifest.json
  models.py
  quality_scale.yaml
  sensor.py
  strings.json
  switch.py
  translations/en.json
  transport.py
  bootstrap.py
tests/
  conftest.py
  fixtures/
  test_binary_sensor.py
  test_bootstrap.py
  test_climate.py
  test_config_flow.py
  test_coordinator.py
  test_device.py
  test_diagnostics.py
  test_init.py
  test_models.py
  test_sensor.py
  test_switch.py
  test_transport.py
.github/workflows/ci.yml
CHANGELOG.md
LICENSE
README.md
hacs.json
pyproject.toml
requirements_test.txt
```

The low-level library remains an external pinned dependency and is not
vendored.

## Testing Strategy

Implementation follows test-driven development. Each live-confirmed behavior
is first encoded as a failing regression test using synthetic, sanitized
fixtures.

Required test groups:

- Bootstrap download pin, malformed bundles, wrong key, wrong chain, UUID
  extraction, in-memory leaf generation, and universal-key disposal
- Config flow success and all failure branches
- Unsupported device and model-family rejection
- Duplicate detection, reconfigure, and reauthentication
- Coordinator polling tiers, OBSERVE merges, generation isolation, reconnects,
  cancellation, and unload
- Correct vendor power path and aggregate-temperature read/modify/write
- All HVAC, fan, airflow, and preset mappings
- Verified command success, coercion, timeout, and related-resource refresh
- Entity availability and disabled-by-default diagnostics
- Humidity, filter, energy, and alarm parsers
- Diagnostics privacy with adversarial secret-like values
- No I/O from entity properties
- Config-entry setup, unload, and reload

CI runs:

- Pytest with at least 95 percent integration-module coverage
- Ruff formatting and linting
- Hassfest manifest and translation validation
- HACS integration validation

Live probes are separate from automated tests. They must capture original
state, change one value, read it back, restore it in a `finally` path, and
verify restoration. Live addresses, credentials, identifiers, and payloads
must never enter Git.

## Documentation and Quality

The repository includes:

- HACS metadata
- UI setup and removal instructions
- A clear warning about the unofficial certificate-authentication workaround
- Supported model and firmware scope
- Entity and preset documentation
- Local-only runtime and air-gapped polling behavior
- Known limitations
- Troubleshooting for bootstrap, authentication, Wi-Fi, and competing clients
- Automation examples
- Changelog
- Home Assistant `quality_scale.yaml`

The initial implementation targets a Silver-quality structure and also
includes diagnostics, reconfigure, translations, privacy-safe
troubleshooting, and Repairs issues for authentication failure and resource
coverage gaps.

## Acceptance Criteria

The implementation is ready for its first release only when:

1. A user can configure the verified model by entering only its host/IP.
2. Bootstrap validates pinned public material and stores no universal CA key.
3. Home Assistant can restart and operate the integration with internet access
   removed.
4. All included climate modes, temperature, fan, airflow, presets, display
   light, and auto-clean pass automated write-verification tests.
5. Air purification, mute-once, and other rejected/unverified surfaces are not
   exposed as working controls.
6. OBSERVE produces immediate updates when present and polling keeps core state
   within about five seconds when it is absent.
7. Every failure path preserves availability semantics and contains no private
   information.
8. Diagnostics pass the adversarial redaction tests.
9. The full automated suite, linting, and validation pass.
10. A final authorized live smoke test confirms setup, representative reads,
    representative reversible writes, unload, restart, internet-blocked
    polling, and exact state restoration.

## References

- Samsung model support and official manual:
  https://www.samsung.com/latin/support/model/AR60F12C1AWNEU/
- SmartThings-Local protocol research:
  https://github.com/QuiteYellow/SmartThings-Local
- LocalThings Home Assistant integration:
  https://github.com/mbillow/localthings
- Older Samsung AC local integration:
  https://github.com/SebuZet/samsungrac
- Home Assistant climate entity:
  https://developers.home-assistant.io/docs/core/entity/climate/
- Home Assistant integration quality rules:
  https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/

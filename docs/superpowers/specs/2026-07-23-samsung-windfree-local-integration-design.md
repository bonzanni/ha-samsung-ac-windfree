# Samsung WindFree Local Integration Design

Date: 2026-07-23
Status: Approved for implementation planning

## Objective

Build a Home Assistant 2026.7 custom integration in this repository for the
specific authorized 2025 Samsung WindFree Comfort room-air-conditioner carrying
the consumer label `AR60F12C1AWNEU`.

Normal operation must be fully local. Home Assistant communicates directly
with the air conditioner over authenticated OCF/CoAP-DTLS and never uses a
Samsung account, SmartThings API, webhook, MQTT bridge, or Samsung command
service.

Initial setup may contact two public HTTPS endpoints once to bootstrap a client
certificate without user-supplied credentials. After a successful setup, the
integration must operate and restart without internet access.

## Scope

Version 1 is intentionally scoped to one physical, live-verified 2025 Tizen
Lite/RT OCF unit and one exact firmware release:

- Consumer model: `AR60F12C1AWNEU`
- OCF device type: `oic.d.airconditioner`
- Exact firmware: `TP1X_DA-AC-RAC-01001_0000`
- Product version: `SYSTEM 2.0`
- Platform: `TizenRT 4.0`
- Platform firmware: `ARA-KR-TP1-25-ARXX00_11260401`
- OCF endpoint: DTLS 1.2 over a discovered UDP port in `49152` through `49160`
- Live target port observed during research: `49154`

Live implementation validation established that this OCF firmware does not
report the consumer SKU anywhere in `/oic/d`, `/oic/p`, or the 39 resource
representations. Samsung's official support page and manual prove that the
consumer label exists, but provide no authoritative mapping from the local OCF
model-number value to that SKU. Version 1 therefore hashes the complete local
model-number value and compares it with a release-pinned SHA-256 for this
physical unit. It also requires the exact firmware, ordered directory
descriptor, 39-resource count, device type, product version, OS, platform
firmware, and capability contract. The raw model-number value is never stored,
logged, shown, or committed. A different physical unit—including another unit
sold under the same consumer SKU—is rejected until an authoritative reusable
model proof is available.

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

The exact PyPI distribution `smartthings-local==0.1.0` was installed during
research. Its `DtlsCoapSession` completed the authenticated DTLS 1.2 handshake,
Block2 reads, OBSERVE registration, and the reversible verified writes in this
document against the target. Implementation still records and locks the release
artifact hash so dependency provenance is reproducible.

The target remained connected to Samsung's service during the live probes:
local OBSERVE notifications continued to arrive, which the upstream project
documents as cloud-connection-gated behavior. This is evidence that the shared
Samsung cloud identity can coexist with the target's ordinary cloud session.

## Security and Certificate Bootstrap

Samsung does not expose a supported per-device local pairing flow for this
firmware. Local access relies on a publicly exposed Samsung `REMOVED_IDENTITY`
intermediate signing key whose identity is trusted by the appliance's factory
OCF access-control list.

This is an authentication workaround and must be documented as such.

The UUID used below is Samsung's public, shared OCF cloud-service identity, not
a device identifier or per-user credential. The target's factory ACL grants
that identity access. Each Home Assistant installation generates a distinct
private key but presents the shared public identity in its leaf certificate.

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
   identity from the subject's `OU=uuid:<UUID>` relative distinguished name.
   The pinned leaf is valid through 2035-04-09. Keeping both the leaf and SPKI
   pins is intentional fail-closed policy, not ordinary Web PKI. Fetching the
   exact pinned certificate avoids embedding the literal shared UUID in the
   integration artifact; it is not expected to produce installation-specific
   information.
7. Generate a fresh RSA-2048 private key in memory.
8. Capture the authenticated HTTP `Date` header from the successful,
   system-trusted HTTPS bundle response in step 1 and require the local UTC
   clock to be within 24 hours of it. The Samsung identity endpoint itself does
   not provide an HTTP response and is not used as a clock source. A missing or
   malformed bundle-response `Date` is a bootstrap failure rather than a reason
   to mint a potentially invalid certificate. Set `notBefore` to the
   authenticated bundle-response time minus five minutes and `notAfter` to that
   same authenticated time plus ten years; the local clock is only a sanity
   check and never anchors certificate validity. Mint the client leaf containing
   that UUID in the subject and SAN, then sign it with REMOVED_IDENTITY using the legacy
   SHA-1 signature required by the device trust chain.
9. Sweep UDP ports `49152` through `49160`, establish authenticated DTLS, and
   require successful reads of `/oic/d`, `/oic/p`, and `/device/0`.
10. Verify the release-pinned SHA-256 of the complete local model-number value,
    exact firmware, exact ordered `/device/0` descriptor, exact 39-resource
    count, and live capability contract before creating the config entry. The
    consumer SKU is a display label and is not inferred from a field the device
    does not expose.
11. Only after local authentication and identity validation succeed, atomically
    persist the generated client private key, client certificate chain,
    discovered port, host, and sanitized identity metadata.
12. Retain no universal CA private key and no bundle members except the public
    intermediate certificates required in the presented client chain. Those
    public chain certificates are intentionally persisted with the generated
    leaf; the original combined bundle and unused members are absent from
    configuration, files, diagnostics, and backups. Python cannot promise
    secure zeroization of transient in-memory buffers; the private-key
    guarantee is non-persistence.

The bundle digest and Samsung identity-certificate pins are release-time
security pins. Supporting changed bootstrap material requires a reviewed
integration release that changes the affected pins explicitly.

The bundle URL is a bootstrap availability dependency, not a trust source.
Before release, maintainers must retain a recoverable copy of the already
public, digest-pinned bytes outside the integration artifact and document how a
new integration release can move the unchanged pin to a project-controlled
mirror. The integration does not silently try unpinned mirrors. Vendoring the
universal signing key requires a separate legal and governance decision.

Scheduled CI must periodically fetch the Samsung identity endpoint and compare
its leaf and SPKI with the release pins. Any rotation is reviewed immediately;
maintainers must also publish a planned pin update with adequate lead time
before the current certificate's 2035 expiry. A bootstrap failure caused by an
unavailable source or changed pin is reported distinctly from a local device or
credential failure.

The integration must not download or regenerate certificate material during
ordinary startup. A stored client-certificate authentication failure starts a
reauthentication flow. Reauthentication repeats the pinned bootstrap only
after explicit user confirmation in the Home Assistant UI.

Ordinary startup checks the stored client certificate's validity window. A
Repairs issue is created 90 days before expiry. Reauthentication creates and
validates replacement credentials before atomically replacing the existing
working credentials; a failed attempt leaves the prior config-entry data
unchanged.

Certificates, private keys, UUIDs, serials, MAC addresses, SSIDs, IP addresses,
and raw payloads must never appear in logs or diagnostics.

The generated per-installation client key is secret config-entry data stored by
Home Assistant and is therefore present in Home Assistant backups. User
documentation must disclose this and recommend protecting backups accordingly.

## Dependency Boundary

Pin `smartthings-local==0.1.0` and use it only for:

- DTLS session construction and certificate-chain loading
- CoAP encoding, parsing, Block2 reads, and response correlation
- OBSERVE registration and deregistration
- Request pacing primitives

The dependency is installed from PyPI, not from a Git URL. Home Assistant's
manifest pins the exact version; release records retain the PyPI wheel and
source-distribution SHA-256 values for audit, while CI installs the audited
artifact on every supported Home Assistant Python architecture. Home
Assistant's ordinary requirement installer enforces the version but does not
enforce those hashes, and documentation must not claim otherwise. Because the
package is pre-1.0 and its DTLS stack is load-bearing, upgrades require the same
transport contract tests and an authorized live smoke test.

The package's runtime dependency closure is `cbor2>=5.6` and
`pyOpenSSL>=23.0`, with pyOpenSSL in turn using Home Assistant's
process-global cryptography, cffi, and typing stack. The integration must not
pin a different pyOpenSSL or cryptography version over Home Assistant's own
constraints. Instead:

- The manifest pins `smartthings-local==0.1.0` and the live-verified
  `cbor2==6.1.3`.
- CI records the complete resolved dependency closure and tests it inside every
  supported Home Assistant release environment, including the minimum version
  declared in `hacs.json` and the current stable release.
- Scheduled dependency CI tests the next Home Assistant beta so a changed
  Home Assistant crypto constraint is found before users upgrade.
- A release is blocked unless the resolved closure passes DTLS context,
  certificate loading, Block2, and OBSERVE contract tests.

Explicitly pinning a conflicting process-global pyOpenSSL or cryptography in a
custom integration could break Home Assistant and is prohibited. If a future
Home Assistant-owned version falls outside the proven transport contract, the
supported Home Assistant range is held and documented until a reviewed
transport release restores compatibility.

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

The DTLS context uses the dependency's cipher-specific
`@SECLEVEL=0` setting solely for this appliance connection because Samsung's
chain requires SHA-1. It must not change Python, OpenSSL, Home Assistant, or
process-global TLS policy. CI and the release smoke test verify certificate
generation and handshake on the supported Home Assistant OS/container
OpenSSL build.

Version `0.1.0` accepts the generated certificate chain and private key as
in-memory PEM strings. The integration uses that path exclusively and never
writes credentials to temporary transport files.

### `WindFreeSessionSupervisor`

Owns the connection generation and reconnect lifecycle.

Responsibilities:

- Establish the one permitted DTLS session
- Register and refresh OBSERVE subscriptions
- Reconnect with exponential backoff from 2 to 60 seconds
- After three consecutive connection failures to the stored UDP port, close the
  failed generation and resweep the verified `49152` through `49160` range
  before the next backoff cycle
- Replace state only with data from the current connection generation
- Prevent in-flight reconnect tasks from resurrecting a session after unload
- Detect likely competing-client/session conflicts and expose a sanitized
  diagnostic reason

The supervisor does not assume that every UDP ephemeral port is an OCF
endpoint. The supported discovery contract remains the live- and
upstream-verified `49152` through `49160` range. Exhausting that range leaves
the device unavailable and produces a Repairs issue; it does not scan arbitrary
LAN ports.

Handshake timeouts, generic handshake errors, socket errors, device reboot, and
competing-session symptoms are transient. They trigger reconnect and port
rediscovery, never reauthentication. Fatal credential rejection requires
either:

- An explicit peer-sent fatal DTLS alert whose code is limited to
  `bad_certificate`, `unsupported_certificate`, `certificate_expired`,
  `certificate_unknown`, `unknown_ca`, or `access_denied`; or
- A completed DTLS session followed by CoAP `4.01 Unauthorized` or
  `4.03 Forbidden` from a resource that the validated identity contract permits.

Either signal must repeat on a fresh connection generation before the
integration offers reauthentication. Unclassified SSL/DTLS errors remain
transient and appear only as sanitized reconnect reasons.

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
- Revalidate the exact locally provable product fingerprint—device type,
  product version, OS, platform firmware, firmware/model-number consistency—
  and the required safe-write resource contract during startup and every full
  reconciliation

If firmware drift removes or changes a required resource, the coordinator
disables only the affected controls, retains safe readable entities, and
creates a Repairs issue describing a sanitized capability-contract mismatch.
It never attempts a legacy or guessed write shape.

If any identity gate changes, the entire model-specific write contract is no
longer trusted: all write surfaces become unavailable, safe readable entities
remain available, and a distinct unsupported-identity Repairs issue is created.

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
- Full identity and capability reconciliation every 5 minutes:
  `/oic/d`, `/oic/p`, and `/device/0`

Polling requests are distributed through the two-request-per-second limiter
rather than sent as a burst.

The steady-state polling budget is approximately 1.4 logical requests per
second before OBSERVE deadline resets: six hot resources per five seconds,
five warm resources per 30 seconds, two cold resources per five minutes, and
three full-reconciliation resources per five minutes. Block2 continuation
datagrams, retries, and command verification consume the remaining headroom.

The scheduler staggers resource deadlines and uses this priority order:

1. User command and its authoritative verification
2. Overdue hot resource
3. Warm resource
4. Cold resource
5. Full reconciliation

There is no separate health-check request: successful hot polling proves
session health, while the hot-tier failure counter drives reconnect supervision.

Priority applies when admitting a logical request. A token-stable Block2
transaction is not preempted between blocks, so full reconciliation begins only
when no command is queued and no hot read is overdue. Its dependency-enforced
block limit bounds the delay once admitted. Tests use a fake monotonic clock and
rate limiter to prove that nominal, failure-free hot state remains within about
five seconds and that Block2 work and retries degrade freshness in a bounded,
observable way.

OBSERVE subscriptions cover all hot and warm resources. Any notification
updates the cache immediately and resets that resource's next poll deadline.
Polling remains enabled so internet-blocked operation has a nominal core-state
delay of about five seconds. Diagnostics report the measured stalest hot
resource age; no strict five-second claim is made during reconnects, Block2
retries, or command verification.

## Command Semantics

All writes use this sequence:

1. Acquire the coordinator operation lock.
2. When a read/modify/write payload is required, perform a fresh GET under that
   lock immediately before modification. Do not start from coordinator cache.
   Change only the intended writable field and preserve the freshly read sibling
   fields required by the device's aggregate schema.
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

Setting a non-Off HVAC mode while the AC is off is one serialized logical
operation:

1. Persist and verify the requested mode while power is off, which the live
   firmware supports.
2. Turn power on and verify both power and mode.
3. If power-on fails, publish the authoritative remaining Off state while
   retaining the device's verified remembered mode.
4. If the device changes the mode during power-on, refresh all climate
   resources and raise a translated action error.

`turn_on` without an explicit mode restores the device's remembered non-Off
mode. `turn_off` changes power only and verifies Off without rewriting mode.

### Mode-dependent controls

The UI may list every model-supported fan, swing, and preset option because
Home Assistant climate capabilities are device-wide, but a command is sent
only when its combination is present in the sanitized compatibility matrix
derived from live evidence. Version 1 must not infer undocumented
combinations.

The completed release probes establish this conservative matrix:

- target temperature: Cool and Heat;
- fan codes Auto, Low, Medium, High, and Turbo: Cool;
- airflow Fix, vertical, horizontal, and both: Cool;
- Off, Quiet, Smart, Speed, Nano, NanoSleep, and Sleep presets: Cool;
- Off and DryComfort presets: Dry;
- Auto and Fan: mode selection only;
- display light: power-independent;
- auto-clean: only while operating;
- two seconds of settling after a mode change before another power/mode
  transition.

Unsupported combinations raise a translated error without sending a request.
If the device nevertheless coerces a previously verified combination, normal
read-back verification wins, related state is refreshed, and a sanitized
capability-contract Repairs issue is created.

## Sanitized Live Protocol Contract

All observations below were captured on the exact target model. Identifiers
and raw production payloads are intentionally omitted.

| Capability | Resource and request shape | Live result |
| --- | --- | --- |
| Device discovery | GET `/oic/res`, `/oic/d`, `/oic/p`, `/device/0` | `2.05`; exact single-unit hash, exact firmware, device type, ordered descriptor, and 39-resource contract confirmed; consumer SKU is not reported |
| Standard power path | POST `/power/0` with `{"value": false}` | `4.04`; must not be used |
| Power | POST `/power/vs/0` with `{"x.com.samsung.da.power": "On"}` | `2.04`, OBSERVE, authoritative read-back `On`; restored `Off` |
| HVAC mode | POST `/mode/vs/0` with `{"x.com.samsung.da.modes": ["<mode>"]}` | With power Off, Auto, Cool, Dry, Fan, and Heat each persisted and read back; restored Cool |
| Temperatures | GET `/temperatures/vs/0` aggregate `items` | Current, desired, min 16, max 30, step 1, Celsius |
| Target temperature | Read/modify/write the aggregate `items` list with desired changed | 26 to 25 °C persisted in both Cool and Heat, notified, and restored to 26 °C |
| Fan | POST `/wind/strength/vs/0` with scalar mode code | Codes 0 through 4 (Auto through Turbo) each persisted in Cool and were restored |
| Airflow | POST `/wind/direction/vs/0` with scalar direction | Fix, vertical, horizontal, and both each persisted in Cool and were restored |
| Special mode | POST `/mode/convenient/vs/0` with scalar mode | Off, Quiet, Smart, Speed, Nano, NanoSleep, and Sleep persisted in Cool; DryComfort was rejected in Cool and persisted in Dry; restored Off |
| Display light | POST `/light/vs/0` with `{"mode": "On\|Off"}` | Both values persisted, notified, and restored |
| Auto-clean setting | POST `/option/autoclean/vs/0` with setting status | Both values persisted while Cool/on; writes while powered off were rejected; restored On |
| Air purification | POST advertised On value | Returned `2.04` but remained Off; exclude |
| Mute once | POST advertised On value | Returned `2.04` but remained Off; exclude |
| Humidity | GET `/humidity/vs/0` | Primary field stayed zero; alternate `fivepercentHumidity` field returned direct percentage-like values, including 36 and 40 |
| Filter | GET `/filter/airdustfilter/vs/0` | Capacity, usage, desired interval, and wash status available |
| Energy | GET `/energy/consumption/vs/0` | Cumulative Wh available; instantaneous W field absent |
| Alarms | GET `/alarms/vs/0` | Stateful device and filter alarm records available |
| Current limit | GET `/electriccurrent/vs/0` | Enabled state and levels 3 through 9 readable; units and safe write semantics unknown |
| Push | OBSERVE on hot and warm resources | Immediate notifications observed for power, mode, temperature, fan, airflow, preset, light, auto-clean, and energy changes |
| Restoration | Final authoritative reads | Power Off, mode Cool, target 26 °C, fan Auto, airflow Fix, preset Off, display light On, auto-clean On |

Mode changes require a two-second firmware settle interval before a following
power or mode command. Without it, an immediate transition away from Auto can
be rejected even after authoritative Auto read-back.

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

`HVACMode.AUTO` writes the live-verified Samsung value `Auto`. Read-back
verification treats Samsung `Auto` and `AI Auto` as one equivalent HA mode, so
firmware normalization between those aliases is success rather than coercion.
Other returned values still fail verification.

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
- Presets None, Quiet, Smart, Boost/MAX, WindFree, WindFree Sleep, Sleep,
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

Despite its vendor name, `fivepercentHumidity` is parsed as the directly
reported integer percentage; it is not multiplied by five. Zero is treated as
the firmware family's unset sentinel unless future live evidence proves a real
zero-percent reading. Values outside 1 through 100, booleans, non-numeric
strings, and missing fields produce unknown humidity without failing the
climate entity. Parser fixtures cover valid numeric strings, zero, and invalid
boundaries.

### Additional entities

| Platform | Key | Default | Source |
| --- | --- | --- | --- |
| Switch | `auto_clean` | Enabled | `/option/autoclean/vs/0` |
| Switch | `display_light` | Enabled | `/light/vs/0` |
| Sensor | `filter_usage` | Enabled | Used/capacity as percent |
| Sensor | `filter_status` | Enabled | Normal, wash, or replace |
| Binary sensor | `filter_attention` | Enabled | Filter status or active filter alarm |
| Sensor | `energy_consumption` | Enabled | Cumulative Wh converted to kWh; energy device class and `total_increasing` state class |
| Binary sensor | `problem` | Enabled | Any active non-filter device alarm |
| Sensor | `active_alarm` | Diagnostic | Active alarm code |
| Binary sensor | `current_limit_enabled` | Diagnostic, disabled | Read-only current-limit state |
| Sensor | `current_limit_level` | Diagnostic, disabled | Read-only opaque level |

Device information carries the manually verified single-unit consumer label,
manufacturer, locally reported firmware, and hardware platform. Firmware fields
do not become standalone entities.

The energy entity uses native unit kWh, `SensorDeviceClass.ENERGY`,
`SensorStateClass.TOTAL_INCREASING`, and a precision that preserves the source
Wh resolution. A decreasing non-negative counter is published as the new
authoritative value so Home Assistant's statistics layer can recognize a reset;
negative, non-finite, malformed, or implausibly overflowing values are unknown
and never synthesized. Tests cover monotonic growth, device reset, missing
instantaneous power, and malformed input.

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
- Only the supervisor's repeated explicit fatal-certificate DTLS alert or
  repeated post-handshake CoAP authorization signal starts reauthentication.
  Timeouts, socket errors, and unclassified handshake failures remain
  reconnectable.
- Unsupported/coerced writes raise a translated action error containing only
  the requested feature, never payload or identity data.
- Shutdown deregisters OBSERVE tokens, closes the exact session, joins
  protocol work within bounded deadlines, and prevents task resurrection.

## Config Flow

User step:

- Required host/IP only

Validation:

- Resolve the host within five seconds; the authenticated DTLS sweep is the
  reachability test
- Show a cancellable progress step while performing the pinned one-time
  bootstrap, certificate generation, port sweep, and local validation
- Sweep OCF ports
- Authenticate locally
- Read identity and resource tree
- Reject non-air-conditioners and any local product, firmware, platform, or
  single-unit hash, exact firmware, platform, or capability fingerprint other
  than the exact tested contract
- Set the OCF device identifier as the unique ID

Every network operation has its own bounded timeout: 5 seconds for host
resolution, 15 seconds for each HTTPS bootstrap fetch, 6 seconds for each of the
nine DTLS sweep attempts, and 8 seconds for an authenticated CoAP read. The
successful swept session is reused for validation rather than handshaken twice;
ordinary reconnects use the transport's 12-second handshake timeout. Port
attempts are sequential and cancelled before the next begins. The setup
worst-case base budget is therefore 5 seconds for resolution, 30 seconds for
fetches, 54 seconds for the complete sweep, and 24 seconds for the three
required identity reads, leaving 37 seconds inside the 150-second overall
deadline for capability validation and cleanup. Cancellation closes sockets,
stops executor work at its next bounded operation, retains no universal signing
material, and returns no partial config entry. Failures are classified as
bootstrap unavailable, pin mismatch, invalid clock, device unreachable, local
authentication rejected, unsupported physical unit, or capability mismatch.

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
- Supported manually verified consumer label and exact single-unit hash status
- Connection state and generation
- Last update success and source
- Poll and OBSERVE health
- Failure and reconnect counts
- Sanitized request latency buckets
- Known resource coverage
- Certificate validity dates without subject, issuer details, or fingerprints
- Days until generated client-certificate expiry and whether the 90-day Repairs
  threshold is active
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

- Bootstrap download pin, source outage, malformed bundles, wrong key, wrong
  chain, subject-OU UUID extraction, bundle HTTPS Date clock validation,
  server-time-anchored leaf validity, persistence of only required public
  intermediates, non-persistence of the universal private key, atomic
  credential replacement, periodic pin canary, and certificate-expiry Repairs
- Config flow success and all failure branches
- Config-flow progress, DNS/fetch/sweep/read and overall timeout arithmetic,
  reuse of the successful swept session, and cancellation cleanup
- Unsupported device, exact-fingerprint, and firmware-family rejection
- Different-unit rejection even when consumer label and firmware match
- Duplicate detection, reconfigure, and reauthentication
- Coordinator polling tiers, OBSERVE merges, generation isolation, reconnects,
  cancellation, and unload
- Five-minute `/oic/d`, `/oic/p`, and `/device/0` identity/capability
  reconciliation
- Stored-port failure followed by automatic verified-range rediscovery
- Transient DTLS failure versus repeatable post-handshake authorization failure
- Poll-budget, priority, staggering, Block2 admission bounds, and hot-resource
  age
- Correct vendor power path and fresh aggregate-temperature
  read/modify/write under the operation lock
- Off-to-mode sequencing and partial-failure reconciliation
- Mode write and authoritative read-back while power is Off
- All HVAC, fan, airflow, and preset mappings
- Canonical `Auto` write and `Auto`/`AI Auto` read-back equivalence
- The live-verified HVAC-mode/control compatibility matrix and rejection of
  unverified combinations without device I/O
- Verified command success, coercion, timeout, and related-resource refresh
- Startup/full-reconciliation capability drift, affected-control disabling,
  and complete write disabling after any identity-gate change
- Entity availability and disabled-by-default diagnostics
- Direct-percentage humidity parsing including the zero sentinel, plus filter,
  energy reset, and alarm parsers
- Diagnostics privacy with adversarial secret-like values
- No I/O from entity properties
- Config-entry setup, unload, and reload
- Scoped OpenSSL security-level behavior on the supported HA runtime
- In-memory PEM credential handoff with no transport temporary files
- Full resolved dependency-closure tests across the minimum, stable, and beta
  Home Assistant environments

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
- Disclosure that the generated client key is included in protected Home
  Assistant backups
- Supported model and firmware scope
- Entity and preset documentation
- Local-only runtime and air-gapped polling behavior
- Known limitations
- Troubleshooting for bootstrap, authentication, Wi-Fi, and competing clients
- Bootstrap-source/pin maintenance and certificate-expiry recovery
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
2. Bootstrap validates pinned public material, uses cancellable bounded
   progress, and persists no universal CA key.
3. Home Assistant can restart and operate the integration with internet access
   removed.
4. All included climate modes, temperature, fan, airflow, presets, display
   light, and auto-clean pass automated write-verification tests.
5. Air purification, mute-once, and other rejected/unverified surfaces are not
   exposed as working controls.
6. OBSERVE produces immediate updates when present and failure-free polling
   keeps nominal core state within about five seconds when it is absent; the
   measured hot-resource age proves the claim in tests and live diagnostics.
7. Every failure path preserves availability semantics and contains no private
   information.
8. Diagnostics pass the adversarial redaction tests.
9. The full automated suite, linting, and validation pass.
10. A final authorized live smoke test confirms setup, representative reads,
    representative reversible writes, unload, restart, internet-blocked
    polling, scoped SHA-1 DTLS compatibility, stored-port failure recovery, and
    exact state restoration.
11. The exact local-fingerprint gate, mode/control compatibility matrix, fatal-auth
    discriminator, and firmware-drift behavior are covered by automated tests.

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

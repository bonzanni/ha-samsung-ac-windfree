# Samsung WindFree AC for Home Assistant

An unofficial Home Assistant custom integration for fully local monitoring and
control of one specifically validated Samsung WindFree air conditioner.
SmartThings is not required. Setup asks only for the device's host or IP address;
the secure port, local identity, capabilities, and client credentials are found
or created automatically.

Normal communication uses authenticated DTLS/CoAP on the local network. Internet
access is required only for a one-time internet bootstrap during initial setup,
reconfiguration, or reauthentication. Runtime updates, commands, Home Assistant
restarts, and reloads are fully local after setup.

> [!WARNING]
> This integration relies on an unofficial certificate bootstrap and a reviewed,
> digest-pinned copy of Samsung shared identity material. It is not endorsed by
> Samsung. Read [Security and backups](#security-and-backups) before installing.

## Supported device and firmware

This release is built and live-tested for exactly:

- Model: `AR60F12C1AWNEU`
- Reported firmware: `TP1X_DA-AC-RAC-01001_001`
- Platform: `TizenRT 4.0`
- Device class: Samsung residential air conditioner

The integration validates the model, platform, firmware family, local identity,
resource shapes, enumerations, and command compatibility before creating an
entry. Only the firmware above is claimed as supported. A later patch revision
sharing the `TP1X_DA-AC-RAC-01001` family may pass validation if its complete
resource contract remains identical, but it is unverified until explicitly
listed here. Older models and older firmware families are not supported.

## Installation

This repository is designed for installation as a custom integration:

1. Copy `custom_components/samsung_ac_windfree` into the same directory under
   your Home Assistant configuration, or install the repository as a custom
   integration with HACS.
2. Restart Home Assistant.
3. Go to **Settings > Devices & services > Add integration** and select
   **Samsung WindFree AC**.
4. Enter only the air conditioner's stable local host or IP address. A DHCP
   reservation is recommended.
5. Keep Home Assistant connected to the internet while the one-time bootstrap
   and local validation complete. No Samsung account, token, certificate,
   SmartThings login, port, PIN, or other value is requested.

The device must be powered, connected to the same routed local network, and
reachable without client isolation. Setup can take up to a few minutes while
the supported secure port range is tested.

## Removal

Go to **Settings > Devices & services**, open the Samsung WindFree AC entry, and
select **Delete**. This unloads local subscriptions and removes the integration's
entry, device, entities, and stored generated client credentials. Remove the
custom integration files and restart Home Assistant if you also want to
uninstall the code.

## Security and backups

The air conditioner requires a client certificate but exposes no supported
owner-pairing flow. During bootstrap, this integration downloads exact
digest-pinned bytes over HTTPS, validates their complete certificate structure,
uses the shared signing identity only in memory, verifies an independently
fetched Samsung identity certificate, and mints a new local client key and
certificate. Any source, pin, identity, clock, or contract mismatch fails closed.

The generated private key and certificate chain are stored in the Home Assistant
config entry so normal startup never needs the internet. Consequently, full
Home Assistant backups contain that generated private key. Protect backups as
credentials: encrypt them, restrict access, and delete obsolete copies. Anyone
who obtains the key may be able to authenticate locally to the configured air
conditioner until the certificate expires. The upstream shared identity method
also has a broader trust risk than an official owner-specific pairing protocol.

No private keys, device identifiers, host addresses, raw protocol payloads,
resource paths, or digest values are shown in UI errors, repairs, diagnostics,
or this documentation.

## Entities and controls

The primary climate entity reports power, current and target temperature,
humidity, and the selected operating mode. Supported HVAC modes are off, auto,
cool, dry, fan-only, and heat (warm mode). It also reports fan, swing, and
preset state.

Climate controls include:

- power and HVAC mode selection, including `cool` and `heat`;
- whole-degree set points from 16 to 30 °C;
- fan speeds `auto`, `low`, `medium`, `high`, and `turbo`;
- swing positions `fixed`, `vertical`, `horizontal`, and `both`;
- presets `none`, `quiet`, `smart`, `boost`, `windfree`,
  `windfree_sleep`, `sleep`, and `dry_comfort`.

The live contract allows temperature, fan, swing, and preset writes only while
the device is in Cool mode. Power, HVAC mode, display light, and auto clean
remain available in every mode. Home Assistant rejects incompatible commands
instead of guessing at undocumented behavior.

Additional entities are:

- sensors for filter usage, filter status, cumulative energy consumption,
  active alarm, and current-limit level;
- problem binary sensors for filter attention and device alarms;
- a diagnostic current-limit-enabled binary sensor;
- switches for auto clean and display light.

The current-limit level and enabled entities are disabled by default because
they are diagnostic. Availability is based on validated local state; malformed
or stale values are not exposed as authoritative readings.

## Update behavior

The integration requests CoAP OBSERVE notifications for frequently changing
and operational state. Bounded polling remains active as a fallback and for
resources that do not need push updates: hot state is checked on a short cadence,
settings and telemetry less often, and filter/current-limit data on a cold
cadence. A periodic identity and capability reconciliation detects firmware or
contract drift. Commands are rate-limited and confirmed by notification or a
local read before Home Assistant reports success.

If the connection drops, entities become unavailable and the coordinator retries
with bounded exponential backoff. It re-establishes subscriptions locally; it
does not contact SmartThings or perform certificate bootstrap during a normal
reconnect or restart.

## Limitations

- Only the exact model and verified firmware listed above are supported.
- One config entry represents one air conditioner; multi-device discovery and
  broadcast discovery are intentionally not implemented.
- There is no cloud control, remote access relay, SmartThings synchronization,
  energy tariff, schedule, firmware update, or owner-account management.
- Local protocol compatibility can change after a Samsung firmware update.
- The device may accept only a small number of authenticated local clients.
- Heating is exposed as `heat`, but the verified contract does not permit target
  temperature, fan, swing, or preset writes while Heat is active.

## Troubleshooting

**Setup cannot resolve or connect**

Confirm the host is correct and stable, the air conditioner is awake, routing
and firewall rules allow local UDP traffic, and Wi-Fi client isolation is off.
Use **Reconfigure** from the integration entry after its address changes.

**Bootstrap is unavailable or times out**

Temporarily allow Home Assistant outbound HTTPS and working DNS. Verify the host
clock is synchronized. Retry setup; do not paste or manually supply certificate
material.

**Bootstrap pin changed**

Stop. This is a security boundary, not a connectivity error. Install a reviewed
integration release with updated pins. Never disable pin checks or use an
unpinned source.

**Authentication rejected or certificate expiry**

Open the Home Assistant repair and start reauthentication. Temporary internet
access is needed to renew credentials; local device access is needed to validate
them. A repair is raised before certificate expiry. If the certificate has
already expired, reauthentication starts before normal coordinator setup.

**Entities are unavailable or commands are rejected**

Check the Repairs dashboard for identity or resource-contract changes. Confirm
that the command is permitted in the current mode. A firmware update can require
a new integration release.

**Port range exhausted or intermittent disconnects**

Close the SmartThings mobile app and any unofficial tools talking directly to
the air conditioner. Such competing clients can consume the device's limited
secure sessions. Restart the air conditioner only after checking network reachability.

For a sanitized diagnostic snapshot, use **Settings > Devices & services >
Samsung WindFree AC > Download diagnostics**. It excludes network addresses,
identifiers, certificate bytes, pins, and raw payloads. Report reproducible
problems at the [issue tracker](https://github.com/bonzanni/ha-samsung-ac-windfree/issues).

## Automation examples

Cool to 24 °C:

```yaml
actions:
  - action: climate.set_hvac_mode
    target:
      entity_id: climate.samsung_windfree_ac
    data:
      hvac_mode: cool
  - action: climate.set_temperature
    target:
      entity_id: climate.samsung_windfree_ac
    data:
      temperature: 24
```

Switch between cool and warm mode from an input:

```yaml
actions:
  - action: climate.set_hvac_mode
    target:
      entity_id: climate.samsung_windfree_ac
    data:
      hvac_mode: cool # mode: cool
  - action: climate.set_hvac_mode
    target:
      entity_id: climate.samsung_windfree_ac
    data:
      hvac_mode: heat # mode: heat
```

The executable Home Assistant climate action field is `hvac_mode`; the comments
also label these choices as `mode: cool` and `mode: heat`.

## Bootstrap source and pin maintenance

This section is for release maintainers. To reduce reliance on the original
archive host, move the unchanged digest-pinned bytes to a project-controlled
HTTPS mirror. Independently download the candidate and verify its complete
SHA-256 digest equals `BUNDLE_SHA256` before uploading or changing code. Then
update only `BUNDLE_URL`; do not change bytes and location in the same review.

Run the complete bootstrap test module, the integration test suite, JSON/YAML
validation, lint, and the authorized live setup gate before issuing a release.
The release notes must explain the mirror-only change. Never add
unpinned fallback mirrors, never fetch from a redirect or a different host, and never
weaken digest or certificate validation. The maintainer archive contains
sensitive shared material and must not be committed to Git or attached to a
release.

Project documentation: <https://github.com/bonzanni/ha-samsung-ac-windfree>

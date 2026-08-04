# Firmware policy

How this integration behaves when the air conditioner's firmware changes.

## Why this exists

The integration targets one release-pinned physical unit, and its write
commands were verified against that unit's exact firmware. Identity checking
enforces that scope. But on 2026-07-28 a routine Samsung OTA changed one build
string and the integration refused to load at all for 6.7 days, while the
device itself was completely healthy. That is the failure this policy prevents.

## What is gated, and what is not

`parse_identity` (`device.py`) enforces the device identity. Fields fall into
two groups.

**Gated — a mismatch refuses the device:**

| Field | Source | Meaning |
| --- | --- | --- |
| `rt` | `/oic/d` | Must include `oic.d.airconditioner` |
| `mnmc` | `/oic/d` | Consumer model, `AR60F12C1AWNEU` |
| `mnpv` | `/oic/p` | Product version, `SYSTEM 2.0` |
| `mnos` | `/oic/p` | Platform, `TizenRT 4.0` |
| `x.com.samsung.da.description` | `/information/vs/0` | Device firmware release |
| `x.com.samsung.da.modelNum` discriminator | `/information/vs/0` | Per-unit anchor, hashed |

**Not gated — recorded as telemetry:**

| Field | Source | Meaning |
| --- | --- | --- |
| `mnfv` | `/oic/p` | Platform firmware build, bumped by routine OTAs |

`di` (device id) sits in neither group: it must be present and a non-empty
string, but its **value is never compared** to the entry it belongs to, so a
different device id does not refuse the device today. That gap is #3.

`mnfv` must still be a string, but its value is never compared. No read or
write path depends on it, and `validate_contract` never consults it.
`TESTED_PLATFORM_FIRMWARE` records the newest platform firmware the live
capability matrix was actually run against; diagnostics report observed versus
tested so a drift is visible without being fatal.

## The unit anchor

`SUPPORTED_UNIT_FINGERPRINT_SHA256` hashes the **discriminator** — everything
after the first `|` in `x.com.samsung.da.modelNum` — not the whole string. The
whole string begins with the firmware release, so hashing it meant every
firmware change invalidated the unit anchor and forced the constant to be
recomputed alongside any firmware bump.

Caveat: the discriminator's stability across firmware updates is an assumption
supported by one post-OTA observation, in which the whole model number was
unchanged. Confirm it against the next update before treating it as
load-bearing.

## Deeper firmware changes refuse loudly

If a larger update changes the **device firmware release**
(`x.com.samsung.da.description`) or the unit discriminator, the integration
refuses to set up and reports "The connected device is not the supported
Samsung WindFree model and firmware". This is deliberate: such a change can
alter command semantics, and those are exactly what the pin protects. It is
expected to be rare, because routine platform OTAs no longer reach this path.

This applies on both paths. At setup the entry fails with "not the supported
Samsung WindFree model and firmware". If the change lands while the device is
disconnected, the reconnect attempt marks identity drift and raises the
`unsupported_identity_after_update` repair rather than retrying as a generic
connection failure — without that, a firmware change is indistinguishable from
an unreachable device and the operator is told to power-cycle a healthy unit.

Recovery requires a release that:

1. re-runs the live capability matrix against the new firmware,
2. updates `SUPPORTED_FIRMWARE` (and `TESTED_PLATFORM_FIRMWARE`) with that
   evidence recorded in the changelog's Verification section.

A user-facing acknowledgement must never restore writes on its own: unchanged
resource shapes do not prove command semantics.

## Known gaps

Tracked, not yet implemented:

- **#3** — `di` is required to be a non-empty string but is not compared against
  the entry's stored `device_id`/`unique_id`. Until it is, a substituted device
  presenting the same model and a replayed discriminator would not be caught by
  the device-id check alone.
- **#4** — the runtime reconcile path degrades to read-only on identity failure
  rather than refusing, which is inconsistent with the refuse-loudly policy
  above and would retain a substituted device in a read-only state.
- **#5** — reauthentication and reconfiguration use the same strict identity
  check, so a unit that has drifted cannot renew its certificate.
- **#6** — an entry stranded by `ConfigEntryError` does not retry
  automatically, so upgrading alone does not revive it without a reload.

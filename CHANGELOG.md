# Changelog

All notable changes to this project are documented in this file.

## [0.1.0] - 2026-07-24

### Supported

- Exact Samsung WindFree model `AR60F12C1AWNEU`, verified with reported firmware
  `TP1X_DA-AC-RAC-01001_0000` on TizenRT 4.0 and its exact local product
  fingerprint.
- Host-only setup with a one-time pinned certificate bootstrap and fully local
  authenticated runtime; SmartThings is not required.
- Climate power, auto/cool/dry/fan/heat modes, current and target temperature,
  humidity, fan, swing, and all verified presets.
- Local filter, energy, alarm, current-limit, auto-clean, and display-light
  entities.
- Hybrid push/poll updates, command confirmation, reconnect, diagnostics,
  reconfiguration, reauthentication, certificate-expiry warnings, and repairs
  for identity, capability, authentication, and local-port failures.

### Explicitly excluded

- Older models, older firmware families, and any device other than
  `AR60F12C1AWNEU`.
- cloud control, SmartThings account integration, remote access, schedules,
  firmware updates, and owner-account management.
- Automatic discovery and multi-device entries; configure each supported air
  conditioner by host.
- Undocumented command combinations. Temperature is verified in Cool and Heat;
  fan/swing and general presets in Cool; DryComfort in Dry; auto-clean while
  operating. Other combinations are rejected before local I/O.

### Verification

- local automated tests: 770 passed
- coverage: 95.24%, passed
- local dependency contract: 3 passed
- architecture import smoke: 2 of 2 passed
- hassfest: 1 integration checked, 0 invalid
- HACS: pending CI
- direct live AC identity/read/write/restoration matrix: passed
- Home Assistant production-console smoke: pending configuration

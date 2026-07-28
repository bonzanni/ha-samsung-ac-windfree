# Changelog

All notable changes to this project are documented in this file.

## [0.3.0] - 2026-07-28

### Changed

- **BREAKING:** the integration no longer creates its own client certificate.
  Setup and reauthentication now ask for a client key and certificate chain,
  uploaded as files, and validate them before anything is stored. Existing
  entries keep working with their stored credential and need no action until it
  expires.
- Setup, reconfiguration and reauthentication are now entirely local. The
  integration performs no network requests outside the local network at any
  point.

### Removed

- The certificate provisioning path, its pinned constants, and the scheduled
  pin canary. Obtaining a credential is outside the scope of this integration.
- The `bootstrap_pin_changed` and `bootstrap_unavailable` repair issues. Both
  were persistent, so entry setup deletes any that already exist. An entry that
  stays disabled never runs setup, so its issues persist until it is enabled.

### Fixed

- Removed a config-entry update listener that, combined with the reload helper,
  is deprecated since Home Assistant 2026.6 and becomes an error in 2026.12.

## [0.2.0] - 2026-07-28

### Added

- Brand images shipped inside the integration
  (`custom_components/samsung_ac_windfree/brand/`), so Home Assistant shows the
  integration's own icon instead of a placeholder. Home Assistant 2026.3.0 and
  later serve local brand images through the brands proxy and prefer them over
  the CDN; the previous route, a pull request against `home-assistant/brands`
  under `custom_integrations/`, is no longer accepted. The existing 2026.7.3
  minimum already satisfies this, so no version floor change was needed.
- Icons carry no Samsung wordmark or logo. See
  `docs/superpowers/specs/2026-07-28-brand-icons-design.md`.

## [0.1.0] - 2026-07-24

### Supported

- One release-pinned physical Samsung WindFree unit carrying consumer label
  `AR60F12C1AWNEU`, verified with exact firmware
  `TP1X_DA-AC-RAC-01001_0000` on TizenRT 4.0.
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

- Every other physical unit, including another unit sold as
  `AR60F12C1AWNEU`. Older models and firmware are also excluded.
- cloud control, SmartThings account integration, remote access, schedules,
  firmware updates, and owner-account management.
- Automatic discovery and multi-device entries.
- Undocumented command combinations. Temperature is verified in Cool and Heat;
  fan/swing and general presets in Cool; DryComfort in Dry; auto-clean while
  operating. Other combinations are rejected before local I/O.

### Verification

- local automated tests: 789 passed
- coverage: 95.36%, passed
- local dependency contract: 3 passed
- architecture import smoke: 2 of 2 passed
- hassfest: 1 integration checked, 0 invalid
- HACS: pending CI
- direct live AC identity/read/write/restoration matrix: passed
- Home Assistant production-console smoke: pending configuration

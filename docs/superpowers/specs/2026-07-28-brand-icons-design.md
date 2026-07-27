# Brand icons — design

**Date:** 2026-07-28
**Status:** implemented

## Problem

The integration ships no brand imagery, so Home Assistant renders a generic
placeholder wherever `samsung_ac_windfree` appears.

The historical fix — opening a PR against `home-assistant/brands` under
`custom_integrations/` — is no longer available. That folder is legacy and the
repository auto-closes new custom-integration PRs. Since **HA 2026.3.0** a custom
component ships its own brand images and the core `brands` integration proxies
them through `/api/brands/integration/{domain}/{image}`, preferring local files
over the CDN.

`hacs.json` already requires HA 2026.7.3, so every supported installation can
resolve local brand images.

## Constraints

The integration is explicitly unofficial and not endorsed by Samsung. The README
says so, and the icon must not contradict it.

An icon is a *source identifier* — the slot where a brand asserts origin. Placing
the Samsung wordmark or logo there would fail the nominative-fair-use conditions
that the integration's naming currently satisfies, and would read as a claim of
endorsement. `WindFree` is likewise a Samsung trademark, so it cannot appear as
lettering either.

Samsung blue as a colour carries no meaningful exposure: single colours are hard
to protect, and the hue on a non-Samsung glyph identifies no source.

## Decision

Device-first artwork in Samsung's blue, carrying no Samsung marks of any kind.

**Palette**

| Role | Light | Dark |
|---|---|---|
| Primary | `#1428A0` | `#6B8AFF` |
| Accent | `#5C7CFA` | `#A9BEFF` |

The dark variants lift the same hues so they hold contrast against the HA dark
theme, where `#1428A0` reads as near-black.

**Composition**

A front-facing wall-split indoor unit fills the upper half: a solid rounded
rectangle in primary, a 9×3 grid of accent dots across its face standing in for
the micro-perforated panel, and a narrow accent louvre bar along its bottom edge.
The louvre is what makes the shape read as an air conditioner rather than a
generic vented box.

Below it, four gently curved rows of accent dots thin and shrink downward.

The dot field replaces the arrow-and-swoosh vocabulary that conventional AC icons
use. WindFree's defining behaviour is diffusing air *without* a draft, so the
icon says still air, not moving air. It also fills the lower half of the canvas,
which a wall-split unit alone — a wide, thin bar — would leave empty.

## Assets

`custom_components/samsung_ac_windfree/brand/`

| File | Size |
|---|---|
| `icon.png` | 256×256 |
| `icon@2x.png` | 512×512 |
| `dark_icon.png` | 256×256 |
| `dark_icon@2x.png` | 512×512 |

Flat colour, transparent background, trimmed to content with a uniform 4% margin,
PNG optimised. `logo.png` is deliberately omitted — the core fallback chain
resolves `logo*` requests to `icon.png`.

The folder name must be exactly `brand`: `homeassistant/loader.py` derives
`has_branding` from `"brand" in self._top_level_files`. No manifest change is
required.

## Production

Generated with `openai/gpt-5.4-image-2` via OpenRouter. Codex CLI cannot do this —
it has no image generation and authenticates with a ChatGPT OAuth token that
cannot reach the images endpoint.

The OpenAI provider rejects `background: "transparent"` and returns RGB with no
alpha; asked for transparency it paints a checkerboard. So each icon renders at
1024×1024 on a flat pure-magenta field, and post-processing snaps every pixel to
the nearest of {magenta, primary, accent}. That both keys out the background and
corrects the model's colour drift to exact brand hexes. Alpha comes from a
separately resized coverage mask, so the downscale anti-aliases without leaving a
magenta fringe.

Verified: zero residual magenta pixels, alpha present, exact palette hexes
dominant, legible at 32×32.

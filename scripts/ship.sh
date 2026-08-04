#!/usr/bin/env bash
# Ship a release end to end: wait for CI, merge, tag, publish, deploy, verify.
#
#   scripts/ship.sh <pr-number> <version>       e.g. scripts/ship.sh 7 0.4.0
#
# Scope: this covers only what the ha-prod-console plugin does NOT do — the
# GitHub half (CI gate, merge, tag, release) and the post-deploy verification.
# The deploy itself is delegated to the plugin's integration-update, which owns
# the HACS and Supervisor semantics; do not reimplement that here.
#
# Every step is idempotent: an already-merged PR, an existing tag and an
# existing release are detected rather than retried blindly, so a run that
# fails midway can simply be re-run. The script stops at the first failure and
# names the stage that failed.
#
# Requires: gh (authenticated), ssh access to the host named in
# .claude/ha-prod-console.yml, and `op` for the Home Assistant token.
set -euo pipefail

PR="${1:?usage: ship.sh <pr-number> <version>}"
VERSION="${2:?usage: ship.sh <pr-number> <version>}"
TAG="v${VERSION}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_ROOT="${HA_PROD_CONSOLE_SCRIPTS:-}"
PLUGIN_CACHE="$HOME/.claude-lesina/plugins/cache/bonzanni-claude-plugins/ha-prod-console"
CONFIG="$REPO_DIR/.claude/ha-prod-console.yml"

cd "$REPO_DIR"
step() { printf '\n=== %s\n' "$*"; }
fail() { printf '!!! FAILED: %s\n' "$*" >&2; exit 1; }

# Resolve the plugin's scripts directory at run time. The cache keeps several
# versions side by side, so a hardcoded one silently runs stale code after an
# upgrade. Prefer an explicit override, else the highest version present.
resolve_plugin() {
    if [ -n "$PLUGIN_ROOT" ]; then
        [ -x "$PLUGIN_ROOT/integration-update.sh" ] ||
            fail "HA_PROD_CONSOLE_SCRIPTS=$PLUGIN_ROOT has no integration-update.sh"
        echo "$PLUGIN_ROOT"
        return
    fi
    [ -d "$PLUGIN_CACHE" ] || fail "ha-prod-console plugin not installed at $PLUGIN_CACHE"
    local newest
    newest="$(find "$PLUGIN_CACHE" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' |
        sort -V | tail -1)"
    [ -n "$newest" ] || fail "no ha-prod-console versions under $PLUGIN_CACHE"
    [ -f "$PLUGIN_CACHE/$newest/scripts/integration-update.sh" ] ||
        fail "ha-prod-console $newest has no scripts/integration-update.sh (layout changed?)"
    echo "$PLUGIN_CACHE/$newest/scripts"
}

# The host lives in the plugin's project config; do not duplicate it here.
ha_host() {
    local host
    host="$(sed -n 's/^host:[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$CONFIG" | head -1)"
    [ -n "$host" ] || fail "no host: in $CONFIG"
    # ssh alias -> address, so the REST API can be reached directly.
    ssh -G "$host" 2>/dev/null | awk '/^hostname /{print $2; exit}'
}

ha_token() {
    # shellcheck disable=SC1090
    source ~/.op-token
    op read 'op://Claude Code/Home Assistant /long-lived access token'
}

ha_get() { curl -fsS -m 15 -H "Authorization: Bearer $TOKEN" "$HA_URL$1"; }

step "1/7 waiting for CI on PR #$PR"
# `gh pr checks` has no --json in this CLI version; its plain output is
# tab-separated NAME<TAB>STATE<TAB>ELAPSED<TAB>URL. It also exits non-zero on a
# merged PR, so a re-run after a partial failure must not wait on it forever.
if [ "$(gh pr view "$PR" --json state --jq .state 2>/dev/null || echo UNKNOWN)" = "MERGED" ]; then
    echo "PR already merged; its CI gate was satisfied before the merge"
    states="pass"
fi
while [ -z "${states:-}" ]; do
    # One snapshot per iteration: polling separately for the wait, the verdict
    # and the count could observe three different runs.
    # `|| true`: gh exits non-zero for a check-less PR and pipefail would
    # otherwise abort the whole run at this assignment.
    snapshot="$(gh pr checks "$PR" 2>/dev/null | awk -F'\t' 'NF > 1 {print $2}')" || true
    if [ -n "$snapshot" ] && ! grep -qx 'pending' <<<"$snapshot"; then
        states="$snapshot"
        break
    fi
    sleep 20
done
# Anything that is not an explicit pass or skip counts as failure, so a
# cancelled or timed-out run can never be mistaken for success.
if grep -qvE '^(pass|skipping)$' <<<"$states"; then
    gh pr checks "$PR" | awk -F'\t' 'NF > 1 && $2 != "pass" && $2 != "skipping"'
    fail "CI is not green on PR #$PR"
fi
echo "CI green: $(grep -cx 'pass' <<<"$states") passing"

step "2/7 merging PR #$PR"
if [ "$(gh pr view "$PR" --json state --jq .state)" = "MERGED" ]; then
    echo "already merged"
else
    gh pr merge "$PR" --squash --delete-branch || fail "merge"
fi

step "3/7 syncing main"
# Name the remote and branch explicitly: main may have no upstream configured.
git checkout -q main && git pull -q --ff-only origin main || fail "pull main"
git log --oneline -1

step "4/7 tagging $TAG"
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "tag exists locally"
else
    git tag -a "$TAG" -m "$TAG" || fail "tag"
fi
git push -q origin "$TAG" 2>/dev/null || echo "tag already pushed"

step "5/7 publishing the release"
if gh release view "$TAG" >/dev/null 2>&1; then
    echo "release exists"
else
    notes="$(mktemp)"
    trap 'rm -f "$notes"' EXIT
    # index(), not a regex: the version's dots and brackets would otherwise be
    # read as a character class and never match the heading.
    awk -v marker="## [$VERSION]" '
        index($0, marker) == 1 {inside = 1; next}
        inside && index($0, "## [") == 1 {exit}
        inside {print}
    ' CHANGELOG.md > "$notes"
    [ -s "$notes" ] || fail "no changelog section for $VERSION"
    gh release create "$TAG" --title "$TAG" --notes-file "$notes" || fail "release"
fi

step "6/7 deploying to production via ha-prod-console (restarts HA Core)"
PLUGIN="$(resolve_plugin)"
echo "using $PLUGIN"
# HACS identifies releases by tag name, not by bare version. --restart is
# required because Home Assistant only loads new Python code on restart, and it
# also clears an entry stranded by a permanent ConfigEntryError (issue #6).
bash "$PLUGIN/integration-update.sh" --version "$TAG" --yes --restart || fail "deploy"

step "7/7 waiting for Home Assistant and verifying"
HA_URL="http://$(ha_host):8123"
TOKEN="$(ha_token)"
until curl -fsS -m 5 -o /dev/null -H "Authorization: Bearer $TOKEN" "$HA_URL/api/"; do
    sleep 10
done
echo "Home Assistant is back at $HA_URL"

# Discover the entry rather than hardcoding its id, and use the coordinator's
# own availability as the verdict: it is authoritative and does not depend on
# what any particular entity happens to be named.
DOMAIN="$(sed -n 's/^[[:space:]]*domain:[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$CONFIG" | head -1)"
[ -n "$DOMAIN" ] || fail "no integration.domain in $CONFIG"
ENTRY_ID="$(ha_get "/api/config/config_entries/entry" |
    python3 -c "import json,sys; print(next(e['entry_id'] for e in json.load(sys.stdin) if e['domain']=='$DOMAIN'))")"
[ -n "$ENTRY_ID" ] || fail "no config entry for $DOMAIN"

available=false
for _ in $(seq 1 15); do
    if ha_get "/api/diagnostics/config_entry/$ENTRY_ID" > /tmp/ship-diag.json 2>/dev/null &&
        python3 -c "import json,sys; sys.exit(0 if json.load(open('/tmp/ship-diag.json'))['data']['connection']['available'] else 1)"; then
        available=true
        break
    fi
    printf '%s waiting for %s to connect\n' "$(date +%H:%M:%S)" "$DOMAIN"
    sleep 20
done

step "result"
python3 - "$DOMAIN" <<'PY'
import json, sys
d = json.load(open("/tmp/ship-diag.json"))["data"]
print("integration_version:", d["integration_version"])
print("connection:", d["connection"])
print("firmware:", d.get("firmware"))
PY
[ "$available" = true ] || fail "$DOMAIN did not connect after the restart"
echo "SHIPPED: $TAG live on production"

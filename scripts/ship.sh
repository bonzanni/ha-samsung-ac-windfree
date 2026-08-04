#!/usr/bin/env bash
# Ship a release end to end: wait for CI, merge, tag, publish, deploy to the
# N150, and verify the integration actually comes back.
#
#   scripts/ship.sh <pr-number> <version>       e.g. scripts/ship.sh 7 0.4.0
#
# Every step is idempotent enough to re-run after a failure: already-merged
# PRs, existing tags and existing releases are detected rather than retried
# blindly. The script stops at the first failure and says which stage failed.
#
# Requires: gh (authenticated), ssh access to the host in
# .claude/ha-prod-console.yml, and `op` for the Home Assistant token.
set -euo pipefail

PR="${1:?usage: ship.sh <pr-number> <version>}"
VERSION="${2:?usage: ship.sh <pr-number> <version>}"
TAG="v${VERSION}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN="$HOME/.claude-lesina/plugins/cache/bonzanni-claude-plugins/ha-prod-console/0.8.0/scripts"
HA_URL="http://192.168.33.2:8123"
ENTITY="climate.samsung_windfree_ac"
ENTRY_ID="01KYAGNK3S1RH3K1F29NAHH2HK"

cd "$REPO_DIR"
step() { printf '\n=== %s\n' "$*"; }
fail() { printf '!!! FAILED: %s\n' "$*" >&2; exit 1; }

ha_token() {
    # shellcheck disable=SC1090
    source ~/.op-token
    op read 'op://Claude Code/Home Assistant /long-lived access token'
}

ha_get() {
    curl -fsS -m 15 -H "Authorization: Bearer $TOKEN" "$HA_URL$1"
}

step "1/7 waiting for CI on PR #$PR"
until [ "$(gh pr checks "$PR" --json bucket --jq 'all(.bucket != "pending")' 2>/dev/null)" = "true" ]; do
    sleep 20
done
if ! gh pr checks "$PR" --json bucket,name --jq 'all(.bucket == "pass" or .bucket == "skipping")' | grep -q true; then
    gh pr checks "$PR" | grep -v pass || true
    fail "CI is not green on PR #$PR"
fi
echo "CI green"

step "2/7 merging PR #$PR"
if [ "$(gh pr view "$PR" --json state --jq .state)" = "MERGED" ]; then
    echo "already merged"
else
    gh pr merge "$PR" --squash --delete-branch || fail "merge"
fi

step "3/7 syncing main"
git checkout -q main && git pull -q --ff-only || fail "pull main"
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
    # Release notes are the changelog section for this version.
    awk -v v="## \\[$VERSION\\]" '
        $0 ~ v {inside=1; next}
        inside && /^## \[/ {exit}
        inside {print}
    ' CHANGELOG.md > /tmp/ship-notes.md
    [ -s /tmp/ship-notes.md ] || fail "no changelog section for $VERSION"
    gh release create "$TAG" --title "$TAG" --notes-file /tmp/ship-notes.md || fail "release"
fi

step "6/7 updating the integration on the N150 via HACS (restarts HA Core)"
# HACS writes the new files, but Home Assistant only loads new Python code on
# restart, so --restart is required for the upgrade to take effect. It also
# clears the entry stranded by the old permanent ConfigEntryError, which never
# retries on its own (issue #6).
bash "$PLUGIN/integration-update.sh" --version "$VERSION" --yes --restart || fail "HACS update"

step "7/7 waiting for Home Assistant and verifying the entity"
TOKEN="$(ha_token)"
until curl -fsS -m 5 -o /dev/null -H "Authorization: Bearer $TOKEN" "$HA_URL/api/"; do
    sleep 10
done
echo "Home Assistant is back"

state=unknown
for _ in $(seq 1 15); do
    state="$(ha_get "/api/states/$ENTITY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])' 2>/dev/null || echo unknown)"
    printf '%s %s = %s\n' "$(date +%H:%M:%S)" "$ENTITY" "$state"
    case "$state" in
        unavailable|unknown|no-entity) sleep 20 ;;
        *) break ;;
    esac
done

step "result"
ha_get "/api/diagnostics/config_entry/$ENTRY_ID" | python3 -c '
import json, sys
d = json.load(sys.stdin)["data"]
print("integration_version:", d["integration_version"])
print("connection:", d["connection"])
print("firmware:", d.get("firmware"))
'
[ "$state" = "unavailable" ] && fail "entity still unavailable after reload"
echo "SHIPPED: $TAG live on the N150, $ENTITY = $state"

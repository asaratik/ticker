#!/usr/bin/env bash
#
# Submit one artifact to Apple's notary service and wait for the verdict.
#
#   ./packaging/macos/notarize.sh dist/Ticker-1.2.3.dmg
#
# Takes a .dmg, a .pkg, or a .zip of a .app -- the notary service will not
# take a bare .app, which is why the workflow dittos the bundle first.
#
# Retry because notarization is a network round trip to a service that has
# bad minutes: a submission can fail to upload, or time out waiting, without
# anything being wrong with the artifact. What is *not* retried is a verdict
# of Invalid -- Apple looked at the thing and rejected it, and asking again
# gets the same answer more slowly. That case prints the log, because
# "Invalid" on its own tells you nothing and the log names the offending
# binary.
#
# Environment (all required):
#   APPLE_ID         the Apple ID the app-specific password belongs to
#   APPLE_PASSWORD   an app-specific password, not the account password
#   APPLE_TEAM_ID    the ten-character Developer Program team identifier

set -euo pipefail

ARTIFACT="${1:-}"
ATTEMPTS="${NOTARIZE_ATTEMPTS:-3}"
WAIT_BETWEEN="${NOTARIZE_RETRY_DELAY:-30}"
TIMEOUT="${NOTARIZE_TIMEOUT:-30m}"

if [ -z "$ARTIFACT" ]; then
    echo "usage: $0 <artifact.dmg|artifact.zip>" >&2
    exit 2
fi
if [ ! -f "$ARTIFACT" ]; then
    echo "notarize: no such file: $ARTIFACT" >&2
    exit 2
fi
for var in APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID; do
    if [ -z "${!var:-}" ]; then
        echo "notarize: $var is not set" >&2
        exit 2
    fi
done

submit() {
    xcrun notarytool submit "$ARTIFACT" \
        --apple-id "$APPLE_ID" \
        --password "$APPLE_PASSWORD" \
        --team-id "$APPLE_TEAM_ID" \
        --timeout "$TIMEOUT" \
        --wait \
        --output-format json
}

for attempt in $(seq 1 "$ATTEMPTS"); do
    echo "== notarizing $(basename "$ARTIFACT") (attempt $attempt/$ATTEMPTS) =="

    # The exit status alone does not distinguish "could not submit" from
    # "submitted and rejected", so the JSON is captured and read.
    set +e
    RESULT="$(submit)"
    STATUS=$?
    set -e
    echo "$RESULT"

    VERDICT="$(printf '%s' "$RESULT" | /usr/bin/python3 -c \
        'import json,sys; print(json.load(sys.stdin).get("status",""))' \
        2>/dev/null || true)"
    SUBMISSION="$(printf '%s' "$RESULT" | /usr/bin/python3 -c \
        'import json,sys; print(json.load(sys.stdin).get("id",""))' \
        2>/dev/null || true)"

    if [ "$VERDICT" = "Accepted" ]; then
        echo "notarized: $ARTIFACT"
        exit 0
    fi

    if [ "$VERDICT" = "Invalid" ] || [ "$VERDICT" = "Rejected" ]; then
        echo "notarize: Apple rejected $ARTIFACT -- retrying will not help" >&2
        if [ -n "$SUBMISSION" ]; then
            # The verdict on its own is useless; the log names the binary.
            xcrun notarytool log "$SUBMISSION" \
                --apple-id "$APPLE_ID" \
                --password "$APPLE_PASSWORD" \
                --team-id "$APPLE_TEAM_ID" >&2 || true
        fi
        exit 1
    fi

    echo "notarize: attempt $attempt did not complete (status ${VERDICT:-none},"\
         "exit $STATUS)" >&2
    if [ "$attempt" -lt "$ATTEMPTS" ]; then
        echo "notarize: retrying in ${WAIT_BETWEEN}s" >&2
        sleep "$WAIT_BETWEEN"
    fi
done

echo "notarize: gave up on $ARTIFACT after $ATTEMPTS attempts" >&2
exit 1

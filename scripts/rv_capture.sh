#!/usr/bin/env bash
# Realistic Validation capture script for Issue #115.
#
# Self-bootstraps the temp fixture inside the worktree, runs the real iar
# entry, and writes the captured stdout to the given output file. Idempotent:
# the fixture directory is recreated on every invocation.
#
# Usage:
#     bash scripts/rv_capture.sh <item-name> <output-txt>
#
# Where <item-name> ∈ {logs-tail, logs-follow-before, logs-follow-after,
# logs-fallback, daemon-status, logs-tail-neg, daemon-status-neg}.

set -euo pipefail

ITEM_NAME="${1:-}"
OUTPUT_TXT="${2:-}"
if [[ -z "$ITEM_NAME" || -z "$OUTPUT_TXT" ]]; then
    echo "usage: $0 <item-name> <output-txt>" >&2
    exit 64
fi

WORKTREE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
FIXTURE_DIR="${WORKTREE_ROOT}/.iar/evidence/fixtures/${ITEM_NAME}"
EVIDENCE_DIR="${WORKTREE_ROOT}/.iar/evidence"

# Common env: point IAR at the in-tree fixture config.
export IAR_CONFIG="${FIXTURE_DIR}/config.toml"
export PYTHONUNBUFFERED=1
export COLUMNS=200  # wide enough for daemon-status log_path column

case "$ITEM_NAME" in
    logs-tail|logs-tail-neg)
        uv run python "${WORKTREE_ROOT}/scripts/rv_setup_fixture.py" \
            --fixture-dir "$FIXTURE_DIR" \
            --repo-id fixture-repo \
            --process-id abc123 \
            --kind daemon \
            --log-lines 50 \
            >/dev/null
        if [[ "$ITEM_NAME" == "logs-tail-neg" ]]; then
            # Negative: assert a sentinel that is NOT in the output.
            IAR_CONFIG="$IAR_CONFIG" uv run iar logs \
                --repo-id fixture-repo --lines 20 \
                | grep -q "SENTINEL_THAT_DOES_NOT_EXIST_ANYWHERE"
        else
            IAR_CONFIG="$IAR_CONFIG" uv run iar logs \
                --repo-id fixture-repo --lines 20
        fi
        ;;

    logs-fallback|logs-fallback-neg)
        # Fixture with one exited record pointing at a log file we then delete
        # so the supervisor's list_processes() falls through to fallback.
        uv run python "${WORKTREE_ROOT}/scripts/rv_setup_fixture.py" \
            --fixture-dir "$FIXTURE_DIR" \
            --repo-id fixture-repo \
            --process-id abc123 \
            --kind daemon \
            --log-lines 5 \
            --not-running \
            >/dev/null
        rm -f "${FIXTURE_DIR}/logs/daemon-abc123.log"
        if [[ "$ITEM_NAME" == "logs-fallback-neg" ]]; then
            IAR_CONFIG="$IAR_CONFIG" uv run iar logs \
                --repo-id fixture-repo \
                | grep -q "SENTINEL_THAT_DOES_NOT_EXIST_ANYWHERE"
        else
            IAR_CONFIG="$IAR_CONFIG" uv run iar logs \
                --repo-id fixture-repo
        fi
        ;;

    logs-follow-before|logs-follow-after|logs-follow-neg)
        # Build a running fixture, then run the follow driver. The driver
        # writes rv-2-logs-follow-before.txt / -after.txt itself.
        uv run python "${WORKTREE_ROOT}/scripts/rv_setup_fixture.py" \
            --fixture-dir "$FIXTURE_DIR" \
            --repo-id fixture-repo \
            --process-id abc123 \
            --kind daemon \
            --log-lines 30 \
            >/dev/null
        BEFORE="${EVIDENCE_DIR}/rv-2-logs-follow-before.txt"
        AFTER="${EVIDENCE_DIR}/rv-2-logs-follow-after.txt"
        : >"$BEFORE"
        : >"$AFTER"
        IAR_CONFIG="$IAR_CONFIG" uv run python \
            "${WORKTREE_ROOT}/scripts/rv_follow.py" \
            --fixture-dir "$FIXTURE_DIR" \
            --repo-id fixture-repo \
            --before-output "$BEFORE" \
            --after-output "$AFTER" \
            --log-file "${FIXTURE_DIR}/logs/daemon-abc123.log" \
            --timeout 20.0
        # Print whichever artifact the caller asked for.
        case "$ITEM_NAME" in
            logs-follow-before) cat "$BEFORE" ;;
            logs-follow-after)  cat "$AFTER" ;;
            logs-follow-neg)
                cat "$AFTER" | grep -q "SENTINEL_THAT_DOES_NOT_EXIST_ANYWHERE"
                ;;
        esac
        ;;

    daemon-status|daemon-status-neg)
        # Spawn a real long-lived sleep process so os.kill(pid, 0) returns
        # alive. The fixture's --live-pid arg points at the spawned pid.
        LIVE_PID_FILE="${FIXTURE_DIR}/live.pid"
        LIVE_READY_FILE="${FIXTURE_DIR}/live.ready"
        rm -f "$LIVE_PID_FILE" "$LIVE_READY_FILE"
        uv run python "${WORKTREE_ROOT}/scripts/rv_spawn_live_pid.py" \
            --pid-file "$LIVE_PID_FILE" \
            --ready-file "$LIVE_READY_FILE" \
            --cmd 'python3 -c "import time; time.sleep(600)"' \
            >/dev/null
        LIVE_PID="$(cat "$LIVE_PID_FILE")"
        uv run python "${WORKTREE_ROOT}/scripts/rv_setup_fixture.py" \
            --fixture-dir "$FIXTURE_DIR" \
            --repo-id fixture-repo \
            --process-id abc123 \
            --kind daemon \
            --log-lines 5 \
            --live-pid "$LIVE_PID" \
            >/dev/null
        # Run the iar daemon status. The -o /dev/null on grep ensures the
        # captured stdout is the iar output, not the grep output.
        if [[ "$ITEM_NAME" == "daemon-status-neg" ]]; then
            IAR_CONFIG="$IAR_CONFIG" uv run iar daemon status \
                --repo-id fixture-repo \
                | grep -q "SENTINEL_THAT_DOES_NOT_EXIST_ANYWHERE"
        else
            IAR_CONFIG="$IAR_CONFIG" uv run iar daemon status \
                --repo-id fixture-repo
        fi
        # Cleanup: kill the live sleep process.
        uv run python "${WORKTREE_ROOT}/scripts/rv_kill_live_pid.py" \
            --pid-file "$LIVE_PID_FILE" || true
        ;;

    *)
        echo "unknown item: $ITEM_NAME" >&2
        exit 64
        ;;
esac

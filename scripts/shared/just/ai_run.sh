#!/usr/bin/env bash

# scripts/shared/just/ai_run.sh
# Shared AI tool dispatch for `just ai *` recipes.
#
# Usage:
#   scripts/shared/just/ai_run.sh <ai_tool> <prompt_text> [--interactive]
#
# Modes:
#   one-shot (default) — claude streams via stream-json + jq for live
#     tool/thinking visibility, then exits. kimi uses --prompt.
#   --interactive     — drops --print / stream-json / jq; the AI tool
#     starts a REPL with the prompt as the first user message and stays
#     open so the caller can keep chatting (Ctrl+C / /exit to leave).
#
# Why both modes live in one script: the four `just ai *` recipes share
# the same ai_tool defaulting, prompt echo, and kimi / claude / fallback
# dispatch. Centralizing them keeps `justfile.shared` from drifting.

set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: scripts/shared/just/ai_run.sh <ai_tool> <prompt_text> [--interactive]"
    exit 1
fi

ai_tool="$1"
prompt_text="$2"
mode="one-shot"
shift 2
for arg in "$@"; do
    case "$arg" in
        --interactive) mode="interactive" ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

if [ -z "$ai_tool" ]; then
    ai_tool="claude"
fi

if [ -z "$prompt_text" ]; then
    echo "ERROR: prompt_text is required"
    exit 1
fi

echo "Tool: $ai_tool"
echo "Prompt: $prompt_text"
echo ""

if [ "$mode" = "interactive" ]; then
    # Interactive REPL: drop --print / stream-json / jq; the AI tool
    # runs in the foreground and listens for follow-up messages.
    case "$ai_tool" in
        claude)
            # `claude "<prompt>"` starts a REPL with the prompt as the
            # first user message; --dangerously-skip-permissions lets
            # the AI read/edit files without per-action prompts.
            claude --dangerously-skip-permissions "$prompt_text" || true
            ;;
        kimi|*)
            # kimi and unknown tools: pass the prompt as a positional
            # argument through the user's interactive shell so aliases
            # resolve. `|| true` keeps the wrapper from failing when
            # the user types /exit or Ctrl+C.
            "${SHELL:-bash}" -i -c "$(printf '%s %q' "$ai_tool" "$prompt_text")" || true
            ;;
    esac
else
    # One-shot: claude streams via stream-json + jq; kimi uses --prompt;
    # other tools fall through to a generic passthrough.
    case "$ai_tool" in
        claude)
            # claude: --print + --verbose + stream-json lets jq render
            # thinking/tool calls in real time. The trailing `|| true`
            # swallows non-zero exits so a truncated stream doesn't
            # surface as a recipe failure.
            claude --dangerously-skip-permissions --print --verbose --output-format stream-json --include-partial-messages < <(printf '%s' "$prompt_text") 2>&1 | \
                jq -n --unbuffered -r '
                    # Stateful: track per-block type and input byte count across the stream.
                    # content_block_start ships an empty `input: {}`; the real parameters
                    # arrive via `input_json_delta` fragments, then content_block_stop closes
                    # the line — so we open on start, stream on deltas, close on stop.
                    foreach inputs as $evt (
                        {index_types: {}, index_chars: {}};

                        if $evt.type == "stream_event" and $evt.event.type == "content_block_start" then
                            .index_types[($evt.event.index | tostring)] = $evt.event.content_block.type
                            | (if $evt.event.content_block.type == "tool_use" then .index_chars[($evt.event.index | tostring)] = 0 else . end)
                        elif $evt.type == "stream_event" and $evt.event.type == "content_block_delta" and $evt.event.delta.type == "input_json_delta" then
                            .index_chars[($evt.event.index | tostring)] = (.index_chars[($evt.event.index | tostring)] // 0) + ($evt.event.delta.partial_json | length)
                        else
                            .
                        end;

                        if $evt.type == "stream_event" and $evt.event.type == "content_block_start" and $evt.event.content_block.type == "tool_use" then
                            "\n🔧 \($evt.event.content_block.name)("
                        elif $evt.type == "stream_event" and $evt.event.type == "content_block_start" and $evt.event.content_block.type == "thinking" then
                            "💭 "
                        elif $evt.type == "stream_event" and $evt.event.type == "content_block_delta" and $evt.event.delta.type == "thinking_delta" then
                            $evt.event.delta.thinking
                        elif $evt.type == "stream_event" and $evt.event.type == "content_block_delta" and $evt.event.delta.type == "input_json_delta" then
                            if (.index_chars[($evt.event.index | tostring)] // 0) <= 120 then
                                $evt.event.delta.partial_json
                            else
                                empty
                            end
                        elif $evt.type == "stream_event" and $evt.event.type == "content_block_stop" and .index_types[($evt.event.index | tostring)] == "thinking" then
                            "\n"
                        elif $evt.type == "stream_event" and $evt.event.type == "content_block_stop" and .index_types[($evt.event.index | tostring)] == "tool_use" then
                            if (.index_chars[($evt.event.index | tostring)] // 0) > 120 then
                                "…)\n"
                            else
                                ")\n"
                            end
                        elif $evt.type == "result" then
                            "\n\($evt.result)\n"
                        else
                            empty
                        end
                    )
                ' || true
            ;;
        kimi)
            # kimi uses --prompt flag instead of positional argument
            "${SHELL:-bash}" -i -c "$(printf '%s --prompt %q' "$ai_tool" "$prompt_text")" || true
            ;;
        *)
            # Passthrough: any other tool name gets the prompt as a
            # positional argument through the user's interactive shell.
            "${SHELL:-bash}" -i -c "$(printf '%s %q' "$ai_tool" "$prompt_text")" || true
            ;;
    esac
fi

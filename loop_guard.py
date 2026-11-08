#!/usr/bin/env python3
"""Codex PreToolUse loop guard (stateless).

Reads the session transcript (which Codex already maintains) plus the pending
call from the hook event, and blocks the call when the same tool call — or the
same group of tool calls — is about to repeat for the 6th time.

Note on namespaces: hook events use friendly tool names and argument shapes
(e.g. tool_name "Bash", tool_input {"command": ...}), while the transcript
stores wire names and arguments (e.g. "exec_command", {"cmd": ...,
"yield_time_ms": ...}). Both sides are normalized into the transcript
namespace before comparison.
"""
import difflib
import json
import os
import sys
from pathlib import Path


LOOP_MESSAGE = (
    "⚠️ 循环检测：你已经连续第 6 次重复同样的工具调用（或同一组工具序列），"
    "参数也几乎不变。你陷入了死循环。立即停止这条路径：总结一下你已知的事实，"
    "换一种完全不同的方法，或者向用户说明卡点并询问。不要再以相同参数调用同样的工具。"
)
MAX_GROUP_SIZE = 5
REPEAT_COUNT = 6
SIMILARITY_THRESHOLD = 0.9
MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
MAX_CALLS = 200

# Hook-event tool name -> transcript tool name.
TOOL_NAME_ALIASES = {
    "Bash": "exec_command",
}


def allow():
    print("{}")
    sys.exit(0)


def block():
    output = json.dumps({"decision": "block", "reason": LOOP_MESSAGE}, ensure_ascii=False)
    try:
        print(output, flush=True)
    except BrokenPipeError:
        pass
    sys.exit(0)


# Keys whose string value is the call's primary payload (shell command,
# patch text, ...), and wrapper-only keys that carry no call identity.
PRIMARY_KEYS = ("cmd", "command", "input")
WRAPPER_KEYS = {
    "cmd", "command", "input",
    "yield_time_ms", "yield_time", "timeout", "timeout_ms",
    "workdir", "cwd", "login", "tty", "shell",
    "sandbox_permissions", "prefix_rule", "justification",
}


def normalize_parameters(value):
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, RecursionError):
            return value
    if isinstance(parsed, dict):
        # Hook events and transcript records wrap the same call differently
        # (e.g. {"command": ...} vs {"cmd": ..., "yield_time_ms": ...} vs a
        # raw string). When the dict is only a wrapper, identity is the
        # primary payload string itself.
        if set(parsed.keys()) <= WRAPPER_KEYS:
            for key in PRIMARY_KEYS:
                primary = parsed.get(key)
                if isinstance(primary, str):
                    return primary
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def same_call(left, right):
    if left["name"] != right["name"]:
        return False
    if left["arguments"] == right["arguments"]:
        return True
    if not left["arguments"] and not right["arguments"]:
        return True
    return difflib.SequenceMatcher(None, left["arguments"], right["arguments"]).ratio() >= SIMILARITY_THRESHOLD


def call_from_event(tool_name, tool_input, call_id=None):
    name = str(tool_name or "")
    name = TOOL_NAME_ALIASES.get(name, name)
    return {
        "name": name,
        "arguments": normalize_parameters(tool_input),
        "id": str(call_id or ""),
    }


def dedupe_calls(calls):
    result = []
    seen_ids = set()
    for call in calls:
        call_id = call.get("id")
        if call_id and call_id in seen_ids:
            continue
        if call_id:
            seen_ids.add(call_id)
        result.append(call)
    return result


def calls_from_transcript(path):
    with open(path, "rb") as source:
        source.seek(0, os.SEEK_END)
        end = source.tell()
        source.seek(max(0, end - MAX_TRANSCRIPT_BYTES))
        tail = source.read(MAX_TRANSCRIPT_BYTES)

    lines = tail.splitlines()
    if end > MAX_TRANSCRIPT_BYTES and lines:
        lines = lines[1:]

    calls = []
    last_call_open = False
    for line in lines:
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        if ptype in {"function_call_output", "custom_tool_call_output"}:
            last_call_open = False
            continue
        if ptype not in {"function_call", "custom_tool_call"}:
            continue
        call_id = payload.get("id") or payload.get("call_id") or ""
        parameters = payload.get("arguments")
        if parameters is None:
            parameters = payload.get("input")
        calls.append(call_from_event(payload.get("name"), parameters, call_id))
        last_call_open = True
    return calls[-MAX_CALLS:], last_call_open


def transcript_from_session_id(session_id):
    if not session_id:
        return None
    roots = []
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        roots.append(Path(codex_home) / "sessions")
    roots.append(Path.home() / ".codex" / "sessions")
    for sessions_root in roots:
        try:
            candidates = sorted(
                (path for path in sessions_root.rglob("*.jsonl") if session_id in path.name),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            continue
        if candidates:
            return candidates[0]
    return None


def is_loop(calls):
    if len(calls) < REPEAT_COUNT:
        return False
    for period in range(1, MAX_GROUP_SIZE + 1):
        window_size = period * REPEAT_COUNT
        if len(calls) < window_size:
            break
        window = calls[-window_size:]
        positions = range(period, window_size)
        if all(
            same_call(window[position % period], window[position])
            for position in positions
        ):
            return True
    return False


def main():
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict) or event.get("hook_event_name") != "PreToolUse":
            allow()

        transcript_path = event.get("transcript_path")
        if not transcript_path:
            transcript_path = transcript_from_session_id(event.get("session_id"))
        if not transcript_path:
            allow()

        calls, last_call_open = calls_from_transcript(transcript_path)
        current = call_from_event(
            event.get("tool_name"),
            event.get("tool_input"),
            event.get("tool_use_id"),
        )
        # Codex writes the pending call's record into the transcript BEFORE
        # PreToolUse hooks run, but its output only after execution. So if the
        # transcript's last call is still "open" (no output record yet) and it
        # is exactly this pending call, it is already counted — appending it
        # again would duplicate the tail element and break group (period >= 2)
        # detection. If the last call is closed, the pending call is not in
        # the transcript yet and must be appended.
        pending_recorded = (
            last_call_open
            and calls
            and calls[-1]["name"] == current["name"]
            and calls[-1]["arguments"] == current["arguments"]
        )
        if not pending_recorded:
            calls.append(current)
        calls = dedupe_calls(calls)[-MAX_CALLS:]
        if is_loop(calls):
            block()
        allow()
    except Exception:
        allow()


if __name__ == "__main__":
    main()

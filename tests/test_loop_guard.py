#!/usr/bin/env python3
"""Unit tests for loop_guard.py.

Simulates Codex 0.167 transcript records and PreToolUse hook events:
- Transcript records use the wire format: name "exec_command", arguments a
  JSON string like {"cmd": "..."}; a completed call is followed by a
  function_call_output record. When pending_open=True the last call has no
  output yet (it is the pending call whose PreToolUse is running).
- Hook events use the friendly format: tool_name "Bash",
  tool_input {"command": "..."}.

Usage: python3 tests/test_loop_guard.py [path/to/loop_guard.py]
"""
import json
import os
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO_ROOT, "loop_guard.py")


def make_transcript(cmds, pending_open=True):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for i, cmd in enumerate(cmds):
            rec = {"type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command",
                "arguments": json.dumps({"cmd": cmd})}}
            f.write(json.dumps(rec) + "\n")
            is_last = i == len(cmds) - 1
            if not (is_last and pending_open):
                out = {"type": "response_item", "payload": {
                    "type": "function_call_output", "output": "ok"}}
                f.write(json.dumps(out) + "\n")
    return path


def run_hook(transcript_path, command):
    event = {
        "hook_event_name": "PreToolUse",
        "session_id": "test",
        "transcript_path": transcript_path,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": "call_x",
    }
    proc = subprocess.run(
        [sys.executable, SCRIPT],
        input=json.dumps(event), capture_output=True, text=True, timeout=10)
    out = (proc.stdout or "").strip()
    try:
        parsed = json.loads(out) if out else {}
    except ValueError:
        parsed = {}
    return "BLOCK" if parsed.get("decision") == "block" else "ALLOW"


A, B, C = "echo AAA", "echo BBB", "echo CCC"
X = "echo LOOPTEST"
cases = []

p = make_transcript([X] * 6)
cases.append(("p1 x6 pending open", "BLOCK", run_hook(p, X)))
p = make_transcript([X] * 5)
cases.append(("p1 x5 pending open", "ALLOW", run_hook(p, X)))
p = make_transcript([A, B, C] * 6)
cases.append(("ABC group x6 pending open", "BLOCK", run_hook(p, C)))
p = make_transcript([A, B, C] * 5 + [A, B])
cases.append(("ABC x5+B pending open", "ALLOW", run_hook(p, B)))
p = make_transcript([X] * 5, pending_open=False)
cases.append(("p1 x5 all closed +append", "BLOCK", run_hook(p, X)))
p = make_transcript([f"cmd-{i}" for i in range(9)])
cases.append(("distinct calls", "ALLOW", run_hook(p, "cmd-new")))
cmds = [f"check status attempt {i}" for i in range(1, 7)]
p = make_transcript(cmds)
cases.append(("fuzzy args x6 pending open", "BLOCK", run_hook(p, "check status attempt 6")))
p = make_transcript([A, B] * 6)
cases.append(("AB group x6 pending open", "BLOCK", run_hook(p, B)))
p = make_transcript([X] * 6, pending_open=False)
cases.append(("p1 x6 closed +different current", "ALLOW", run_hook(p, "echo OTHER")))

ok = True
for name, expected, got in cases:
    passed = expected == got
    ok = ok and passed
    print(f"{'PASS' if passed else 'FAIL'}  {name}: expected {expected}, got {got}")
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)

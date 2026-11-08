# codex-loop-guard

A stateless [Codex CLI](https://github.com/openai/codex) `PreToolUse` hook that breaks tool-call loops.

When the model repeats the **same tool call 6 times** — or repeats the **same group of up to 5 tool calls 6 times** — with identical or nearly identical arguments (≥ 90% similar), the hook blocks the pending call and feeds the model a warning telling it to stop, reassess, and change approach.

No daemon, no state files, no dependencies: the hook re-reads the session transcript that Codex already maintains, on every invocation.

## Why

Codex agents sometimes get stuck: the same shell command over and over, or a cycle like `A → B → C → A → B → C`, burning quota with no progress. This hook detects the pattern mechanically and interrupts it at the 6th repetition, before the model wastes more turns.

## How it works

1. On every `PreToolUse` event, the hook reads only the tail of the session transcript (the detection window needs just the last 30 calls) (`transcript_path` from the hook input; falls back to locating the session file by `session_id` under `~/.codex/sessions`).
2. It appends the pending call (unless it is already recorded — see notes below) and checks, for each group size `p = 1..5`, whether the last `6 × p` calls form 6 repetitions of the same sequence. Two calls count as *the same* when the tool names match and the normalized arguments are equal or ≥ 0.9 similar (`difflib` ratio).
3. On a match it returns `{"decision": "block", "reason": "..."}` so the call never executes and the model sees the warning. Otherwise it stays silent and allows the call.
4. Fail-open by design: any error (missing transcript, parse failure, unexpected format) allows the call. A buggy guard must never brick your sessions.

## Requirements

- Codex CLI with hooks support (developed and live-tested against **codex-cli 0.167.0**; transcript/event formats may differ on other versions)
- Python 3 (standard library only)

## Install

1. Copy the script:

   ```sh
   mkdir -p ~/.codex/hooks
   cp loop_guard.py ~/.codex/hooks/loop_guard.py
   ```

2. Register it in `~/.codex/hooks.json` for **all** tools (see `examples/hooks.json`):

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "/usr/bin/python3 /Users/YOU/.codex/hooks/loop_guard.py"
             }
           ]
         }
       ]
     }
   }
   ```

   Replace the path with your real home directory. To guard only shell calls, add `"matcher": "Bash"` to the group.

3. **Trust the hook.** Restart Codex (or open `/hooks` in the TUI) and choose *Trust all and continue*. This step is not optional: Codex silently skips untrusted hooks, and the trust hashes it writes to `config.toml` (`hooks.state`) are computed by Codex itself — editing `hooks.json` afterwards re-triggers the review.

## Tuning

Constants at the top of `loop_guard.py`:

| Constant | Default | Meaning |
| --- | --- | --- |
| `REPEAT_COUNT` | `6` | Repetitions needed to trigger a block |
| `MAX_GROUP_SIZE` | `5` | Largest tool-group size checked |
| `SIMILARITY_THRESHOLD` | `0.9` | Argument similarity that still counts as "the same call" |
| `MAX_CALLS` | derived: `REPEAT_COUNT × MAX_GROUP_SIZE + 10` | How many recent calls are considered |

If you get false positives (e.g. legitimately listing six files whose names differ by one character), raise `SIMILARITY_THRESHOLD` or `REPEAT_COUNT`.

## Implementation notes (the hard-won parts)

Live integration testing against Codex 0.167.0 surfaced three quirks that any transcript-based hook must handle:

- **Two namespaces.** Hook events use friendly names and arguments — `tool_name: "Bash"`, `tool_input: {"command": "..."}` — while the transcript stores wire format — `exec_command`, `{"cmd": "...", "yield_time_ms": ...}`. The script normalizes both sides to the call's *primary payload string* (the `cmd` / `command` / `input` field of wrapper-shaped argument dicts) before comparing, or loop detection never matches anything.
- **The pending call is already in the transcript.** Codex writes the pending call's record *before* `PreToolUse` runs, and its output record only after execution. The script detects this ("last call still has no output") and does not append the pending call a second time. Naively appending duplicates the tail element, which silently breaks group (period ≥ 2) detection while single-tool detection keeps working — a nasty partial failure.
- **Trust hashes are per hook definition, matcher included.** The same script registered under two matchers needs two trust entries with different hashes. Let Codex write them via the TUI; don't hand-craft them.

## Limitations

- Relies on the 0.167.0 transcript/event formats; other Codex versions may need adjustments.
- Heavily parallel tool calls can reduce detection accuracy (records may not be flushed in call order when hooks run).
- Detection is purely syntactic — it cannot tell a stuck loop from a deliberate, legitimate repetition. The block reason tells the model to explain itself or change approach, and a changed call goes through.

## Tests

```sh
python3 tests/test_loop_guard.py
```

Covers single-tool and group repetition, fuzzy arguments, allow cases, and the pending-call append/recorded paths. The hook was additionally verified live: a model instructed to repeat one shell command was stopped at the 6th call, an `A/B/C` group was stopped at the 18th call, and identical `apply_patch` calls were stopped from the 6th.

## License

MIT

---

# 中文说明

一个无状态的 Codex CLI `PreToolUse` 钩子，专门打断工具调用死循环。

**检测规则**：同一个工具调用重复满 6 次，或者同一组工具序列（组大小 1–5，例如 A、B、C 循环）重复满 6 组，且参数相同或高度相似（≥ 90%），钩子就会拦下当前调用，并把一段警告作为拦截理由反馈给模型，让它停下来换方法。

**无状态实现**：钩子自己不存任何状态。每次触发时，它从 Codex 自带的会话记录（transcript）里读取最近的工具调用，加上当前待执行的调用一起判断。任何异常都默认放行，绝不因钩子出错卡住正常会话。

**安装**：把 `loop_guard.py` 放到 `~/.codex/hooks/`，按 `examples/hooks.json` 注册到 `~/.codex/hooks.json`，然后重启 Codex 并在界面里**信任**这条钩子（未受信任的钩子会被 Codex 静默跳过，这一步不能省）。

**调参**：脚本顶部的 `REPEAT_COUNT`（触发次数，默认 6）、`MAX_GROUP_SIZE`（组大小上限，默认 5）、`SIMILARITY_THRESHOLD`（参数相似度，默认 0.9）可按需调整。

开发与实测环境为 codex-cli 0.167.0。实现中踩过的三个坑（事件与记录的命名空间不一致、待执行调用会被预先写进记录导致组检测失效、信任哈希由 Codex 按定义逐条计算）都写在上面的 Implementation notes 里。

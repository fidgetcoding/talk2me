"""Pure-translation test for CodexBackend. Every input line below is VERBATIM
from the live protocol spike against codex-cli 0.144.6 (2026-07-19) — if
codex changes shape, this suite is the tripwire.

Run:  ./.venv/bin/python -m tests.test_codex_translate
"""

import json

from talk2me.backends.codex import CodexBackend
from talk2me.events import (
    AssistantTextDelta,
    ToolActivity,
    TurnComplete,
)

RESULTS: list[bool] = []


def _report(group: str, ok: bool) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {group}")
    RESULTS.append(ok)
    return ok


def _fresh(**kw) -> CodexBackend:
    return CodexBackend(**kw)


def main() -> int:
    # thread.started -> session id captured + callback fired, no events out.
    seen: list[str] = []
    b = _fresh(on_session=seen.append)
    got = b._translate(json.loads(
        '{"type":"thread.started","thread_id":"019f7c95-1d40-7302-81e4-dd69bddf0931"}'
    ))
    _report(
        "thread.started -> session recorded + callback",
        got == [] and b._session_id == "019f7c95-1d40-7302-81e4-dd69bddf0931"
        and seen == ["019f7c95-1d40-7302-81e4-dd69bddf0931"],
    )

    # agent_message -> spoken prose + rollup.
    got = b._translate(json.loads(
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}'
    ))
    _report("agent_message -> AssistantTextDelta", got == [AssistantTextDelta(text="OK")])

    # command_execution started -> early tool line; completed -> upgrade with
    # the output as the body.
    early = b._translate(json.loads(
        '{"type":"item.started","item":{"id":"item_1","type":"command_execution",'
        '"command":"/bin/zsh -lc \'echo hello\'","aggregated_output":"","exit_code":null,'
        '"status":"in_progress"}}'
    ))
    late = b._translate(json.loads(
        '{"type":"item.completed","item":{"id":"item_1","type":"command_execution",'
        '"command":"/bin/zsh -lc \'echo hello\'","aggregated_output":"hello\\n","exit_code":0,'
        '"status":"completed"}}'
    ))
    _report(
        "command_execution start -> tool, complete -> upgrade + output body",
        early == [ToolActivity(name="shell", summary="/bin/zsh -lc 'echo hello'")]
        and len(late) == 1
        and late[0].upgrade is True
        and late[0].body == "hello",
    )

    # turn.completed -> TurnComplete carrying the prose rollup.
    got = b._translate(json.loads(
        '{"type":"turn.completed","usage":{"input_tokens":14253,'
        '"cached_input_tokens":10496,"output_tokens":5,"reasoning_output_tokens":0}}'
    ))
    _report(
        "turn.completed -> TurnComplete(rollup)",
        got == [TurnComplete(text="OK")],
    )

    # Unknown shapes degrade to nothing.
    _report("unknown type -> []", b._translate({"type": "whatever"}) == [])

    # Resume argv shape + fresh argv shape.
    argv = _fresh(resume_session_id="tid-1", model="gpt-5.3-codex", cwd="/tmp")._argv()
    fresh_argv = _fresh()._argv()
    _report(
        "argv: parent flags precede resume (live-hit clap quirk)",
        argv.index("--sandbox") < argv.index("resume")
        and argv.index("-m") < argv.index("resume")
        and argv.index("-C") < argv.index("resume")
        and argv[argv.index("resume") + 1] == "tid-1"
        and argv.index("--json") > argv.index("resume")
        and argv[-1] == "-"
        and "resume" not in fresh_argv,
    )

    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

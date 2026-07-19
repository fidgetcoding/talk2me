"""Voice tool-approval gate, headless. No mic, no audio device, no LLM cost.

Covers: the spoken-intent grammar, the spoken phrasing, the backend's
control_request translation + control_response writing (wire shapes pinned in
docs/permission-spike-results.md), and the full orchestrator round-trip —
approve, deny, and unclear->re-ask->deny.

Run:  ./.venv/bin/python -m tests.test_permission
"""

import asyncio
import json

import numpy as np

from talk2me.backends import ClaudeCodeBackend
from talk2me.config import Config
from talk2me.events import (
    AssistantTextDelta,
    PermissionRequest,
    TurnComplete,
)
from talk2me.orchestrator import (
    Orchestrator,
    _drain_first_clause,
    _phrase_permission,
    match_intent,
)
from talk2me.vad import EnergyVAD

from .fakes import FakeBackend, FakeMic, FakeSpeaker, FakeSTT, FakeTTS

SR = 16000
FRAME = 480

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


def _speech(n):
    return [(np.random.randn(FRAME) * 0.2).astype(np.float32) for _ in range(n)]


def _silence(n):
    return [np.zeros(FRAME, dtype=np.float32) for _ in range(n)]


# ---- intent grammar ------------------------------------------------------


def test_intent() -> None:
    cases = [
        ("approve", "approve"),
        ("Yes, go ahead.", "approve"),
        ("do it", "approve"),
        ("okay sure", "approve"),
        ("no", "deny"),
        ("nope", "deny"),
        ("no, don't do it", "deny"),  # deny wins over the "do it" phrase
        ("stop stop stop", "deny"),
        ("banana", None),
        ("", None),
    ]
    for text, want in cases:
        got = match_intent(text)
        check(f"intent {text!r} -> {want}", got == want, f"got={got}")


# ---- spoken phrasing -----------------------------------------------------


def test_phrasing() -> None:
    bash = _phrase_permission(
        PermissionRequest("r1", "Bash", {"command": "npm install left-pad"})
    )
    check(
        "phrase Bash",
        bash == "Claude wants to run the command npm install left-pad. Approve or deny?",
        bash,
    )
    long_cmd = " ".join(f"w{i}" for i in range(20))
    truncated = _phrase_permission(PermissionRequest("r2", "Bash", {"command": long_cmd}))
    check("phrase Bash truncates >12 words", "and more" in truncated, truncated)
    write = _phrase_permission(
        PermissionRequest("r3", "Write", {"file_path": "/tmp/a/b/notes.md"})
    )
    check(
        "phrase Write basename",
        write == "Claude wants to create the file notes.md. Approve or deny?",
        write,
    )
    mcp = _phrase_permission(PermissionRequest("r4", "mcp__github__create_issue", {}))
    check(
        "phrase mcp server+tool",
        mcp == "Claude wants to use the github tool create_issue. Approve or deny?",
        mcp,
    )


# ---- backend translate + control_response wire ---------------------------


class _StubStdin:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    def write(self, b: bytes) -> None:
        self.lines.append(b)

    async def drain(self) -> None:
        pass


class _StubProc:
    def __init__(self) -> None:
        self.stdin = _StubStdin()


def test_translate() -> None:
    b = ClaudeCodeBackend()
    # The exact envelope captured live from CLI 2.1.214.
    pinned = {
        "type": "control_request",
        "request_id": "edf969bc",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "display_name": "Bash",
            "input": {"command": "touch x"},
            "permission_suggestions": [],
        },
    }
    evs = b._translate(pinned)
    ok = (
        len(evs) == 1
        and isinstance(evs[0], PermissionRequest)
        and evs[0].request_id == "edf969bc"
        and evs[0].tool_name == "Bash"
        and evs[0].tool_input == {"command": "touch x"}
    )
    check("translate pinned control_request", ok, str(evs))

    alt = {
        "type": "sdk_control_request",
        "request": {
            "subtype": "permission",
            "request_id": "p1",
            "tool": "Write",
            "tool_input": {"file_path": "a.txt"},
        },
    }
    evs = b._translate(alt)
    ok = (
        len(evs) == 1
        and evs[0].request_id == "p1"
        and evs[0].tool_name == "Write"
        and evs[0].tool_input == {"file_path": "a.txt"}
    )
    check("translate alt-spelling request", ok, str(evs))

    ignored = b._translate(
        {"type": "control_response", "response": {"subtype": "success"}}
    )
    check("control_response ignored", ignored == [])
    unknown = b._translate(
        {"type": "control_request", "request": {"subtype": "initialize"}}
    )
    check("unknown control subtype ignored", unknown == [])


async def test_respond_and_interrupt_wire() -> None:
    b = ClaudeCodeBackend()
    b._proc = _StubProc()

    await b.respond_permission("req-9", True)
    allow = json.loads(b._proc.stdin.lines[-1])
    ok = allow == {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": "req-9",
            "response": {"behavior": "allow"},
        },
    }
    check("respond_permission allow wire", ok, json.dumps(allow))

    await b.respond_permission("req-10", False)
    deny = json.loads(b._proc.stdin.lines[-1])
    inner = deny["response"]["response"]
    ok = (
        deny["response"]["request_id"] == "req-10"
        and inner["behavior"] == "deny"
        and inner["message"] == "Denied by voice"
    )
    check("respond_permission deny wire", ok, json.dumps(deny))

    await b.interrupt()
    intr = json.loads(b._proc.stdin.lines[-1])
    ok = (
        intr["type"] == "control_request"
        and intr["request"] == {"subtype": "interrupt"}
        and intr["request_id"].startswith("int_")
    )
    check("interrupt wire", ok, json.dumps(intr))


def test_argv_wiring() -> None:
    b = ClaudeCodeBackend(
        permission_prompt_stdio=True,
        allowed_tools=["Read", "Bash(ls:*)"],
        disallowed_tools=["Bash(sudo:*)"],
        setting_sources="project,local",
        append_system_prompt="voice persona",
    )
    argv = b._argv()
    joined = " ".join(argv)
    ok = (
        "--permission-prompt-tool stdio" in joined
        and "--allowedTools Read,Bash(ls:*)" in joined
        and "--disallowedTools Bash(sudo:*)" in joined
        and "--setting-sources project,local" in joined
        and "--append-system-prompt voice persona" in joined
    )
    check("argv wires stdio + tool lists + isolation", ok, joined)

    bare = ClaudeCodeBackend()._argv()
    bare_joined = " ".join(bare)
    ok = (
        "--permission-prompt-tool" not in bare_joined
        and "--allowedTools" not in bare_joined
        and "--setting-sources" not in bare_joined
        and "--append-system-prompt" not in bare_joined
    )
    check("argv omits gate + isolation when unset", ok)


def test_first_clause() -> None:
    cases = [
        # (input, expected clause or None)
        ("I checked the repository and found three issues, the first is in",
         "I checked the repository and found three issues,"),
        ("Sure, let me look at that for you", None),  # comma before 24 chars
        ("No boundary here so nothing should be emitted yet", None),
        ("Let me walk through the plan: first we update the config",
         "Let me walk through the plan:"),
    ]
    for text, want in cases:
        rest, clause = _drain_first_clause(text)
        ok = clause == want and (
            clause is None and rest == text
            or clause is not None and not rest.startswith(" ")
        )
        check(f"first-clause {text[:24]!r}…", ok, f"got={clause!r}")


# ---- orchestrator round-trips --------------------------------------------


def _perm_script(reply_after: str) -> list:
    return [
        AssistantTextDelta(text="Running tests now. "),
        PermissionRequest(
            request_id="req-1", tool_name="Bash", tool_input={"command": "pytest"}
        ),
        AssistantTextDelta(text=reply_after),
        TurnComplete(text=f"Running tests now. {reply_after}"),
    ]


async def _run_gate(transcripts: list[str], frames: list) -> tuple:
    cfg = Config(silence_ms=900, min_speech_ms=250)
    mic = FakeMic(frames, sample_rate=SR)
    stt = FakeSTT(transcripts)
    tts = FakeTTS()
    backend = FakeBackend(scripts=[_perm_script("Tests pass.")])
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=stt,
        tts=tts,
        mic=mic,
        speaker=FakeSpeaker(SR),
    )
    await asyncio.wait_for(orch.run(), timeout=10)
    return backend, tts, mic


async def test_gate_approve() -> None:
    # Utterance 1 = the user turn; utterance 2 = "approve" for the gate.
    frames = _speech(15) + _silence(35) + _speech(15) + _silence(35)
    backend, tts, mic = await _run_gate(["Run the tests.", "approve"], frames)
    check(
        "approve: responded allow",
        backend.permission_responses == [("req-1", True, None)],
        str(backend.permission_responses),
    )
    check(
        "approve: spoke prompt + both sentences",
        tts.spoken
        == [
            "Running tests now.",
            "Claude wants to run the command pytest. Approve or deny?",
            "Tests pass.",
        ],
        str(tts.spoken),
    )
    # Half-duplex contract across the gate: mute (speech) -> mute (prompt) ->
    # unmute (listen) -> mute (resume speech) -> unmute (turn end).
    check(
        "approve: mute cycle",
        mic.muted_log == [True, True, False, True, False],
        str(mic.muted_log),
    )


async def test_gate_deny() -> None:
    frames = _speech(15) + _silence(35) + _speech(15) + _silence(35)
    backend, tts, _ = await _run_gate(["Run the tests.", "no thanks"], frames)
    check(
        "deny: responded deny with message",
        backend.permission_responses == [("req-1", False, "Denied by voice")],
        str(backend.permission_responses),
    )


async def test_gate_unclear_then_deny() -> None:
    # Second listen finds the mic stream ended -> "" -> unclear -> deny.
    frames = _speech(15) + _silence(35) + _speech(15) + _silence(35)
    backend, tts, _ = await _run_gate(["Run the tests.", "banana"], frames)
    check(
        "unclear x2: denied",
        backend.permission_responses == [("req-1", False, "Denied by voice")],
        str(backend.permission_responses),
    )
    check(
        "unclear x2: re-asked once",
        "I didn't catch that — approve or deny?" in tts.spoken,
        str(tts.spoken),
    )


async def test_gate_unclear_then_approve() -> None:
    # Three utterances: turn, "banana" (unclear), "yes" (approve on re-ask).
    frames = (
        _speech(15) + _silence(35) + _speech(15) + _silence(35) + _speech(15) + _silence(35)
    )
    backend, tts, _ = await _run_gate(["Run the tests.", "banana", "yes"], frames)
    check(
        "unclear->approve: responded allow",
        backend.permission_responses == [("req-1", True, None)],
        str(backend.permission_responses),
    )


async def main() -> int:
    test_intent()
    test_phrasing()
    test_translate()
    await test_respond_and_interrupt_wire()
    test_argv_wiring()
    test_first_clause()
    await test_gate_approve()
    await test_gate_deny()
    await test_gate_unclear_then_deny()
    await test_gate_unclear_then_approve()

    passed = sum(1 for _, ok in RESULTS if ok)
    ok = passed == len(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks -> {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

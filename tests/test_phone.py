"""Phone-bridge protocol, headless: a real websockets server and a real
client on 127.0.0.1 play the part of the iPhone. What can't run here is the
actual Safari page — that's the live-phone test — but everything the Mac
side does is exercised for real: page serving, mic framing, source-side
muting, chunked playback with played-acks, and the barge flush.

Run:  ./.venv/bin/python -m tests.test_phone
"""

import asyncio
import contextlib
import http.client
import json
import struct

import numpy as np

from talk2me.phone import PhoneBridge, WebMic, WebSpeaker

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


async def _blocks(arrays):
    for a in arrays:
        yield a


async def _rough_network(bridge: PhoneBridge, port: int) -> None:
    """What a phone on a flaky tunnel really does: tears packets, drops, comes back."""
    import websockets

    url = f"ws://127.0.0.1:{port}/ws?t={bridge.token}"
    speaker = WebSpeaker(bridge, sample_rate=16000)

    async with websockets.connect(url) as ws:
        await asyncio.wait_for(bridge.wait_connected(), timeout=5)
        await ws.send(b"\x01\x02\x03")  # odd length: half a sample
        await ws.send("not json at all")
        await ws.send('{"type": "played", "id": "banana"}')
        await ws.send(json.dumps(["wrong", "shape"]))
        await ws.send((np.ones(480, dtype=np.int16) * 100).tobytes())
        frame = await asyncio.wait_for(WebMic(bridge, 480).frames().__anext__(), timeout=5)
        check("junk from the phone doesn't hang up", frame.shape == (480,))
        while not bridge.mic_queue.empty():
            bridge.mic_queue.get_nowait()

        # A phone that floods faster than we read: bounded, newest audio wins.
        for i in range(200):
            await ws.send((np.ones(160, dtype=np.int16) * i).tobytes())
        await asyncio.sleep(0.3)
        depth = bridge.mic_queue.qsize()
        newest = None
        while not bridge.mic_queue.empty():
            newest = bridge.mic_queue.get_nowait()
        check("mic flood stays bounded", depth <= 64, f"depth={depth}")
        check("mic flood keeps the newest audio", abs(float(newest[0]) - 199 / 32768) < 1e-5)

        # The tunnel drops mid-sentence: play() must let go, not hang the loop.
        play = asyncio.create_task(speaker.play(_blocks([np.zeros(1600, dtype=np.float32)])))
        await asyncio.wait_for(ws.recv(), timeout=5)
    played = await asyncio.wait_for(play, timeout=5)
    check("tunnel drop mid-playback releases play()", played is False)
    await asyncio.sleep(0.1)
    check("disconnect is noticed", bridge._ws is None)

    # Sending into no phone at all must not raise.
    check("playback with no phone doesn't wedge", await asyncio.wait_for(
        speaker.play(_blocks([np.zeros(160, dtype=np.float32)])), timeout=5) is False)

    async with websockets.connect(url) as ws:
        await asyncio.wait_for(bridge.wait_connected(), timeout=5)
        check("phone reconnects with the same link", True)

        # Safari reloading before the old socket dies: the newest one owns the bridge.
        async with websockets.connect(url) as second:
            await asyncio.sleep(0.1)
            play = asyncio.create_task(speaker.play(_blocks([np.zeros(160, dtype=np.float32)])))
            msg = await asyncio.wait_for(second.recv(), timeout=5)
            chunk_id, _rate = struct.unpack_from("<II", msg)
            await second.send(json.dumps({"type": "played", "id": chunk_id}))
            check("newest connection gets the audio", await asyncio.wait_for(play, timeout=5) is True)
        await asyncio.sleep(0.1)


async def _whole_loop() -> None:
    """The real orchestrator with the phone adapters, over a real socket: speech from
    the 'phone' becomes a turn, the reply comes back as audio, and talking over it
    flushes the page. The agent, ears and voice are fakes; the socket isn't."""
    import websockets

    from talk2me.config import Config
    from talk2me.events import AssistantTextDelta, TurnComplete
    from talk2me.orchestrator import Orchestrator
    from talk2me.vad import EnergyVAD

    from .fakes import FakeBackend, FakeSTT, FakeTTS

    bridge = PhoneBridge(port=0)
    await bridge.start()
    url = f"ws://127.0.0.1:{bridge.port}/ws?t={bridge.token}"
    backend = FakeBackend(scripts=[[AssistantTextDelta(text="Sure. It works."), TurnComplete(text="Sure. It works.")]])
    tts = FakeTTS()
    speech = (np.random.randn(480) * 0.2 * 32767).astype(np.int16).tobytes()
    silence = np.zeros(480, dtype=np.int16).tobytes()
    async with websockets.connect(url) as ws:
        await asyncio.wait_for(bridge.wait_connected(), timeout=5)
        orch = Orchestrator(
            cfg=Config(silence_ms=300, min_speech_ms=100, phone=True),
            backend=backend,
            vad=EnergyVAD(sample_rate=16000, frame_samples=480, threshold=0.012),
            stt=FakeSTT(["Run the tests."]),
            tts=tts,
            mic=WebMic(bridge, 480),
            speaker=WebSpeaker(bridge, tts.sample_rate),
        )
        run = asyncio.create_task(orch.run())
        await asyncio.sleep(0.2)
        for _ in range(20):
            await ws.send(speech)
            await asyncio.sleep(0.005)
        for _ in range(30):
            await ws.send(silence)
            await asyncio.sleep(0.005)
        acks = 0
        try:
            while acks < 2:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                if isinstance(msg, bytes):
                    chunk_id, _rate = struct.unpack_from("<II", msg)
                    await ws.send(json.dumps({"type": "played", "id": chunk_id}))
                    acks += 1
        except TimeoutError:
            pass
        await asyncio.sleep(0.2)
        check("phone speech became an agent turn", backend.sent == ["Run the tests."], str(backend.sent))
        check("the reply came back as audio", acks >= 2, f"{acks} chunks")
        run.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await run
    await bridge.close()


async def main() -> int:
    import websockets

    bridge = PhoneBridge(port=0)
    await bridge.start()
    port = bridge.port

    # --- the page is served over plain HTTP on the same port, token-gated ---
    def _get_page(path: str) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        return resp.status, body

    status, body = await asyncio.to_thread(_get_page, f"/?t={bridge.token}")
    check("page served with token", status == 200 and "talk2me" in body and "getUserMedia" in body)
    status_bad, _ = await asyncio.to_thread(_get_page, "/")
    check("page refused without token", status_bad == 403)
    # A junk token that isn't ASCII still earns a 403: compare_digest raises on
    # non-ASCII str, so this is what proves the check compares bytes.
    status_junk, _ = await asyncio.to_thread(_get_page, "/?t=%C3%A9%C3%A9")
    check("page refused with non-ASCII token", status_junk == 403)

    # A tokenless WebSocket is refused before the handshake completes.
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws"):
            check("ws refused without token", False, "connected?!")
    except Exception:  # noqa: BLE001  any refusal counts, the library's error type varies
        check("ws refused without token", True)

    async with websockets.connect(f"ws://127.0.0.1:{port}/ws?t={bridge.token}") as ws:
        await asyncio.wait_for(bridge.wait_connected(), timeout=5)
        check("phone connect detected", True)

        # --- mic path: int16 bytes in -> 480-sample float32 frames out ---
        mic = WebMic(bridge, frame_samples=480)
        pcm = (np.ones(1000, dtype=np.int16) * 16384).tobytes()  # ~0.5 amp
        await ws.send(pcm)
        frames_iter = mic.frames()
        frame1 = await asyncio.wait_for(anext(frames_iter), timeout=5)
        check(
            "mic frames sliced + scaled",
            frame1.shape == (480,) and abs(float(frame1[0]) - 0.5) < 0.01,
            f"shape={frame1.shape}",
        )
        frame2 = await asyncio.wait_for(anext(frames_iter), timeout=5)
        check("second frame from same packet", frame2.shape == (480,))

        # --- source-side muting: nothing is enqueued while muted ---
        mic.set_muted(True)
        await ws.send(pcm)
        await asyncio.sleep(0.2)
        check("muted mic discards at source", bridge.mic_queue.empty())
        mic.set_muted(False)

        # --- speaker path: chunk with header arrives; ack completes play ---
        speaker = WebSpeaker(bridge, sample_rate=22050)

        async def phone_side():
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            assert isinstance(msg, bytes)
            chunk_id, rate = struct.unpack_from("<II", msg)
            pcm_out = np.frombuffer(msg[8:], dtype="<i2")
            await ws.send(json.dumps({"type": "played", "id": chunk_id}))
            return chunk_id, rate, pcm_out

        play_task = asyncio.create_task(
            speaker.play(_blocks([np.ones(2205, dtype=np.float32) * 0.25]))
        )
        chunk_id, rate, pcm_out = await phone_side()
        played = await asyncio.wait_for(play_task, timeout=5)
        check(
            "playback chunk framed + acked",
            played is True and rate == 22050 and pcm_out.shape == (2205,)
            and abs(int(pcm_out[0]) - 8191) <= 1,
            f"id={chunk_id} rate={rate}",
        )

        # --- barge cut: stop() flushes the phone and aborts the wait ---
        play_task = asyncio.create_task(
            speaker.play(_blocks([np.zeros(2205, dtype=np.float32)]))
        )
        msg = await asyncio.wait_for(ws.recv(), timeout=5)  # the audio chunk
        speaker.stop()
        flush = await asyncio.wait_for(ws.recv(), timeout=5)
        played = await asyncio.wait_for(play_task, timeout=5)
        check(
            "stop() -> flush on the wire, play() aborts",
            isinstance(msg, bytes)
            and json.loads(flush)["type"] == "flush"
            and played is False,
        )

    await _rough_network(bridge, port)
    await bridge.close()
    check("bridge closes cleanly", True)
    await _whole_loop()

    passed = sum(1 for _, ok in RESULTS if ok)
    ok = passed == len(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks -> {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Phone-bridge protocol, headless: a real websockets server and a real
client on 127.0.0.1 play the part of the iPhone. What can't run here is the
actual Safari page — that's the live-phone test — but everything the Mac
side does is exercised for real: page serving, mic framing, source-side
muting, chunked playback with played-acks, and the barge flush.

Run:  ./.venv/bin/python -m tests.test_phone
"""

import asyncio
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


async def main() -> int:
    import websockets

    bridge = PhoneBridge(port=0)
    await bridge.start()
    port = bridge.port

    # --- the page is served over plain HTTP on the same port ---
    def _get_page() -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        return resp.status, body

    status, body = await asyncio.to_thread(_get_page)
    check("page served", status == 200 and "talk2me" in body and "getUserMedia" in body)

    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
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

    await bridge.close()
    check("bridge closes cleanly", True)

    passed = sum(1 for _, ok in RESULTS if ok)
    ok = passed == len(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks -> {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

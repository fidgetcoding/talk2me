"""Phone mode — your iPhone becomes the mic and speaker over SSH.

SSH carries keystrokes, not audio, so the audio takes its own ride: talk2me
serves a tiny web page + WebSocket on 127.0.0.1, and the SSH session's own
port-forward carries it to the phone (Blink: add `-L 8765:localhost:8765` to
the host). The phone opens http://localhost:8765 in Safari — localhost is a
secure context, so the mic works with zero certificates — taps once, and from
then on it streams mic audio down the tunnel and plays the agent's voice back.

The bridge implements the SAME Mic/Speaker seams the local hardware does, so
every loop mechanic — half-duplex muting, barge-in, pause, the permission
gate, the speech classifier — runs unchanged. Muting discards frames at the
source exactly like the local Mic; Speaker.stop() sends a flush the page
obeys instantly, which is what makes barge-in cuts feel immediate. Playback
completion is ack'd by the page, so play() returns when the PHONE finishes
speaking, not when the last byte was sent — half-duplex timing stays honest.

The iPhone's hardware echo cancellation (echoCancellation:true) is what makes
barge-in workable on the phone's own speaker — the phone already knows how to
not hear itself; it's been doing it on calls for two decades.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct

import numpy as np

# Wire format, mac -> phone: 4 bytes chunk id (LE u32) + 4 bytes sample rate
# (LE u32) + int16 mono PCM. phone -> mac: bare int16 mono PCM @ 16 kHz.
_HEADER = struct.Struct("<II")
PHONE_SAMPLE_RATE = 16000


class PhoneBridge:
    """One WebSocket client (the phone), reconnect-tolerant. Owns the queues
    both adapters below read/write."""

    def __init__(self, port: int = 8765) -> None:
        self.port = port
        self.mic_queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)
        self.mic_muted = False
        self._ws = None
        self._server = None
        self._connected = asyncio.Event()
        self._chunk_id = 0
        self._played_id = -1
        self._played_ev = asyncio.Event()
        self._flushed = False

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        from websockets.asyncio.server import serve

        self._server = await serve(
            self._handler,
            "127.0.0.1",  # tunnel-only by design: never exposed to the LAN
            self.port,
            process_request=self._http,
            max_size=2**22,
        )
        if self.port == 0:  # ephemeral (tests): learn the assigned port
            self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    def url(self) -> str:
        return f"http://localhost:{self.port}"

    async def wait_connected(self) -> None:
        await self._connected.wait()

    # ---- HTTP: serve the page --------------------------------------------

    def _http(self, connection, request):
        if request.path == "/ws":
            return None  # continue the WebSocket handshake
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        body = PAGE.encode()
        return Response(
            200,
            "OK",
            Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ]),
            body,
        )

    # ---- WebSocket -------------------------------------------------------

    async def _handler(self, ws) -> None:
        # A reload replaces the old connection; the newest phone wins.
        self._ws = ws
        self._connected.set()
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    if self.mic_muted:
                        continue  # discarded at the source, like the local Mic
                    pcm = np.frombuffer(message, dtype=np.int16)
                    frame = pcm.astype(np.float32) / 32768.0
                    try:
                        self.mic_queue.put_nowait(frame)
                    except asyncio.QueueFull:  # drop-oldest, like the local Mic
                        with contextlib.suppress(asyncio.QueueEmpty):
                            self.mic_queue.get_nowait()
                        with contextlib.suppress(asyncio.QueueFull):
                            self.mic_queue.put_nowait(frame)
                else:
                    self._on_control(message)
        finally:
            if self._ws is ws:
                self._ws = None
                self._connected.clear()

    def _on_control(self, message: str) -> None:
        import json

        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        if msg.get("type") == "played":
            try:
                self._played_id = max(self._played_id, int(msg.get("id", -1)))
            except (TypeError, ValueError):
                return
            self._played_ev.set()

    # ---- speaker side ----------------------------------------------------

    async def send_audio(self, block: np.ndarray, rate: int) -> int:
        """Ship one PCM block; returns its chunk id (for the played-ack)."""
        self._chunk_id += 1
        chunk_id = self._chunk_id
        pcm = (np.clip(block, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send(_HEADER.pack(chunk_id, rate) + pcm)
        return chunk_id

    async def wait_played(self, chunk_id: int, timeout: float = 60.0) -> bool:
        """Block until the phone reports that chunk finished playing (or a
        flush / disconnect / timeout gives up). Mirrors the local Speaker's
        blocking play() so half-duplex mic timing stays correct."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._played_id < chunk_id:
            if self._flushed or self._ws is None:
                return False
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return True  # never wedge the loop on a lost ack
            self._played_ev.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._played_ev.wait(), min(remaining, 1.0))
        return True

    def flush(self) -> None:
        """Cut playback NOW (barge-in): tell the page to kill queued audio."""
        self._flushed = True
        self._played_ev.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(
                    self._send_flush(ws)
                )

    @staticmethod
    async def _send_flush(ws) -> None:
        with contextlib.suppress(Exception):
            await ws.send('{"type": "flush"}')

    def reset_flush(self) -> None:
        self._flushed = False


class WebMic:
    """The phone's microphone behind the local Mic's exact interface."""

    def __init__(self, bridge: PhoneBridge, frame_samples: int) -> None:
        self._bridge = bridge
        self.sample_rate = PHONE_SAMPLE_RATE
        self.frame_samples = frame_samples
        self._buf = np.zeros(0, dtype=np.float32)
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def set_muted(self, muted: bool) -> None:
        self._bridge.mic_muted = muted

    async def frames(self):
        while True:
            chunk = await self._bridge.mic_queue.get()
            self._buf = np.concatenate([self._buf, chunk])
            while self._buf.shape[0] >= self.frame_samples:
                frame = self._buf[: self.frame_samples]
                self._buf = self._buf[self.frame_samples :]
                yield frame


class WebSpeaker:
    """The phone's speaker behind the local Speaker's exact interface."""

    def __init__(self, bridge: PhoneBridge, sample_rate: int) -> None:
        self._bridge = bridge
        self.sample_rate = sample_rate

    async def play(self, blocks) -> bool:
        self._bridge.reset_flush()
        last_id: int | None = None
        async for block in blocks:
            if self._bridge._flushed:
                return False
            last_id = await self._bridge.send_audio(block, self.sample_rate)
        if last_id is None:
            return True
        return await self._bridge.wait_played(last_id)

    def stop(self) -> None:
        self._bridge.flush()


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>talk2me</title>
<style>
  body { background: #000; color: #a8dd00; font-family: ui-monospace, Menlo,
         monospace; display: flex; flex-direction: column; align-items: center;
         justify-content: center; min-height: 90vh; margin: 0; }
  h1 { font-size: 2.2rem; letter-spacing: .06em; }
  h1 span { color: #ff5fd7; }
  button { background: #000; color: #a8dd00; border: 2px dashed #a8dd00;
           font: inherit; font-size: 1.4rem; padding: 1rem 2rem;
           border-radius: 6px; }
  #st { margin-top: 1.5rem; color: #5fd7ff; min-height: 1.4em; }
  .quiet { color: #666; font-size: .85rem; margin-top: 2rem; text-align: center;
           padding: 0 1.5rem; }
</style></head><body>
<h1>talk2me<span>.</span></h1>
<button id="go">&#9654;&#xFE0E; tap to connect</button>
<div id="st"></div>
<p class="quiet">keep this page open — it is the microphone and the speaker.
The conversation shows in your SSH terminal.</p>
<script>
const st = document.getElementById('st');
const go = document.getElementById('go');
go.onclick = async () => {
  go.disabled = true;
  let ctx, stream;
  try {
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    await ctx.resume();
    stream = await navigator.mediaDevices.getUserMedia({ audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true
    }});
  } catch (e) { st.textContent = 'mic blocked: ' + e; go.disabled = false; return; }
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://')
                           + location.host + '/ws');
  ws.binaryType = 'arraybuffer';

  // --- capture: downsample to 16 kHz int16 and ship ---
  const src = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  const sink = ctx.createGain(); sink.gain.value = 0;  // keep proc alive, silently
  src.connect(proc); proc.connect(sink); sink.connect(ctx.destination);
  proc.onaudioprocess = (e) => {
    if (ws.readyState !== 1) return;
    const d = e.inputBuffer.getChannelData(0);
    const ratio = ctx.sampleRate / 16000;
    const n = Math.floor(d.length / ratio);
    const out = new Int16Array(n);
    for (let i = 0; i < n; i++) {
      const v = Math.max(-1, Math.min(1, d[Math.floor(i * ratio)]));
      out[i] = v * 32767;
    }
    ws.send(out.buffer);
  };

  // --- playback: scheduled PCM chunks, ack on finish, flush on command ---
  let playT = 0, live = [];
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === 'flush') {
        live.forEach(s => { try { s.stop(); } catch {} });
        live = []; playT = ctx.currentTime;
        st.textContent = 'connected — talk';
      }
      return;
    }
    const dv = new DataView(ev.data);
    const id = dv.getUint32(0, true), rate = dv.getUint32(4, true);
    const pcm = new Int16Array(ev.data, 8);
    const buf = ctx.createBuffer(1, pcm.length, rate);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
    const s = ctx.createBufferSource();
    s.buffer = buf; s.connect(ctx.destination);
    const at = Math.max(ctx.currentTime, playT);
    s.start(at); playT = at + buf.duration; live.push(s);
    st.textContent = 'speaking…';
    s.onended = () => {
      live = live.filter(x => x !== s);
      if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'played', id }));
      if (live.length === 0) st.textContent = 'connected — talk';
    };
  };
  ws.onopen = () => { st.textContent = 'connected — talk'; };
  ws.onclose = () => { st.textContent = 'disconnected — reload the page'; go.disabled = false; };
};
</script></body></html>
"""

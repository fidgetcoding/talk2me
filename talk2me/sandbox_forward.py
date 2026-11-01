"""Runs INSIDE the Linux sandbox, ahead of the agent: `sandbox_forward.py SOCK PORT -- cmd...`

A private network namespace has only its own loopback, so the proxy outside is
reachable only through the Unix socket bound in. This listens on that loopback,
pipes to the socket, and runs the command. Standard library only, and it stays a
plain script so it can start without importing talk2me.
"""

import signal
import socket
import subprocess
import sys
import threading


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _serve(listener: socket.socket, path: str) -> None:
    while True:
        client, _ = listener.accept()
        upstream = socket.socket(socket.AF_UNIX)
        try:
            upstream.connect(path)
        except OSError:
            client.close()
            continue
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()


def main() -> int:
    path, port = sys.argv[1], int(sys.argv[2])
    command = sys.argv[4:]
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(64)
    threading.Thread(target=_serve, args=(listener, path), daemon=True).start()
    child = subprocess.Popen(command)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda s, _f: child.send_signal(s))
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())

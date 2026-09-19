"""Publish server files to a connected badge over its serial console.

The badge is not a USB storage drive. Its console accepts a narrow transfer
handshake: ``put PATH BYTES`` -> ``READY`` -> raw file bytes -> ``OK BYTES``.
The normal command writes one app's inbox.tmp and inbox.ready, in that order.
The end-to-end capture test also uses its safe app-file helper for sprite assets.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import serial
    from serial.tools import list_ports
except ImportError as exc:  # A clearer message than an import traceback.
    raise SystemExit("Install dependencies first: py -m pip install -r requirements.txt") from exc


REVISION_RE = re.compile(r"(?:^|\n)revision=(\d+)(?:\n|$)")
SLUG_RE = re.compile(r"^[a-z0-9_-]{1,48}$")
FILE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")


class BadgeProtocolError(RuntimeError):
    pass


def http_text(url: str, method: str = "GET", body: dict[str, object] | None = None) -> str:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Server returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach server: {exc.reason}") from exc


def revision_from(text: str) -> int:
    match = REVISION_RE.search(text)
    if not match:
        raise BadgeProtocolError("Server response did not include a revision line.")
    return int(match.group(1))


def fetch_snapshot(base: str, prefix: str) -> tuple[str, str]:
    return (
        http_text(f"{base}{prefix}/badge/inbox"),
        http_text(f"{base}{prefix}/badge/ready"),
    )


class BadgeSerialBridge:
    def __init__(self, port_name: str, slug: str, baudrate: int = 115200, raw_delay: float = 0.5) -> None:
        if not SLUG_RE.fullmatch(slug):
            raise BadgeProtocolError("Badge app slug contains unsupported characters.")
        self.port_name = port_name
        self.slug = slug
        self.baudrate = baudrate
        self.raw_delay = raw_delay
        self.port: serial.Serial | None = None

    def __enter__(self) -> "BadgeSerialBridge":
        # Construct before opening so pyserial does not assert DTR/RTS during
        # open. Some ESP32 USB-serial drivers treat that transition as reset.
        self.port = serial.Serial(port=None, baudrate=self.baudrate, timeout=0.1, write_timeout=5)
        self.port.dtr = False
        self.port.rts = False
        self.port.port = self.port_name
        self.port.open()
        # ESP boot output can include a premature "badge>" before the actual
        # console task exists.  Do not send a raw transfer until that task has
        # announced itself; otherwise its first bytes can be discarded.
        self._wait_for_console_start(10)
        self._drain_boot_output()
        # Ask the now-live console for a fresh prompt.
        self._write(b"\n")
        self._wait_for("badge>", 8)
        return self

    def __exit__(self, *_: object) -> None:
        if self.port is not None:
            self.port.close()

    def _write(self, data: bytes) -> None:
        if self.port is None:
            raise BadgeProtocolError("Serial port is not open.")
        # USB serial writes can complete in short chunks. Do not assume one
        # write() call queued every byte of an inbox snapshot.
        offset = 0
        while offset < len(data):
            sent = self.port.write(data[offset:])
            if not sent:
                raise BadgeProtocolError("Serial write returned zero bytes.")
            offset += sent
        self.port.flush()

    def _drain_boot_output(self) -> None:
        if self.port is None:
            raise BadgeProtocolError("Serial port is not open.")
        quiet_until = time.monotonic() + 0.35
        while time.monotonic() < quiet_until:
            chunk = self.port.read(self.port.in_waiting or 1)
            if chunk:
                quiet_until = time.monotonic() + 0.35
            else:
                time.sleep(0.02)

    def _write_raw_file(self, payload: bytes) -> None:
        """Feed the small native-USB console buffer without overflowing it."""
        for start in range(0, len(payload), 16):
            self._write(payload[start : start + 16])
            time.sleep(0.02)

    def _wait_for_console_start(self, timeout_seconds: float) -> None:
        """Wait through a reset, but also allow an already-running console."""
        if self.port is None:
            raise BadgeProtocolError("Serial port is not open.")
        deadline = time.monotonic() + timeout_seconds
        received = bytearray()
        while time.monotonic() < deadline:
            chunk = self.port.read(self.port.in_waiting or 1)
            if chunk:
                received.extend(chunk)
                if b"hal_console: console started" in received:
                    return
            else:
                time.sleep(0.02)
        # If opening the port did not reset the ESP, no boot marker is
        # expected. A fresh prompt below remains a valid readiness test.

    def _wait_for(self, marker: str, timeout_seconds: float) -> str:
        if self.port is None:
            raise BadgeProtocolError("Serial port is not open.")
        deadline = time.monotonic() + timeout_seconds
        received = bytearray()
        while time.monotonic() < deadline:
            chunk = self.port.read(self.port.in_waiting or 1)
            if chunk:
                received.extend(chunk)
                text = received.decode("utf-8", errors="replace")
                if marker in text:
                    return text
            else:
                time.sleep(0.02)
        tail = received.decode("utf-8", errors="replace")[-500:]
        raise BadgeProtocolError(f"Timed out waiting for {marker!r}. Recent serial output:\n{tail}")

    def put_app_file(self, slug: str, file_name: str, contents: str | bytes) -> None:
        """Write one safe relative file to an installed app directory."""

        if not SLUG_RE.fullmatch(slug):
            raise BadgeProtocolError("Badge app slug contains unsupported characters.")
        if not FILE_NAME_RE.fullmatch(file_name):
            raise BadgeProtocolError("Badge file name contains unsupported characters.")
        payload = contents.encode("utf-8") if isinstance(contents, str) else contents
        path = f"/littlefs/apps/{slug}/{file_name}"
        self._write(f"put {path} {len(payload)}\n".encode("ascii"))
        self._wait_for("READY", 8)
        # The ESP console prints READY before the filesystem task has fully
        # switched from line input to its raw-byte receiver.  A half-second
        # handoff is deliberately conservative; sending immediately can lose
        # the start of the file and leaves the badge waiting forever.
        time.sleep(self.raw_delay)
        self._write_raw_file(payload)
        # Native USB/JTAG can acknowledge slowly while the filesystem flushes.
        self._wait_for(f"OK {len(payload)}", 35)

    def put_inbox_file(self, file_name: str, contents: str) -> None:
        if file_name not in {"inbox.tmp", "inbox.ready"}:
            raise BadgeProtocolError("This bridge may write only inbox.tmp and inbox.ready.")
        self.put_app_file(self.slug, file_name, contents)

    def publish(self, inbox: str, ready: str) -> int:
        inbox_revision = revision_from(inbox)
        ready_revision = revision_from(ready)
        if inbox_revision != ready_revision:
            raise BadgeProtocolError(
                f"Refusing mismatched snapshot: inbox={inbox_revision}, ready={ready_revision}."
            )
        self.put_inbox_file("inbox.tmp", inbox)
        self.put_inbox_file("inbox.ready", ready)
        return inbox_revision


def list_available_ports() -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return
    for port in ports:
        print(f"{port.device}  {port.description}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a PokeLife world snapshot to one badge.")
    parser.add_argument("--port", help="Windows COM port for the ESP32 USB JTAG/serial device")
    parser.add_argument("--slug", default="pokedex", help="Badge app slug (default: pokedex)")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="FastAPI base URL")
    parser.add_argument("--live", action="store_true", help="Publish the real world instead of the isolated test world")
    parser.add_argument("--run-test", action="store_true", help="Reset dummy data and run one simulation tick before publishing")
    parser.add_argument("--no-jev", action="store_true", help="Use deterministic simulation rules for --run-test")
    parser.add_argument("--raw-delay", type=float, default=0.5, help="Seconds to wait after READY (default: 0.5)")
    parser.add_argument("--watch", action="store_true", help="Poll the server and publish each newer revision until interrupted")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between server polls in --watch mode")
    parser.add_argument("--list-ports", action="store_true", help="List serial ports and exit")
    args = parser.parse_args()

    if args.list_ports:
        list_available_ports()
        return 0
    if not args.port:
        parser.error("--port is required unless --list-ports is used")
    if args.live and args.run_test:
        parser.error("--run-test cannot be combined with --live")

    base = args.server.rstrip("/")
    if args.run_test:
        result = json.loads(
            http_text(
                f"{base}/simulation/test/run",
                method="POST",
                body={"prefer_jev": not args.no_jev},
            )
        )
        print(
            "Test tick: "
            f"director={result['director_used']} "
            f"backboard_verified={result['backboard_verified']}"
        )
        if result.get("director_note"):
            print(f"Note: {result['director_note']}")

    prefix = "" if args.live else "/simulation/test"
    if args.raw_delay < 0 or args.interval <= 0:
        parser.error("--raw-delay must be zero or greater and --interval must be positive")
    with BadgeSerialBridge(args.port, args.slug, raw_delay=args.raw_delay) as bridge:
        published = -1
        while True:
            inbox, ready = fetch_snapshot(base, prefix)
            candidate = revision_from(ready)
            if candidate > published:
                revision = bridge.publish(inbox, ready)
                published = revision
                print(f"Published revision {revision} to {args.slug}/inbox.tmp and inbox.ready.")
            if not args.watch:
                break
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BadgeProtocolError, RuntimeError, serial.SerialException) as exc:
        print(f"Bridge failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

"""
capture_receiver.py -- listens to the ESP32-S3 cam over USB serial and writes
every photo it sends into a folder on this machine.

    pip install pyserial
    python capture_receiver.py /dev/ttyACM0          # Linux
    python capture_receiver.py /dev/cu.usbmodem1101  # macOS
    python capture_receiver.py COM5                  # Windows

Photos land in ./captures/ as 20260919-143012-photo_0003.jpg

Options:
    --out DIR        where to save (default: captures)
    --baud N         must match SERIAL_BAUD in the sketch (default 921600)
    --post URL       also POST each photo to an HTTP endpoint (needs `requests`)
    --list           list available serial ports and exit

Press Enter at any time to trigger a capture.
"""

import argparse
import os
import sys
import threading
import time
from datetime import datetime

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is not installed. Run:  pip install pyserial")


def read_exact(ser, n, timeout=30.0):
    """Serial reads can come back short; keep going until we have all n bytes."""
    buf = bytearray()
    deadline = time.time() + timeout
    while len(buf) < n and time.time() < deadline:
        chunk = ser.read(n - len(buf))
        if chunk:
            buf.extend(chunk)
    return bytes(buf)


def stdin_trigger(ser, stop):
    """Press Enter in the terminal to fire a capture.

    Uses readline() rather than `for line in sys.stdin`: iterating a file
    object uses read-ahead buffering, which on a terminal can swallow the
    line instead of delivering it immediately.
    """
    while not stop.is_set():
        try:
            line = sys.stdin.readline()
        except Exception:
            return
        if line == "":          # EOF (stdin closed / piped input exhausted)
            return
        if stop.is_set():
            return
        try:
            ser.write(b"c")
            ser.flush()
        except Exception:
            return


def post_photo(url, path, data):
    try:
        import requests
    except ImportError:
        print("  [post] `requests` not installed, skipping upload")
        return
    try:
        r = requests.post(
            url,
            # FastAPI's capture endpoint names its required multipart part
            # "image". Keep this identical to end_to_end_test.py so a real
            # Pokeball capture and a stored test capture take the same ingress.
            files={"image": (os.path.basename(path), data, "image/jpeg")},
            data={"device_id": "pokeball-01"},
            timeout=30,
        )
        print(f"  [post] {url} -> {r.status_code}")
    except Exception as e:
        print(f"  [post] failed: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", help="serial port of the ESP32")
    ap.add_argument("--out", default="captures")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--post", default=None, help="HTTP endpoint to POST photos to")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    args = ap.parse_args()

    if args.list or not args.port:
        ports = list(list_ports.comports())
        if not ports:
            print("No serial ports found. Is the board plugged into the UART port?")
        for p in ports:
            print(f"{p.device:24} {p.description}")
        return

    os.makedirs(args.out, exist_ok=True)

    # Configure DTR/RTS *before* opening. Opening the port still resets the
    # board via the auto-reset circuit, which is why the sketch ignores
    # triggers for the first moment after boot.
    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = args.baud
    ser.timeout = 1.0
    ser.dtr = False
    ser.rts = False
    ser.open()

    # Let the board finish rebooting, then drop whatever it said on the way up.
    time.sleep(2.0)
    ser.reset_input_buffer()

    print(f"Listening on {args.port} @ {args.baud}")
    print(f"Saving to {os.path.abspath(args.out)}/")
    print("Press Enter to capture, Ctrl-C to quit.\n")

    stop = threading.Event()
    threading.Thread(target=stdin_trigger, args=(ser, stop), daemon=True).start()

    count = 0
    try:
        while True:
            line = ser.readline()
            if not line:
                continue

            if line.startswith(b"IMG:"):
                try:
                    _, length_s, name = line.strip().split(b":", 2)
                    length = int(length_s)
                    name = name.decode("ascii", "replace")
                except ValueError:
                    print(f"! malformed header: {line!r}")
                    continue

                data = read_exact(ser, length)
                if len(data) != length:
                    print(f"! truncated image: got {len(data)} of {length} bytes")
                    continue
                if not data.startswith(b"\xff\xd8"):
                    print("! not a JPEG (bad framing) -- try a lower --baud")
                    continue

                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                path = os.path.join(args.out, f"{stamp}-{name}")
                with open(path, "wb") as f:
                    f.write(data)

                count += 1
                print(f"[{count}] saved {path}  ({length/1024:.1f} KB)")
                if args.post:
                    post_photo(args.post, path, data)

            elif line.startswith(b"LOG:"):
                print("  " + line[4:].decode("utf-8", "replace").rstrip())
            else:
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    print("  " + text)

    except KeyboardInterrupt:
        print(f"\nDone. {count} photo(s) saved to {os.path.abspath(args.out)}/")
    finally:
        stop.set()
        ser.close()


if __name__ == "__main__":
    main()

"""Bounded startup readiness for the trial's two loopback listeners."""

import socket
import time

deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        for port in (9223, 8790):
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        raise SystemExit(0)
    except OSError:
        time.sleep(1)
raise SystemExit("Marketplace browser/MCP did not bind within 60 seconds")

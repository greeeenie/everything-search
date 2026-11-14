import argparse
import json
import socket

parser = argparse.ArgumentParser()
parser.add_argument("--pi", required=True)
parser.add_argument("--vps", required=True)
args = parser.parse_args()
for host, ports in ((args.pi, (8765, 8770, 8088, 10808)), (args.vps, (8765, 8766))):
    for port in ports:
        with socket.socket() as client:
            client.settimeout(2)
            try:
                connected = client.connect_ex((host, port)) == 0
                if connected:
                    path = "/" if port == 8088 else "/health"
                    request = (
                        b"\x05\x01\x00"
                        if port == 10808
                        else (
                            "GET " + path + " HTTP/1.1\r\nHost: " + host + "\r\nConnection: close\r\n\r\n"
                        ).encode()
                    )
                    client.sendall(request)
                    reply = bytearray()
                    while len(reply) < 16384:
                        try:
                            chunk = client.recv(4096)
                        except socket.timeout:
                            break
                        if not chunk:
                            break
                        reply.extend(chunk)
                        if port == 10808:
                            break
                    reachable = (
                        reply.startswith(b"\x05\x00")
                        if port == 10808
                        else (
                            b"research-v2" in reply
                            or b"searx" in reply.lower()
                            or (port == 8770 and b'"status":"ok"' in reply)
                        )
                    )
                else:
                    reachable = False
            except OSError:
                reachable = False
            print(json.dumps({"host": host, "port": port, "externally_reachable": reachable}))
            if reachable:
                raise SystemExit("A private Research port is reachable externally")

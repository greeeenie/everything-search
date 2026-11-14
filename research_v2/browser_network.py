"""Short-lived loopback CONNECT proxy for Chromium's own HTTPS stack.

DNS is checked and pinned before opening each tunnel. Browser requests retain
Chromium TLS while sharing the fixed regional exits used by ordinary fetching.
"""

import asyncio
import ipaddress
from urllib.parse import urlsplit

from .network import AccessError, EgressRouter, host_matches, resolve_public, validate_url


async def connect_socks(proxy, ip, port):
    endpoint = urlsplit(proxy)
    reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        if await reader.readexactly(2) != b"\x05\x00":
            raise AccessError("browser_proxy_unavailable")
        address = ipaddress.ip_address(ip)
        kind = b"\x01" if address.version == 4 else b"\x04"
        writer.write(b"\x05\x01\x00" + kind + address.packed + port.to_bytes(2, "big"))
        await writer.drain()
        reply = await reader.readexactly(4)
        if reply[:2] != b"\x05\x00":
            raise AccessError("browser_proxy_unavailable")
        lengths = {1: 4, 4: 16}
        length = lengths.get(reply[3])
        if reply[3] == 3:
            length = (await reader.readexactly(1))[0]
        if length is None:
            raise AccessError("browser_proxy_unavailable")
        await reader.readexactly(length + 2)
        return reader, writer
    except BaseException:
        writer.close()
        await writer.wait_closed()
        raise


class BrowserNetwork:
    def __init__(self, settings, region, domains, *, resolver=resolve_public):
        self.settings = settings
        self.region = region
        self.domains = domains
        self.resolver = resolver
        self.server = None
        self.tasks = set()
        self.connections = asyncio.Semaphore(16)
        self.transferred = 0
        self.limit = settings.max_bytes * 6

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0, limit=8192)
        self.url = "http://127.0.0.1:" + str(self.server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def destination(self, authority):
        parsed, host = validate_url("https://" + authority + "/")
        if parsed.port != 443 or not any(host_matches(host, domain) for domain in self.domains):
            raise AccessError("browser_destination_denied")
        if any(host_matches(host, domain) for domain in self.settings.blocked_domains):
            raise AccessError("source_policy_denied")
        return await self.resolver(host, 443)

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        try:
            async with self.connections, asyncio.timeout(45):
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
                method, authority, version = header.split(b"\r\n", 1)[0].decode("ascii").split(" ")
                if method != "CONNECT" or version != "HTTP/1.1":
                    raise AccessError("browser_connect_required")
                ip = await self.destination(authority)
                proxy = EgressRouter(self.settings).proxy(self.region)
                if proxy:
                    remote, upstream = await connect_socks(proxy, ip, 443)
                else:
                    remote, upstream = await asyncio.open_connection(ip, 443)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

                async def copy(source, target):
                    while chunk := await source.read(65536):
                        self.transferred += len(chunk)
                        if self.transferred > self.limit:
                            raise AccessError("browser_transfer_limit")
                        target.write(chunk)
                        await target.drain()

                # End both directions as soon as either side closes or fails.
                copies = [
                    asyncio.create_task(copy(reader, upstream)),
                    asyncio.create_task(copy(remote, writer)),
                ]
                try:
                    await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for copy_task in copies:
                        copy_task.cancel()
                    await asyncio.gather(*copies, return_exceptions=True)
        except (
            AccessError,
            OSError,
            ValueError,
            UnicodeError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            writer.close()
            if upstream:
                upstream.close()
            self.tasks.discard(task)

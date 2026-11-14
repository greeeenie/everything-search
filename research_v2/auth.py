import os
import secrets

import uvicorn


class BearerGate:
    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/health":
            headers = dict(scope.get("headers", []))
            supplied = headers.get(b"authorization", b"").decode("latin1")
            if not secrets.compare_digest(supplied, "Bearer " + self.token):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def serve_http(mcp, port):
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(request):
        return JSONResponse({"status": "ok", "service": "research-v2"})

    app = mcp.streamable_http_app()
    app.routes.append(Route("/health", health))
    token = os.getenv("RESEARCH_MCP_TOKEN", "")
    trusted = os.getenv("RESEARCH_TRUSTED_TUNNEL", "") == "1"
    if not token and not trusted:
        raise RuntimeError(
            "Set RESEARCH_MCP_TOKEN or explicitly enable the authenticated Secure MCP Tunnel deployment"
        )
    if token:
        app = BearerGate(app, token)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)

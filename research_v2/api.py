from dataclasses import dataclass
from html import escape
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

import community_sources

from .engine import get_engine
from .tools import Limit, Region

SERVICE = "research-v2"
try:
    PACKAGE = distribution(SERVICE)
except PackageNotFoundError:
    PACKAGE = None
VERSION = PACKAGE.version if PACKAGE else "2.0.0"
SKILL_PATH = Path(__file__).resolve().parents[1] / "docs" / "SKILL.md"
Query = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Condition = Literal["new", "used", "refurbished"]
Price = Annotated[float, Field(ge=0, allow_inf_nan=False)]
ShortLimit = Annotated[int, Field(ge=1, le=5)]
HTTP_URL = TypeAdapter(AnyHttpUrl)


def validate_url(value):
    parsed = HTTP_URL.validate_python(value)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Use a URL without embedded credentials.")
    return value


Url = Annotated[str, Field(json_schema_extra={"format": "uri"}), AfterValidator(validate_url)]


class ApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductCompare(ApiRequest):
    model: Query
    marketplaces: list[str] | None = None
    city: str | None = None
    condition: Condition | None = None
    max_results: Limit = 12
    region: Region = None


class ProductSearch(ProductCompare):
    query: Query
    model: Query | None = None
    max_results: Limit = 9
    min_price: Price | None = None
    max_price: Price | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")] | None = None
    specifications: dict[str, str] | None = None

    @model_validator(mode="after")
    def price_range(self):
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("min_price must be less than or equal to max_price.")
        return self


class ProductDetails(ApiRequest):
    url: Url | None = None
    model: Query | None = None
    region: Region = None

    @model_validator(mode="after")
    def require_identifier(self):
        if self.url is None and self.model is None:
            raise ValueError("Provide a product URL or an exact model.")
        return self


class ProductReviews(ApiRequest):
    model: Query
    domains: list[str] | None = None
    max_results: Limit = 8
    region: Region = None


class WebSearch(ApiRequest):
    query: Query
    site: Annotated[str, StringConstraints(
        to_lower=True, max_length=253, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,63}$",
    )] | None = None
    max_results: ShortLimit = 4
    region: Region = None


class WebRead(ApiRequest):
    url: Url
    region: Region = None
    dynamic: bool = False
    refresh: bool = False


class Research(ApiRequest):
    query: Query
    max_threads: ShortLimit = 4
    mode: Literal["quick", "deep"] | None = None
    domains: list[str] | None = None
    model: str | None = None
    city: str | None = None


class HackerNewsSearch(ApiRequest):
    query: Query
    max_results: Annotated[int, Field(ge=1, le=10)] = 6


class GithubSearch(ApiRequest):
    query: Query
    repository: Annotated[str, Field(max_length=150, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")] | None = None
    max_results: ShortLimit = 4


@dataclass(frozen=True)
class Operation:
    path: str
    name: str
    summary: str
    model: type[ApiRequest]
    source: Literal["engine", "server", "community"] = "engine"

    async def handle(self, request):
        try:
            arguments = self.model.model_validate_json(await request.body()).model_dump()
        except ValidationError as error:
            return JSONResponse(
                {"detail": error.errors(include_input=False, include_context=False, include_url=False)},
                status_code=422,
            )
        if self.source == "engine":
            provider = get_engine()
        elif self.source == "server":
            import server as provider
        else:
            provider = community_sources
        target = getattr(provider, self.name)
        return JSONResponse(await target(**arguments))


OPERATIONS = (
    Operation("/products/search", "search_products", "Find verified public product offers", ProductSearch),
    Operation("/products/compare", "compare_prices", "Compare offers for an exact model", ProductCompare),
    Operation("/products/details", "get_product_details", "Read product specifications and evidence", ProductDetails),
    Operation("/products/reviews", "get_product_reviews", "Find buyer reviews for an exact model", ProductReviews),
    Operation("/web/search", "search_web", "Search and read public web pages", WebSearch),
    Operation("/web/read", "read_page", "Read a public web page", WebRead),
    Operation("/research", "research", "Research a question across sources", Research, source="server"),
    Operation("/community/hn", "search_hacker_news", "Search Hacker News stories and comments", HackerNewsSearch,
              source="community"),
    Operation("/community/github", "search_github_issues", "Search public GitHub issues and comments", GithubSearch,
              source="community"),
)
API_PREFIX = "/api/v1"
STATUS_PATH = API_PREFIX + "/status"
DISCOVERY = {
    "/mcp": "MCP Streamable HTTP",
    STATUS_PATH: "REST API status",
    "/openapi.json": "OpenAPI 3.1 schema",
    "/skill.md": "Agent skill",
    "/llms.txt": "Agent endpoint directory",
}


def operation_schema(name, summary):
    return {
        "operationId": name,
        "summary": summary,
        "x-openai-isConsequential": False,
        "responses": {
            "200": {
                "description": "Successful result",
                "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}},
            },
            "401": {"description": "Missing or invalid credentials"},
        },
    }


def openapi_document(base_url, *, auth_mode: Literal["token", "none"] = "token"):
    status = operation_schema("get_status", "Show available marketplaces and egress diagnostics")
    status["parameters"] = [{
        "name": "quick", "in": "query", "required": False,
        "description": "Set to 1 to skip live egress probes.",
        "schema": {"type": "string", "enum": ["0", "1"], "default": "0"},
    }]
    paths = {STATUS_PATH: {"get": status}}
    models = {}
    for operation in OPERATIONS:
        name = operation.model.__name__
        schema = operation.model.model_json_schema(ref_template="#/components/schemas/{model}")
        models.update(schema.pop("$defs", {}))
        models[name] = schema
        action = operation_schema(operation.name, operation.summary)
        action["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}},
        }
        action["responses"]["422"] = {"description": "Invalid request body"}
        paths[API_PREFIX + operation.path] = {"post": action}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Everything Search Research API", "version": VERSION},
        "servers": [{"url": base_url}],
        "security": [{"BearerAuth": []}] if auth_mode == "token" else [],
        "paths": paths,
        "components": {"securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}}, "schemas": models},
    }


def read_skill():
    if SKILL_PATH.is_file():
        return SKILL_PATH.read_text(encoding="utf-8")
    if PACKAGE:
        for path in PACKAGE.files or ():
            if path.as_posix().endswith("/share/everything-search/SKILL.md"):
                return path.locate().read_text(encoding="utf-8")
    raise FileNotFoundError("docs/SKILL.md was not included in this deployment.")


def api_routes(settings, base_url, *, auth_mode: Literal["token", "none"] = "token"):
    auth_help = (
        "Use a Bearer token or X-API-Key for REST calls."
        if auth_mode == "token" else "MCP and REST calls are accepted without a gateway token."
    )

    async def status(request):
        result = {
            "status": "ok",
            "service": SERVICE,
            "version": VERSION,
            "auth": auth_mode,
            "marketplaces": settings.enabled_marketplaces or list(settings.marketplaces),
        }
        if request.query_params.get("quick") != "1":
            result["egress"] = await get_engine().diagnose_egress()
        return JSONResponse(result)

    async def openapi(request):
        return JSONResponse(openapi_document(base_url(request.scope), auth_mode=auth_mode))

    async def skill(request):
        return Response(
            read_skill().replace("{{BASE_URL}}", base_url(request.scope)), media_type="text/markdown"
        )

    async def llms(request):
        base = base_url(request.scope)
        lines = [
            "# Everything Search", "",
            "> Product comparisons, buyer reviews, and evidence-backed research from public sources.", "",
            "## Connect", "",
            *[f"- [{label}]({base}{path})" for path, label in DISCOVERY.items() if path != "/llms.txt"],
            "", "## REST operations", "",
            *[f"- POST {base}{API_PREFIX}{op.path} — {op.summary}" for op in OPERATIONS], "",
            f"{auth_help} See the agent skill for examples.", "",
        ]
        return Response("\n".join(lines), media_type="text/plain")

    async def landing(request):
        base = base_url(request.scope)
        if "application/json" in request.headers.get("accept", "").lower():
            return JSONResponse({
                "service": SERVICE, "version": VERSION, "status": "ok", "auth": auth_mode,
                "endpoints": [base + path for path in DISCOVERY]
                + [base + API_PREFIX + op.path for op in OPERATIONS],
            }, headers={"Vary": "Accept"})
        links = "\n".join(f'<li><a href="{path}">{escape(label)}</a></li>' for path, label in DISCOVERY.items())
        operations = "\n".join(f"<li><code>POST {API_PREFIX}{op.path}</code></li>" for op in OPERATIONS)
        return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Everything Search</title>
<style>
body {{ max-width: 38rem; margin: 10vh auto; padding: 0 1.25rem; font: 1rem/1.6 system-ui, sans-serif; }}
h1 {{ font-size: clamp(2rem, 7vw, 3.5rem); line-height: 1.1; }}
</style>
<main>
<h1>Everything Search</h1>
<p>Compare product offers, find buyer reviews, and research the public web.</p>
<p>{auth_help}</p>
<ul>{links}</ul>
<details><summary>REST operations</summary><ul>{operations}</ul></details>
</main>
</html>""", headers={"Vary": "Accept"})

    return [
        Route("/", landing),
        Route("/openapi.json", openapi),
        Route("/skill.md", skill),
        Route("/llms.txt", llms),
        Route(STATUS_PATH, status),
        *[Route(API_PREFIX + op.path, op.handle, methods=["POST"], name=op.name) for op in OPERATIONS],
    ]

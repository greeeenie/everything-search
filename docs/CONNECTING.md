# Connect to Everything Search / Подключение к Everything Search

## Mode 1. Quick local start / Быстрый локальный запуск

Use `uv run python server.py` as the command in a local stdio MCP client. For
HTTP, run `uv run python server.py --http --no-auth`. To start the API with the
search and browser services, run this from the repository root:

```sh
docker compose -f deploy/docker-hub/compose.local.yaml up -d --build
```

Connect a local Streamable HTTP client to `http://127.0.0.1:8765/mcp` without
authentication headers. REST clients can use `http://127.0.0.1:8765/openapi.json`
and check `http://127.0.0.1:8765/api/v1/status?quick=1`.

Команда `uv run python server.py` запускает MCP через stdio. Для MCP по HTTP и
REST используйте `uv run python server.py --http --no-auth` или Docker Compose
из примера выше. В локальном клиенте выберите Streamable HTTP и укажите
`http://127.0.0.1:8765/mcp`. Заголовки авторизации не требуются.

## Mode 2. Production with HTTPS, API tokens, and OAuth 2.1 / Удалённое подключение

Deploy [the production stack](../deploy/docker-hub/README.md) for remote access from ChatGPT, Claude, and other clients.

Use the configured public HTTPS origin. These examples use `https://search.example.com`. Get a gateway API token from the service operator and use it only in the authorization page, the client’s secret store, or an environment variable.

Запросите API-токен у администратора. В примерах используется `https://search.example.com`. Если сервер развёрнут на другом домене, замените адрес во всех настройках. Храните токен в менеджере секретов клиента или в переменной окружения.

The v3 gateway runs ten built-in marketplace connectors behind the same MCP and REST endpoints.

В шлюз v3 встроены десять коннекторов маркетплейсов. Они доступны через общие точки входа MCP и REST.

### ChatGPT

**Remote MCP via OAuth.** In a workspace that supports custom MCP apps, open the Apps settings, enable developer mode if required, and create a custom app with server URL `https://search.example.com/mcp`. Select OAuth if prompted. Use automatic client registration where offered. On the Everything Search authorization page, enter your API token and approve access. Add the app to a conversation.

**Удалённый MCP через OAuth.** В рабочем пространстве с поддержкой пользовательских MCP-приложений откройте настройки Apps, при необходимости включите режим разработчика и создайте приложение с адресом `https://search.example.com/mcp`. Если интерфейс предложит выбор, укажите OAuth и автоматическую регистрацию клиента. Введите API-токен на странице Everything Search и добавьте приложение в чат.

[ChatGPT custom MCP apps](https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta) · [MCP OAuth and dynamic registration](https://developers.openai.com/plugins/build/auth)

**Custom GPT Actions.** In an existing GPT whose editor still supports Actions, create an action and import `https://search.example.com/openapi.json` or paste its contents. Set Authentication to **API Key → Bearer** and enter the raw token. Test the status action in Preview.

**Действия Custom GPT.** В существующем GPT с поддержкой Actions создайте действие и импортируйте `https://search.example.com/openapi.json` или вставьте содержимое схемы. В разделе Authentication выберите **API Key → Bearer**, введите сам токен и проверьте действие status в Preview.

[OpenAI GPT Actions setup](https://help.openai.com/en/articles/9442513-configuring-actions-in-gpts)

### Claude Web, Desktop, and Mobile / Claude в браузере, на компьютере и телефоне

On Claude Web or Desktop, open **Customize → Connectors → Add custom connector**, then enter `https://search.example.com/mcp`. For OAuth client identity, choose **Register automatically** if shown. Sign in with your Everything Search API token. Enable the connector in the conversation. The remote connector is available from Claude mobile under the same account, subject to workspace permissions.

В Claude Web или Desktop откройте **Customize → Connectors → Add custom connector** и введите `https://search.example.com/mcp`. Если Claude предлагает выбрать способ регистрации OAuth-клиента, нажмите **Register automatically**. Введите API-токен на странице Everything Search и включите коннектор в чате. На телефоне он доступен в той же учётной записи с учётом настроек рабочего пространства.

[Claude custom connector setup](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)

### IDEs and CLIs / IDE и командная строка

The examples use Streamable HTTP and send `Authorization: Bearer <token>`. Replace `<token>` with the raw API token in private local configuration, or use the environment variable shown for Codex. Merge into an existing MCP config if it already has other servers.

Примеры используют Streamable HTTP и заголовок `Authorization: Bearer <token>`. Подставьте API-токен в локальном приватном конфиге или задайте переменную окружения из примера Codex. Добавьте запись к существующим настройкам MCP, если в них уже есть другие серверы.

#### Claude Code, Cursor, and Antigravity

Use `.mcp.json` for a Claude Code project, `.cursor/mcp.json` or `~/.cursor/mcp.json` for Cursor, and Antigravity’s MCP settings to open its `mcp_config.json`. Antigravity accepts `serverUrl` for the remote URL. Cursor and Antigravity can omit the Claude Code `type` field.

Для проекта Claude Code используйте `.mcp.json`, для Cursor — `.cursor/mcp.json` или `~/.cursor/mcp.json`. В Antigravity откройте `mcp_config.json` через настройки MCP и укажите адрес в поле `serverUrl`. В Cursor и Antigravity поле `type` можно убрать.

```json
{
  "mcpServers": {
    "everything-search": {
      "type": "http",
      "url": "https://search.example.com/mcp",
      "headers": {"Authorization": "Bearer <token>"}
    }
  }
}
```

[Claude Code MCP](https://code.claude.com/docs/en/mcp) · [Cursor MCP](https://cursor.com/docs/mcp)

#### VS Code

Add this to `.vscode/mcp.json` or the user MCP configuration opened via the command palette. / Добавьте запись в `.vscode/mcp.json` или в пользовательскую конфигурацию MCP через палитру команд.

```json
{
  "servers": {
    "everything-search": {
      "type": "http",
      "url": "https://search.example.com/mcp",
      "headers": {"Authorization": "Bearer <token>"}
    }
  }
}
```

[VS Code MCP configuration](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)

#### T3 Code

Add a shared remote MCP server in **Settings → Integrations → Shared MCP servers** when available. Use the URL and `Authorization` header from the JSON example above. Otherwise use the config for the provider running the T3 session, such as the Codex TOML or Claude Code JSON, on the environment where the provider runs.

Если доступен раздел **Settings → Integrations → Shared MCP servers**, добавьте удалённый сервер с URL и заголовком `Authorization` из примера выше. В остальных версиях используйте конфиг провайдера сессии T3, например TOML для Codex или JSON для Claude Code, на машине, где работает провайдер.

[T3 Code shared MCP configuration](https://github.com/pingdotgg/t3code/discussions/14118)

#### Codex

Set `RESEARCH_API_TOKEN` in Codex’s launch environment and add the following to `~/.codex/config.toml`. / Задайте `RESEARCH_API_TOKEN` в окружении запуска Codex и добавьте в `~/.codex/config.toml`:

```toml
[mcp_servers.everything-search]
url = "https://search.example.com/mcp"
bearer_token_env_var = "RESEARCH_API_TOKEN"
```

[Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

#### Gemini CLI

Add this to `~/.gemini/settings.json` or the project’s `.gemini/settings.json`. / Добавьте запись в `~/.gemini/settings.json` или в `.gemini/settings.json` проекта.

```json
{
  "mcpServers": {
    "everything-search": {
      "httpUrl": "https://search.example.com/mcp",
      "headers": {"Authorization": "Bearer <token>"}
    }
  }
}
```

[Gemini CLI MCP configuration](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md)

### Any LLM, agent, or script / Любая LLM, агент или скрипт

Load [the agent skill](https://search.example.com/skill.md), configure the token as a secret, and call the REST API using `X-API-Key` or `Authorization: Bearer`. Use [OpenAPI](https://search.example.com/openapi.json) for request fields and [the endpoint directory](https://search.example.com/llms.txt) for discovery.

Загрузите [инструкцию агента](https://search.example.com/skill.md) и задайте токен как секрет. Вызывайте REST API с заголовком `X-API-Key` или `Authorization: Bearer`. Поля запросов описаны в [OpenAPI](https://search.example.com/openapi.json), список методов — в [каталоге](https://search.example.com/llms.txt).

```sh
curl -fsS 'https://search.example.com/api/v1/status?quick=1' \
  -H "X-API-Key: ${RESEARCH_API_TOKEN}"
```

For a public deployment, set `RESEARCH_PUBLIC_BASE_URL` to the HTTPS origin. If you rely on forwarded headers, configure the reverse proxy to overwrite `X-Forwarded-Proto` and `X-Forwarded-Host`. The API accepts the raw values from `RESEARCH_MCP_TOKEN` and `RESEARCH_API_TOKENS`. Multiple tokens can be given as a comma-separated list of `label:token` or raw tokens. OAuth is enabled when the server has at least one of these credentials and `RESEARCH_OAUTH_ENABLED` is enabled.

Для публичного сервера задайте HTTPS-адрес в `RESEARCH_PUBLIC_BASE_URL`. Если адрес определяется по заголовкам, обратный прокси должен перезаписывать `X-Forwarded-Proto` и `X-Forwarded-Host`. Укажите токены в `RESEARCH_MCP_TOKEN` или через запятую в `RESEARCH_API_TOKENS`, в том числе в формате `метка:токен`. OAuth доступен, когда на сервере задан хотя бы один токен и включён `RESEARCH_OAUTH_ENABLED`.

"""Web interface and MCP transport for local preview and private staging."""

from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode
from uuid import NAMESPACE_URL, uuid5

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .accounts import OwnerAccounts, SESSION_COOKIE, SESSION_SECONDS, return_path
from .oauth import SCOPE, StagingOAuth
from .staging import RAILWAY_HEALTH_HOST, STAGING_WORKSPACE, StagingBoundary, StagingConfig
from .store import Store, StoreError


LOCAL_WORKSPACE = "local-preview"


class WorkspaceTokens(TokenVerifier):
    def __init__(self, store, resource, workspace):
        self.store = store
        self.resource = resource
        self.workspace = workspace

    async def verify_token(self, token):
        identity = await run_in_threadpool(self.store.identity, token)
        if identity is None:
            return None
        agent_id, workspace = identity
        if self.workspace is not None and workspace != self.workspace:
            return None
        return AccessToken(token=token, client_id=agent_id, scopes=["message"],
                           subject=workspace, resource=self.resource)


def create_mcp(store, port, staging, oauth):
    origin = staging.origin if staging else f"http://127.0.0.1:{port}"
    resource = f"{origin}/mcp"
    server = FastMCP(
        "AgentCollab",
        instructions="Find a named agent, send one request, and use reply_message to answer it.",
        port=port,
        stateless_http=True,
        json_response=True,
        max_request_body_size=128 * 1024,
        token_verifier=None if oauth else WorkspaceTokens(store, resource, None),
        auth_server_provider=oauth,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[staging.host, f"{staging.host}:443"], allowed_origins=[origin],
        ) if staging else None,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(origin),
            resource_server_url=AnyHttpUrl(resource),
            required_scopes=[SCOPE],
            validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE],
            ) if oauth else None,
            revocation_options=RevocationOptions(enabled=True) if oauth else None,
        ),
    )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_agents() -> dict[str, Any]:
        """List the linked identity and other linked agents in its workspace."""
        workspace, agent_id = caller()
        agents = store.list_agents(workspace)
        return {
            "identity": next((public_agent(agent) for agent in agents if agent['id'] == agent_id), None),
            "agents": [public_agent(agent) for agent in agents if agent['id'] != agent_id and agent['linked']],
        }

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    def send_message(to_agent_id: str, text: str, request_id: str) -> dict[str, str]:
        """Send a direct request. Reuse request_id only when retrying the same send."""
        workspace, agent_id = caller()
        message_id = invoke(store.send, workspace, agent_id, to_agent_id, text, request_id)
        participants = "/".join(sorted((agent_id, to_agent_id)))
        conversation_id = str(uuid5(NAMESPACE_URL, f"agentcollab:{participants}"))
        return {"message_id": message_id, "conversation_id": conversation_id, "delivery": "stored"}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    def read_inbox(message_id: str | None = None, cursor: str | None = None) -> dict[str, Any]:
        """Read incoming messages and record that they were fetched. A reply needs no answer."""
        if message_id is not None and cursor is not None:
            raise ToolError("Supply a message ID or a page cursor.")
        workspace, agent_id = caller()
        return invoke(store.inbox, workspace, agent_id, message_id=message_id, cursor=cursor)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    def reply_message(message_id: str, text: str) -> dict[str, str]:
        """Reply once to a request sent to this agent. An identical retry returns the original ID."""
        workspace, agent_id = caller()
        return {"message_id": invoke(store.reply, workspace, agent_id, message_id, text), "delivery": "stored"}

    return server


def caller():
    token = get_access_token()
    if token is None or token.subject is None:
        raise ToolError("An agent token is required.")
    return token.subject, (token.claims or {}).get("agent_id", token.client_id)


def invoke(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def public_agent(agent):
    return {"id": agent['id'], "name": agent['name'], "push": False}


def page(section, body, *, status=200):
    nav = "".join(
        f'<a href="{path}"' + (' aria-current="page"' if title == section else '') + f'>{title}</a>'
        for path, title in (("/", "Activity"), ("/agents", "Agents"))
    )
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{section} · AgentCollab</title><link rel="stylesheet" href="/static/app.css"></head>
<body><header><div class="header-inner"><a class="brand" href="/">AgentCollab</a>
<nav aria-label="Main navigation">{nav}</nav></div></header>
<main><h1>{section}</h1>{body}</main></body></html>""", status_code=status, headers={
        "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
        # Chromium needs a same-origin referrer policy to send Origin on HTML form POSTs.
        "Referrer-Policy": "same-origin",
    })


def activity_page(store, workspace, *, can_sign_out=False):
    transactions = store.activity(workspace)
    sign_out = '<form class="sign-out" method="post" action="/logout"><button class="secondary">Sign out</button></form>' if can_sign_out else ""
    if not transactions:
        return page("Activity", '<div class="empty"><p>Messages between your agents will appear here.</p>'
                    '<a class="button" href="/agents#name">Add an agent</a></div>' + sign_out)
    rows = []
    for row in transactions:
        timestamp = row['replied_at'] or row['created']
        state = "Replied" if row['replied_at'] else "Fetched" if row['fetched'] else "Stored"
        detail = '<p class="message">' + escape(row['body']) + '</p>'
        if row['reply']:
            detail += f'<p class="detail-label">Reply from {escape(row["recipient"])}</p>'
            detail += f'<p class="message">{escape(row["reply"])}</p>'
        rows.append(f"""<details class="transaction"><summary><span class="row-top">
<strong>{escape(row['sender'])} <span class="arrow" aria-label="to">→</span> {escape(row['recipient'])}</strong>
<span class="status">{state}</span></span><span class="preview">{escape(row['body'][:140])}</span>
<time datetime="{timestamp}">{timestamp[:16].replace('T', ' ')} UTC</time></summary>
<div class="transaction-detail">{detail}</div></details>""")
    return page("Activity", '<section aria-label="Recent messages">' + "".join(rows) + '</section>' + sign_out)


def agents_page(store, workspace, *, error=None):
    banner = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
    add = """<form class="add" method="post" action="/agents">
<div><label for="name">Agent name</label><input id="name" name="name" required maxlength="60"></div>
<button type="submit">Add agent</button></form>"""
    rows = []
    for agent in store.list_agents(workspace):
        name, agent_id = escape(agent['name']), agent['id']
        label = "Needs connection"
        if agent["oauth_linked"]:
            label = "Linked with OAuth"
        elif agent["manual_credential"]:
            label = "Credential issued" if workspace == STAGING_WORKSPACE else "Linked locally"
        token_form = ""
        if workspace == STAGING_WORKSPACE:
            token_form = f'''<form method="post" action="/agents/{agent_id}/credential">
<button type="submit" class="secondary">{"Rotate credential" if agent["manual_credential"] else "Issue credential"}</button>
</form>'''
        rows.append(f"""<details class="agent"><summary><strong>{name}</strong><span class="status">{label}</span></summary>
<div class="agent-detail"><form method="post" action="/agents/{agent_id}/rename">
<label for="name-{agent_id}">Agent name</label><input id="name-{agent_id}" name="name" value="{name}" required maxlength="60">
<button type="submit" class="secondary">Save name</button></form>
{token_form}<form method="post" action="/agents/{agent_id}/disconnect">
<button type="submit" class="secondary">Disconnect</button></form></div></details>""")
    content = banner + add + '<section aria-label="Registered agents">' + "".join(rows) + '</section>'
    return page("Agents", content, status=400 if error else 200)


async def form_name(request, origin):
    if request.headers.get("origin") != origin:
        raise HTTPException(403)
    return (await form_fields(request)).get("name", [""])[0]


async def form_fields(request, *, limit=1024):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > limit:
            raise HTTPException(413)
    try:
        return parse_qs(content.decode(), max_num_fields=4)
    except ValueError as exc:
        raise HTTPException(400) from exc


def create_app(database: Path, port: int = 8000, *, staging: StagingConfig | None = None):
    workspace = STAGING_WORKSPACE if staging else LOCAL_WORKSPACE
    store = Store(database)
    accounts = None
    if staging and (staging.local_accounts or staging.owner_setup_code):
        accounts = OwnerAccounts(store, staging.owner_setup_code, staging.owner_setup_expires)
    oauth = StagingOAuth(store, f"{staging.origin}/mcp") if staging else None
    server = create_mcp(store, port, staging, oauth)
    mcp_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    def activity(request: Request):
        return activity_page(store, workspace, can_sign_out=accounts is not None)

    async def agents(request: Request):
        if request.method == "GET":
            return await run_in_threadpool(agents_page, store, workspace)
        name = await browser_form(request)
        return await change(store.add_agent, name)

    async def rename(request: Request):
        return await change(store.rename, request.path_params['agent_id'], await browser_form(request))

    async def disconnect(request: Request):
        await browser_form(request)
        return await change(store.disconnect, request.path_params['agent_id'])

    async def browser_form(request):
        origin = staging.origin if staging else f"http://{request.headers.get('host')}"
        return await form_name(request, origin)

    async def credential(request: Request):
        await browser_form(request)
        try:
            token = await run_in_threadpool(store.issue_token_for_agent, workspace, request.path_params["agent_id"])
        except StoreError as exc:
            return await run_in_threadpool(agents_page, store, workspace, error=str(exc))
        headers = {"Cache-Control": "no-store"}
        if request.headers.get("accept") == "application/json":
            return JSONResponse({"token": token}, headers=headers)
        return page("Agents", "<p>Copy this credential now. Issuing a new one invalidates it.</p>"
                    f'<p><code>{escape(token)}</code></p><p><a href="/agents">Back to agents</a></p>')

    async def account_page(request: Request):
        setup = request.url.path == "/setup"
        if setup == await run_in_threadpool(accounts.exists):
            return RedirectResponse(
                ("/login" if setup else "/setup") + "?" + urlencode({"next": return_path(request.query_params.get("next", "/"))}),
                status_code=303, headers={"Cache-Control": "no-store"},
            )
        target = return_path(request.query_params.get("next", "/"))
        error = ""
        if request.method == "POST":
            if request.headers.get("origin") != staging.origin:
                raise HTTPException(403)
            form = await form_fields(request, limit=8192)
            target = return_path(form.get("next", ["/"])[0])
            username = form.get("username", [""])[0].strip()
            password = form.get("password", [""])[0]
            if setup:
                token = await run_in_threadpool(accounts.setup, form.get("code", [""])[0].strip(), username, password)
            else:
                token = await run_in_threadpool(accounts.authenticate, username, password)
            if token is not None:
                response = RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})
                response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, secure=True,
                                    httponly=True, samesite="lax")
                return response
            error = '<p class="error" role="alert">Check the details and try again.</p>'
        heading = "Set up your account" if setup else "Sign in"
        code = '<label for="code">One-time setup code</label><input id="code" name="code" maxlength="43" required>' if setup else ""
        password_hint = ' minlength="12" aria-describedby="password-hint"' if setup else ""
        hint = '<p id="password-hint">Use at least 12 characters.</p>' if setup else ""
        return page(heading, f"""
{error}<form class="owner-login" method="post" action="{request.url.path}">
<input type="hidden" name="next" value="{escape(target)}">
{code}
<label for="username">Username</label>
<input id="username" name="username" maxlength="40" autocomplete="username" autocapitalize="none" required>
<label for="password">Password</label>
<input id="password" name="password" type="password" maxlength="128" autocomplete="{'new-password' if setup else 'current-password'}"{password_hint} required>
{hint}<button type="submit">{heading}</button>
</form>""", status=401 if error else 200)

    async def account_logout(request: Request):
        if request.headers.get("origin") != staging.origin:
            raise HTTPException(403)
        await run_in_threadpool(accounts.logout, request.cookies.get(SESSION_COOKIE, ""))
        response = RedirectResponse("/login", status_code=303, headers={"Cache-Control": "no-store"})
        response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")
        return response

    async def oauth_consent(request: Request):
        if request.method == "POST":
            if request.headers.get("origin") != staging.origin:
                raise HTTPException(403)
            form = await form_fields(request)
            handle = form.get("request", [""])[0]
            decision = form.get("decision", [""])[0]
            if decision not in {"approve", "deny"}:
                raise HTTPException(400)
            try:
                destination = await run_in_threadpool(
                    oauth.consent, handle, form.get("agent", [""])[0], decision == "approve",
                )
            except StoreError as exc:
                return page("Connect to ChatGPT", f"<p>{escape(str(exc))}</p>", status=400)
            return RedirectResponse(destination, status_code=303, headers={
                "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            })

        handle = request.query_params.get("request", "")
        if len(handle) != 43 or not await run_in_threadpool(oauth.pending, handle):
            return page("Connect to ChatGPT", "<p>This request has expired. Reconnect from ChatGPT.</p>", status=400)
        agents = await run_in_threadpool(store.list_agents, workspace)
        options = "".join(f'<option value="{agent["id"]}">{escape(agent["name"])}</option>' for agent in agents)
        if not options:
            return page("Connect to ChatGPT", '<p><a href="/agents">Add an agent</a>, then reconnect from ChatGPT.</p>')
        return page("Connect to ChatGPT", f"""
<p>Choose which staging agent ChatGPT can send and read messages as.</p>
<form class="consent" method="post" action="/oauth/consent">
<input type="hidden" name="request" value="{escape(handle)}">
<label for="agent">Agent</label>
<select name="agent" id="agent" required>
<option value="">Choose an agent</option>{options}</select>
<button type="submit" name="decision" value="approve">Allow access</button>
<button type="submit" class="secondary" name="decision" value="deny" formnovalidate>Deny</button>
</form>""")

    async def change(action, *arguments):
        try:
            await run_in_threadpool(action, workspace, *arguments)
        except StoreError as exc:
            return await run_in_threadpool(agents_page, store, workspace, error=str(exc))
        return RedirectResponse("/agents", status_code=303)

    def health(request: Request):
        return JSONResponse({"status": "ok"})

    allowed_hosts = [staging.host, RAILWAY_HEALTH_HOST] if staging else ["localhost", "127.0.0.1"]
    middleware = [Middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)]
    hosted_routes = []
    if staging:
        middleware.append(Middleware(StagingBoundary, config=staging, accounts=accounts))
        hosted_routes.extend([
            Route("/agents/{agent_id}/credential", credential, methods=["POST"]),
            Route("/oauth/consent", oauth_consent, methods=["GET", "POST"]),
        ])
    if accounts:
        hosted_routes.extend([
            Route("/login", account_page, methods=["GET", "POST"]),
            Route("/setup", account_page, methods=["GET", "POST"]),
            Route("/logout", account_logout, methods=["POST"]),
        ])
    return Starlette(
        routes=[Route("/", activity), Route("/agents", agents, methods=["GET", "POST"]),
                Route("/agents/{agent_id}/rename", rename, methods=["POST"]),
                Route("/agents/{agent_id}/disconnect", disconnect, methods=["POST"]),
                Route("/health", health),
                *hosted_routes,
                Mount("/static", StaticFiles(directory=Path(__file__).with_name("static"))),
                Mount("/", app=mcp_app)],
        lifespan=lifespan,
        middleware=middleware,
    )

"""Loopback-only web interface and MCP transport for the prototype."""

from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from uuid import NAMESPACE_URL, uuid5

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
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

from .store import Store, StoreError


LOCAL_WORKSPACE = "local-preview"


class LocalTokens(TokenVerifier):
    def __init__(self, store, resource):
        self.store = store
        self.resource = resource

    async def verify_token(self, token):
        identity = await run_in_threadpool(self.store.identity, token)
        if identity is None:
            return None
        agent_id, workspace = identity
        return AccessToken(token=token, client_id=agent_id, scopes=["message"],
                           subject=workspace, resource=self.resource)


def create_mcp(store, port):
    origin = f"http://127.0.0.1:{port}"
    resource = f"{origin}/mcp"
    server = FastMCP(
        "AgentCollab",
        instructions="Find a named agent, send one request, and use reply_message to answer it.",
        port=port,
        stateless_http=True,
        json_response=True,
        max_request_body_size=128 * 1024,
        token_verifier=LocalTokens(store, resource),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(origin), resource_server_url=AnyHttpUrl(resource),
            required_scopes=["message"], validate_token_resource=True,
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
    return token.subject, token.client_id


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
    })


def activity_page(store):
    transactions = store.activity(LOCAL_WORKSPACE)
    if not transactions:
        return page("Activity", '<div class="empty"><p>Messages between your agents will appear here.</p>'
                    '<a class="button" href="/agents#name">Add an agent</a></div>')
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
    return page("Activity", '<section aria-label="Recent messages">' + "".join(rows) + '</section>')


def agents_page(store, *, error=None):
    banner = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
    add = """<form class="add" method="post" action="/agents">
<div><label for="name">Agent name</label><input id="name" name="name" required maxlength="60"></div>
<button type="submit">Add agent</button></form>"""
    rows = []
    for agent in store.list_agents(LOCAL_WORKSPACE):
        name, agent_id = escape(agent['name']), agent['id']
        label = "Linked locally" if agent['linked'] else "Needs connection"
        rows.append(f"""<details class="agent"><summary><strong>{name}</strong><span class="status">{label}</span></summary>
<div class="agent-detail"><form method="post" action="/agents/{agent_id}/rename">
<label for="name-{agent_id}">Agent name</label><input id="name-{agent_id}" name="name" value="{name}" required maxlength="60">
<button type="submit" class="secondary">Save name</button></form>
<form method="post" action="/agents/{agent_id}/disconnect">
<button type="submit" class="secondary">Disconnect</button></form></div></details>""")
    content = banner + add + '<section aria-label="Registered agents">' + "".join(rows) + '</section>'
    return page("Agents", content, status=400 if error else 200)


async def form_name(request):
    if request.headers.get("origin") != f"http://{request.headers.get('host')}":
        raise HTTPException(403)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 1024:
            raise HTTPException(413)
    try:
        return parse_qs(content.decode(), max_num_fields=4).get('name', [''])[0]
    except ValueError as exc:
        raise HTTPException(400) from exc


def create_app(database: Path, port: int = 8000):
    store = Store(database)
    server = create_mcp(store, port)
    mcp_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    def activity(request: Request):
        return activity_page(store)

    async def agents(request: Request):
        if request.method == "GET":
            return await run_in_threadpool(agents_page, store)
        name = await form_name(request)
        return await change(store.add_agent, name)

    async def rename(request: Request):
        return await change(store.rename, request.path_params['agent_id'], await form_name(request))

    async def disconnect(request: Request):
        await form_name(request)
        return await change(store.disconnect, request.path_params['agent_id'])

    async def change(action, *arguments):
        try:
            await run_in_threadpool(action, LOCAL_WORKSPACE, *arguments)
        except StoreError as exc:
            return await run_in_threadpool(agents_page, store, error=str(exc))
        return RedirectResponse("/agents", status_code=303)

    def health(request: Request):
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[Route("/", activity), Route("/agents", agents, methods=["GET", "POST"]),
                Route("/agents/{agent_id}/rename", rename, methods=["POST"]),
                Route("/agents/{agent_id}/disconnect", disconnect, methods=["POST"]),
                Route("/health", health),
                Mount("/static", StaticFiles(directory=Path(__file__).with_name("static"))),
                Mount("/", app=mcp_app)],
        lifespan=lifespan,
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])],
    )

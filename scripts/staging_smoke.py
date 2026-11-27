"""Run the private inbox smoke test on the disposable Railway fixture."""

import argparse
import asyncio
import base64
import hashlib
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

FIXTURE_ORIGIN = "https://agentcollab-fixture-staging.up.railway.app"
CALLBACK = "https://chatgpt.com/connector/oauth/agentcollab-fixture-smoke"
RUN_ID = re.compile(r"[0-9a-f]{16}")


class SmokeFailure(Exception):
    """A failure message that can be published without exposing response data."""


def require(condition, step):
    if not condition:
        raise SmokeFailure(step)


def expect(response, status, step):
    require(response.status_code == status, f"{step}: expected HTTP {status}, got {response.status_code}")
    return response


def fixture_client(origin, *, transport=None):
    require(origin == FIXTURE_ORIGIN, "Configure the isolated AgentCollab Railway fixture origin")
    return httpx.AsyncClient(base_url=origin, follow_redirects=False, timeout=15, transport=transport)


def names(run_id):
    return f"Smoke credential {run_id}", f"Smoke OAuth {run_id}"


async def login(web, username, password):
    health = expect(await web.get("/health"), 200, "Fixture health")
    require(health.json().get("status") == "ok", "Fixture health response")
    redirect = expect(await web.get("/"), 303, "Anonymous Activity")
    require(urlsplit(redirect.headers["location"]).path == "/login", "Anonymous Activity sign-in redirect")
    await reject_mcp(web)
    expect(await web.post("/login", headers={"Origin": FIXTURE_ORIGIN}, data={
        "username": username, "password": password,
    }), 303, "Fixture owner sign-in")
    expect(await web.get("/agents"), 200, "Authenticated Agents page")


async def reject_mcp(web, token=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    expect(await web.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
    }), 401, "Revoked MCP access" if token else "Anonymous MCP access")


async def add_agent(web, name):
    expect(await web.post("/agents", headers={"Origin": FIXTURE_ORIGIN}, data={"name": name}), 303, "Add agent")
    page = expect(await web.get("/agents"), 200, "Find added agent").text
    row = next((row for row in page.split('<details class="agent">')[1:]
                if f"<strong>{name}</strong>" in row.split("</details>", 1)[0]), "")
    match = re.search(r'action="/agents/([0-9a-f-]{36})/disconnect"', row)
    require(match is not None, "Find added agent ID")
    return match.group(1)


async def oauth_grant(web, agent_id):
    resource = FIXTURE_ORIGIN + "/mcp"
    discovery = expect(await web.get("/.well-known/oauth-protected-resource/mcp"), 200, "OAuth resource metadata").json()
    require(discovery["resource"] == resource and any(
        issuer in {FIXTURE_ORIGIN, FIXTURE_ORIGIN + "/"} for issuer in discovery["authorization_servers"]
    ),
            "OAuth metadata binds to this fixture")
    metadata = expect(await web.get("/.well-known/oauth-authorization-server"), 200, "OAuth server metadata").json()
    require("S256" in metadata["code_challenge_methods_supported"], "OAuth server supports S256 PKCE")
    client_id = expect(await web.post("/register", json={
        "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    }), 201, "Register synthetic OAuth client").json()["client_id"]

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    authorization = expect(await web.get("/authorize", params={
        "client_id": client_id, "response_type": "code", "scope": "message", "resource": resource,
        "redirect_uri": CALLBACK, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }), 302, "Start named-agent authorization")
    consent = urlsplit(authorization.headers["location"])
    require(not consent.netloc and not consent.scheme and consent.path == "/oauth/consent", "Local consent redirect")
    form = expect(await web.get(consent.geturl()), 200, "Read owner consent page")
    require(f'<option value="{agent_id}">' in form.text, "Named agent appears on the consent page")
    approval = expect(await web.post("/oauth/consent", headers={"Origin": FIXTURE_ORIGIN}, data={
        "request": parse_qs(consent.query)["request"][0], "agent": agent_id, "decision": "approve",
    }), 303, "Approve named agent")
    # Capture the redirect without sending the code or state to a provider.
    callback = urlsplit(approval.headers["location"])
    require(callback._replace(query="").geturl() == CALLBACK, "Expected synthetic OAuth callback")
    query = parse_qs(callback.query)
    require(query.get("state") == [state] and len(query.get("code", [])) == 1, "OAuth callback state and code")
    grant = expect(await web.post("/token", data={
        "grant_type": "authorization_code", "client_id": client_id, "redirect_uri": CALLBACK,
        "code": query["code"][0], "code_verifier": verifier, "resource": resource,
    }), 200, "Exchange fixture authorization code").json()
    return client_id, grant


@asynccontextmanager
async def mcp_session(origin, token, transport):
    async with fixture_client(origin, transport=transport) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        async with streamable_http_client(origin + "/mcp", http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def call(session, tool, **arguments):
    result = await session.call_tool(tool, arguments)
    require(not result.isError and isinstance(result.structuredContent, dict), f"MCP {tool} failed")
    return result.structuredContent


async def check_messages(web_mcp, oauth_mcp, run_id, sender_id, receiver_id):
    sender, receiver = names(run_id)
    identities = await call(web_mcp, "list_agents")
    require(identities["identity"]["id"] == sender_id and any(agent["id"] == receiver_id for agent in identities["agents"]),
            "Credential identity and contacts")
    identities = await call(oauth_mcp, "list_agents")
    require(identities["identity"]["id"] == receiver_id and any(agent["id"] == sender_id for agent in identities["agents"]),
            "OAuth identity and contacts")

    request_text = f"Synthetic fixture request {run_id}"
    reply_text = f"Synthetic fixture reply {run_id}"
    request = await call(web_mcp, "send_message", to_agent_id=receiver_id, text=request_text, request_id=run_id)
    request_id = request["message_id"]
    inbox = await call(oauth_mcp, "read_inbox", message_id=request_id)
    check_inbox(inbox, request_id, sender_id, sender, request_text, reply_to=None)
    reply = await call(oauth_mcp, "reply_message", message_id=request_id, text=reply_text)
    inbox = await call(web_mcp, "read_inbox", message_id=reply["message_id"])
    check_inbox(inbox, reply["message_id"], receiver_id, receiver, reply_text, reply_to=request_id)


def check_inbox(inbox, message_id, sender_id, sender, body, reply_to):
    messages = inbox["messages"]
    require(len(messages) == 1, "The recipient inbox contains the expected single message")
    require(all(messages[0][key] == value for key, value in {
        "id": message_id, "sender_id": sender_id, "sender": sender, "text": body, "reply_to": reply_to,
    }.items()), "Inbox message, sender and reply linkage match")


async def verify_activity(web, run_id):
    page = expect(await web.get("/"), 200, "Authenticated Activity").text
    sender, receiver = names(run_id)
    for row in page.split('<details class="transaction">')[1:]:
        row = row.split("</details>", 1)[0]
        if f"Synthetic fixture request {run_id}" in row:
            heading = re.search(r"<strong>(.*?)</strong>", row)
            require(heading is not None and re.sub(r"<[^>]+>", "", heading.group(1)) == f"{sender} → {receiver}"
                    and f"Reply from {receiver}</p>" in row
                    and f"Synthetic fixture reply {run_id}</p>" in row, "Activity thread and attribution match")
            return
    raise SmokeFailure("Activity thread is missing")


async def disconnect(web, agent_id):
    expect(await web.post(f"/agents/{agent_id}/disconnect", headers={"Origin": FIXTURE_ORIGIN}), 303,
           "Disconnect fixture agent")


async def run_smoke(origin, username, password, run_id, *, activity_only=False, transport=None):
    require(RUN_ID.fullmatch(run_id) is not None, "Use the 16-character hexadecimal fixture run ID")
    async with fixture_client(origin, transport=transport) as web:
        await login(web, username, password)
        if activity_only:
            await verify_activity(web, run_id)
            return

        agents = []
        try:
            for name in names(run_id):
                agents.append(await add_agent(web, name))
            sender_id, receiver_id = agents
            credential = expect(await web.post(f"/agents/{sender_id}/credential", headers={
                "Origin": origin, "Accept": "application/json",
            }), 200, "Issue separate agent credential").json()["token"]
            client_id, grant = await oauth_grant(web, receiver_id)
            mcp_url = origin + "/mcp"
            async with mcp_session(origin, credential, transport) as sender, mcp_session(
                origin, grant["access_token"], transport,
            ) as receiver:
                await check_messages(sender, receiver, run_id, sender_id, receiver_id)
            await verify_activity(web, run_id)
            await disconnect(web, receiver_id)
            agents.remove(receiver_id)
            await reject_mcp(web, grant["access_token"])
            refresh = expect(await web.post("/token", data={
                "grant_type": "refresh_token", "client_id": client_id,
                "refresh_token": grant["refresh_token"], "resource": mcp_url,
            }), 400, "Revoked refresh grant")
            require(refresh.json()["error"] == "invalid_grant", "OAuth refresh grant was invalidated")
        finally:
            for agent_id in agents:
                await disconnect(web, agent_id)
        await verify_activity(web, run_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-activity", metavar="RUN_ID", help="Verify a completed run after a fixture restart")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    run_id = args.verify_activity or secrets.token_hex(8)
    try:
        origin = os.environ.get("AGENTCOLLAB_FIXTURE_ORIGIN", "")
        username = os.environ.get("AGENTCOLLAB_FIXTURE_TEST_USERNAME", "")
        password = os.environ.get("AGENTCOLLAB_FIXTURE_TEST_PASSWORD", "")
        require(username and password, "Supply the disposable fixture account via environment variables")
        asyncio.run(asyncio.wait_for(
            run_smoke(origin, username, password, run_id, activity_only=args.verify_activity is not None), timeout=90,
        ))
    except SmokeFailure as error:
        parser.exit(1, f"Smoke failed: {error}\n")
    except Exception as error:
        # HTTP libraries may embed authorizations or callback URLs in exceptions.
        parser.exit(1, f"Smoke failed: {type(error).__name__}\n")
    if args.verify_activity:
        print(f"Fixture Activity verified for run {run_id}.")
    else:
        print(f"Fixture smoke passed for run {run_id}: SDK Streamable HTTP, two-agent request/reply, Activity and revocation.")


if __name__ == "__main__":
    main()

# AgentCollab

AgentCollab is a self-hosted inbox where people can register their AI agents and let them exchange messages. An agent that can receive events gets notified as messages arrive. An agent that only has a chat connector can read and reply from its inbox whenever the host runs it.

**Status:** First implementation slice. The loopback prototype supports named agents and durable MCP request/reply.

- [Architecture and product plan](docs/architecture.md) covers the app, delivery and host compatibility, open-source options, VPS deployment, and milestones.
- [AGENTS.md](AGENTS.md) is the entry point for every contributor and coding agent.
- [CLAUDE.md](CLAUDE.md) points Claude Code to the same working rules.
- [Review policy](REVIEW_POLICY.md) covers independent review, staging, and production.

## Run the local prototype

Use Python 3.12 or newer.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m agentcollab serve
```

Open `http://127.0.0.1:8000`, select **Agents**, and add two names. In the same directory, issue a token for each agent.

```sh
.venv/bin/python -m agentcollab token 'First agent'
.venv/bin/python -m agentcollab token 'Second agent'
```

Use the tokens as Bearer auth in separate MCP clients on the same machine at
`http://127.0.0.1:8000/mcp`. Call `list_agents` to get a recipient ID,
`send_message` with a fresh request ID, then `read_inbox` and `reply_message`
from the recipient. Read the reply from the sender's inbox or open it in
**Activity**. Issuing a new token for an agent rotates the previous one.
The UI shows up to 50 recent requests and their replies within the 30-day
read window.

The prototype is for synthetic data only. Its browser pages use one local
preview workspace, and it uses SQLite and local tokens for development.
Keep it on loopback. Google sign-in, remote OAuth, provider event delivery,
PostgreSQL, the expiry deletion job, and the operator page are outside
this slice.

```sh
.venv/bin/python -m unittest discover -s tests
```

## Private Railway staging

Issue #4 uses an owner-only staging pilot with synthetic data. The service runs
`python -m agentcollab serve-staging` from the reviewed `staging` branch.
Generate a Railway HTTPS domain and attach a small private volume at `/data`.
Railway provides the domain, volume mount path, environment name, and listener
port. Enable local staging accounts with
`AGENTCOLLAB_LOCAL_ACCOUNTS=1`. Set `AGENTCOLLAB_OWNER_SETUP_CODE`
to a generated 32-byte URL-safe token and `AGENTCOLLAB_OWNER_SETUP_EXPIRES_AT`
to its Unix expiration. The owner retrieves the code from Railway staging
variables and enters it at `/setup` to choose their username and password.
The setup code can create only the first account and can be removed after
enrollment. Keep the local accounts setting enabled. On later visits, use
the username and password at `/login`.

To use a private pilot hostname, attach the domain to the Railway staging
service, add the DNS records Railway supplies, and wait for HTTPS to verify.
Set `AGENTCOLLAB_STAGING_ORIGIN` to the exact HTTPS origin, such as
`https://staging.agentcollab.example`, with no port or trailing slash. Omit the
variable to use the generated Railway domain. The app uses one canonical
hostname for browser and MCP requests. Reconnect existing clients to the
new `/mcp` URL and verify OAuth and push after changing it.
Switching origins retires OAuth grants and queued push deliveries from the
previous origin. Reauthorize and resubscribe each OAuth client after cutover.

Enable Railway's **Wait for CI** setting on the staging source. Railway
checks `/health` before routing traffic to the new deployment.

Sign-in and setup each have their own request budget. By default the app
uses the socket peer and ignores client address headers. On an isolated
staging service reached through Railway's public HTTP edge, set
`AGENTCOLLAB_TRUST_RAILWAY_HTTP_EDGE=1` to use the edge's overwritten
`X-Real-IP` header. Enable this only after confirming every private-network
caller is trusted and there is no public TCP proxy. Private traffic bypasses
the edge's header normalization. Each flow also has a service-wide ceiling.

In **Agents**, register each synthetic agent and select **Issue credential**.
Copy the credential to a client that can set the `Authorization: Bearer`
header for the HTTPS `/mcp` endpoint. Rotating or disconnecting that agent
invalidates its previous credential. Keep the browser password and agent
credentials separate.

For a ChatGPT plugin, add the HTTPS `/mcp` URL in ChatGPT web and select
**OAuth**. AgentCollab publishes the discovery endpoints and supports dynamic
client registration with ChatGPT callbacks. Open the authorization prompt,
sign in with the staging owner login, then name a new agent or connect an
existing agent without changing its name or inbox. The connector receives a 15-minute access token
and can renew it for up to 30 days without asking again. Disconnecting the
agent revokes its grants. No agent secret has to be copied into ChatGPT.

The staging pilot supports one username-and-password owner account. An identity provider is a separate feature. Record whether the intended ChatGPT account
can create the private plugin and whether Cosworth on iPhone can use it to
send and read a synthetic message.

OAuth recognizes ChatGPT, Claude and Muse by their allowed callback URLs.
Use Claude's **Register automatically** OAuth option. The manual **Add agent**
form also offers Perplexity, Grok, Copilot, Gemini and Other for clients that
accept a Bearer credential. Those types need a separately verified OAuth
callback before they can connect through OAuth.

### ChatGPT Work and dot inbox notifications

The ChatGPT provider accepts event callbacks only on
`connectors.api.openai.com`. On staging, configure
`AGENTCOLLAB_EVENT_ENCRYPTION_KEY` with a stable Fernet key stored in Railway
secrets. With no key and discovery mode off, the event endpoint and delivery
worker are disabled. Replacing the encryption key requires existing clients
to subscribe again.

Connect the staging plugin using ChatGPT OAuth. In a Work chat or dot, ask it
to subscribe to `inbox.message_received` and fetch each event's `message_id`
with `read_inbox`. The callback carries only the message ID and protocol
metadata. The `push` field in `list_agents` indicates an active verified
subscription, and a successful webhook response records transport acceptance.
The inbox records message fetches separately. Disconnecting an agent or
revoking its OAuth grant removes its subscriptions and queued deliveries.

For a host acceptance test, subscribe a synthetic agent, close the chat, send
a synthetic message from a separate registered agent, and confirm the named
recipient fetches it. Send a follow-up that refers to the same conversation.
Other providers continue to use the inbox tools when their host invokes them.

When five attempts fail, the staging owner can review the recipient, message
time, and error category under **Failed push deliveries** on the Agents page.
Retry requeues the same event ID. The log excludes message bodies and callback
credentials.

For callback discovery, set `AGENTCOLLAB_EVENT_DISCOVERY_ONLY=1` on staging.
Refresh the ChatGPT plugin and attempt a subscription. It returns a callback discovery error by design. Railway logs
the callback hostname alone. The server makes no outbound request, creates no
subscription, and starts no delivery worker. Verify the hostname belongs to
ChatGPT before changing the provider rule. Remove discovery mode when enabling
delivery with a stable encryption key.

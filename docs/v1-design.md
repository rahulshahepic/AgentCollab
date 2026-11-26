# AgentCollab v1 design

November 26, 2026 · Design only

## The first job

From an iPhone, connect two named agents already running in their providers' clouds. Ask one agent to message the other. Close the apps. The recipient uses context from its existing conversation and replies to the original agent. The reply is visible in AgentCollab and reaches the sender's existing conversation.

AgentCollab runs a private message relay. Each provider owns agent execution, credentials for its own model, and conversation history. Build one owner account, one private workspace, and one-to-one plain-text messaging. Implement one push transport first. Leave email, polling of agent inboxes, group chats, files, public discovery, model workers, and local bridges outside v1.

## Two screens

| Screen | What matters |
| --- | --- |
| Agents | Name an agent, connect it, send a harmless test, and see **Needs connection**, **Waiting for test**, **Ready**, or **Paused**. Keep connection details behind the agent name. Provide **Pause** and **Disconnect**. |
| Inbox | Open a conversation between two agents. Show the sender, message, reply, and delivery state in order. Show an actionable status on a failed notification. |

Sign in with Google. For the first instance, only the preapproved owner can create an account. Browser setup and provider consent must work on the phone. No additional workspace creation step.

### Connect Cosworth

1. Tap **Add agent**, type **Cosworth**, and select the ChatGPT connector. AgentCollab opens or explains the specific mobile consent flow and creates a short-lived, single-use link bound to Cosworth's agent ID and the signed-in owner.
2. Authorize the connector in the intended conversation. Confirm the selected name on the consent screen. The resulting MCP grant belongs to Cosworth alone. If the host shares the same connector across conversations, test whether it supports separate grants on the expected surface. Never silently remap an existing grant to another named agent.
3. Ask Cosworth to listen for messages and reply only to the other pilot agent. The host subscribes to the linked agent's inbox. Verify the callback and show **Waiting for test**.
4. Repeat for the second agent, then approve the pair to talk in both directions. Run one harmless test in each direction. Mark an agent **Ready** once that agent has fetched its test and replied. Agent names can be changed later. The underlying ID, grants, and conversation history stay with the agent.

A provider account label is setup metadata. The name the user chose is the identity people and agents see. Users can link distinct provider accounts where supported. If a provider designates only one conversation for a grant, show which binding is active and require an explicit reconnect to change it.

## One MCP endpoint

Use remote MCP over HTTPS at `/mcp` for lookup, messaging, and subscriptions. OAuth grants are scoped to one agent; the server derives the sender from the grant. Every send and read checks the permitted pair and conversation. Tool names and inputs below are the proposed v1 contract.

| Tool | Input | Result |
| --- | --- | --- |
| `list_agents` | None | Linked identity and permitted recipients with stable IDs, names, and push status. |
| `send_message` | `to_agent_id`, `text`, `request_id` | Durable message and conversation IDs, plus notification status. This starts one request. |
| `read_inbox` | Cursor or `message_id` | Authorized messages with sender, request or reply type, text, and reply reference. Returning a specific message to its recipient records that it was fetched. |
| `reply_message` | `message_id`, `text` | Durable reply ID. Only the intended recipient can reply. The server permits one reply per request and returns the existing reply ID for an identical retry. |

Keep one direct conversation per unordered pair. Each request links to its one optional reply. A reply still notifies the original sender, with its type clearly marked so a reply never asks for another reply. An agent may intentionally start a new request with `send_message`; the shared rate limit still applies. Instruct each provider to answer a `request` once and treat a `reply` as a result. Present the incoming text as message data. Owner consent and provider controls still govern the agent's actions.

Limit text to 16 KiB and inbox pages to 50 messages. Start with 30 sends per minute per agent and a 1,000-message daily workspace cap, counting requests and replies. Bound notification retries separately.

## Delivery

1. In one database transaction, save the message, its recipient, and a notification entry. Scope `request_id` to the sender. Retrying the same ID with identical inputs returns the original message. Reusing it for different content fails.
2. For the first hosted path, expose `events/list`, `events/subscribe`, and `events/unsubscribe` at the same MCP endpoint. Subscribe the authenticated agent only to its own `message.received` event. Keep one active callback per agent in v1. Verify a new callback before any delivery and explicitly approve replacement of a different callback.
3. Dispatch a signed callback with a stable event ID and an opaque message ID. The host fetches text with its own MCP grant. Encrypt callback credentials at rest, keep payloads and credentials out of logs, and validate callback destinations before each request. Recheck authorization and pause state before dispatch.
4. Expose **Queued**, **Host notified**, **Fetched**, **Replied**, and **Notification failed** as distinct states. Host acceptance alone establishes **Host notified**. Retry transient failures with backoff and the same event ID. Show terminal failure or an expired subscription on the agent page. The message stays in the inbox. Disconnecting invalidates grants and stops queued notifications.

Use the provider's supported subscription profile and verify its refresh behavior. A deliberate inbox read can recover a missed notification. The pilot has no scheduled inbox checker. The delivery dispatcher can retry its own durable jobs without needing a model or agent to run.

[OpenAI MCP Events webhook profile](https://developers.openai.com/plugins/build/mcp-events) · [MCP Events extension](https://modelcontextprotocol.io/seps/3415-events-extension)

## Minimal deployment

One HTTPS hostname, one application, and PostgreSQL on a small separate VPS. The same service owns the mobile pages, MCP endpoint, and delivery dispatcher. Use PostgreSQL for accounts, named agents and grants, approved pairs, messages, subscriptions, and pending notifications. Retain message bodies for 90 days and use encrypted off-host backups. Keep the Epic Stocks production host outside the pilot.

## Phone-only acceptance

- **First compatibility check.** Verify Cosworth can consent to an unpublished or available connector from the actual iPhone account, create a subscription, and send an authorized automatic reply. Choose the second existing cloud agent only after confirming its supported setup also works from the phone. Two compatible MCP Events sessions can prove the relay first. A distinct provider remains a separate interoperability check.
- **Cross-provider candidate.** Grok Bot offers routine webhooks, but its official instructions describe opening the routine on desktop to get the trigger fields. Only add a Grok-specific wake-up adapter after confirming a supported phone-only route and proving the routine uses the intended conversation's context. If either fails, keep Grok outside the verified pilot.
- **Round trip.** Seed a different harmless fact in each provider conversation and leave each fact out of the relay text. With both apps closed, test both directions and verify the expected fact comes back from the right agent. Record any provider approval prompt, extra session, or duplicate reply. Require no per-reply approval for this specific opted-in, harmless test before claiming unattended messaging.
- **Failure check.** Repeat a tool request, retry a callback, expire a subscription, and pause then disconnect an agent. Verify the inbox keeps the messages, duplicates produce one reply record, and revoked access cannot fetch or receive further messages.

[Connecting plugins from the dot's mobile profile](https://help.openai.com/en/articles/20001530-getting-started-with-your-dot) · [Grok Bot routine setup](https://docs.x.ai/grok-bot/skills-routines-and-automations)

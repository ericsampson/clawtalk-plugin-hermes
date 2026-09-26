# ClawTalk for Hermes

Give your [Hermes Agent](https://hermes-agent.nousresearch.com/) a phone number.

This is a Python port of [`team-telnyx/clawtalk-plugin`](https://github.com/team-telnyx/clawtalk-plugin)
(TypeScript, for OpenClaw) to the Hermes plugin system. Once installed, your
agent can answer calls, place them, send and receive texts, run multi-step
outreach missions, and ask you to approve sensitive actions from your phone —
using the same tools, memory, and personality it uses in chat.

Voice, SMS, and mission traffic arrives over a **single outbound WebSocket** to
[clawdtalk.com](https://www.clawdtalk.com/), so it works behind NAT, inside
Docker, and on a laptop, with no inbound port, tunnel, or public URL.

---

## Install

```bash
git clone https://github.com/team-telnyx/clawtalk-plugin-hermes \
  ~/.hermes/plugins/clawtalk

hermes plugins enable clawtalk
```

The directory **must** be named `clawtalk` — Hermes derives the plugin name
from the folder, and the platform, tools, and skill are all namespaced to it.

Then set your API key (get one at
[clawdtalk.com/portal/dashboard](https://clawdtalk.com/portal/dashboard)) and
enable the platform:

```bash
export CLAWTALK_API_KEY=ct_live_...
hermes gateway
```

Verify:

```bash
hermes clawtalk doctor
```

---

## Configure

Environment variables win over `config.yaml`, which wins over the built-in
defaults.

### `config.yaml`

```yaml
gateway:
  platforms:
    clawtalk:
      enabled: true
      home_channel:
        chat_id: sms:+155****4567
        name: Home
      extra:
        api_key: ct_live_...
        owner_name: Rudra              # used in the greeting and identity block
        agent_name: Daisy              # what the agent calls itself on a call
        greeting: "Hey {ownerName}, what's up?"
        sms_max_reply_chars: 300
        report_call_outcomes: true     # post a summary after each call ends
        missions:
          enabled: true
          default_voice: Rime.ArcanaV3.astra
          default_model: openai/gpt-4o
          observer:
            enabled: true
            interval_s: 300            # how often to check running missions
            cooldown_s: 300            # min gap between nudges for one mission
```

### Environment

| Variable | Default | Purpose |
|---|---|---|
| `CLAWTALK_API_KEY` | — (required) | ClawTalk API key |
| `CLAWTALK_SERVER` | `https://clawdtalk.com` | ClawTalk server URL |
| `CLAWTALK_OWNER_NAME` | `there` | Your name, used in the greeting |
| `CLAWTALK_AGENT_NAME` | `ClawTalk` | Agent name announced on calls |
| `CLAWTALK_GREETING` | `Hey {ownerName}, what's up?` | Inbound call greeting |
| `CLAWTALK_VOICE_CONTEXT` | built-in | Override the voice system prompt |
| `CLAWTALK_AUTO_CONNECT` | `true` | Open the socket at gateway start |
| `CLAWTALK_SMS_MAX_CHARS` | `300` | Hard cap on SMS replies |
| `CLAWTALK_ALLOWED_USERS` | — | Local allowlist of E.164 senders (see below) |
| `CLAWTALK_ALLOW_ALL_USERS` | — | Allow every sender the server delivers |
| `CLAWTALK_HOME_CHANNEL` | — | Cron delivery target, e.g. `sms:+155****4567` |
| `CLAWTALK_MISSIONS_ENABLED` | `true` | Register the eleven mission tools |
| `CLAWTALK_MISSION_OBSERVER_ENABLED` | `true` | Run the background mission observer |
| `CLAWTALK_MISSION_OBSERVER_INTERVAL_S` | `300` | Observer check interval |
| `CLAWTALK_MISSION_COOLDOWN_S` | `300` | Observer nudge cooldown |

`home_channel` is the typed gateway setting for the ClawTalk platform. Its
`chat_id` must be a channel-qualified target such as `sms:+155****4567`; a bare
phone number is not valid. An explicit `CLAWTALK_HOME_CHANNEL` environment
variable takes precedence over the YAML value.

---

## How it works

The OpenClaw plugin ran agent turns out-of-band: a `CoreBridge` dynamically
imported OpenClaw's compiled `extensionAPI.js` and called `runEmbeddedPiAgent`
in-process, managing its own session store to keep each call, contact, and
mission separate.

Hermes has no equivalent seam — and does not need one. A **gateway platform
adapter** already does exactly that job: an inbound event becomes a normal
agent turn with the full tool set, memory, and per-chat session isolation, and
the reply comes back through `adapter.send()`.

So each ClawTalk channel is a namespaced chat id on one platform:

| Channel | chat id | Reply goes out as |
|---|---|---|
| Voice deep-tool request | `call:<call_id>` | `deep_tool_result`, then `response` for follow-ups |
| Walkie-talkie | `walkie:<session>` | `walkie_response` |
| SMS / MMS | `sms:<digits>` | `POST /v1/messages/send` |
| Mission | `mission:<slug>` | nothing — the agent acts through tools |
| Call outcome report | `events:calls` | nothing — informational |

Because Hermes derives the session key from platform + chat id, that table is
also the session-isolation table. Every call, every contact, and every mission
gets its own conversation, which is what the OpenClaw session-key scheme did by
hand.

```
   caller / texter
         │
    Telnyx network
         │
   ClawTalk SaaS  ── persistent WS (outbound from you) ──┐
                                                          ▼
                                          ┌─────────────────────────────┐
                                          │  Hermes gateway             │
                                          │  ┌───────────────────────┐  │
                                          │  │ ClawTalkAdapter       │  │
                                          │  │  ws_service ──┐       │  │
                                          │  │  approvals    │       │  │
                                          │  │  missions     │       │  │
                                          │  │  observer     │       │  │
                                          │  └───────────────┼───────┘  │
                                          │      handle_message()       │
                                          │            ▼                │
                                          │   agent turn (tools,        │
                                          │   memory, per-chat session) │
                                          │            │                │
                                          │      adapter.send()  ───────┼──▶ back out
                                          │                             │
                                          │  21 tools · /clawtalk       │
                                          │  hermes clawtalk · skill    │
                                          └─────────────────────────────┘
```

### Per-channel prompts

Each turn carries a `channel_prompt` — Hermes's ephemeral, per-turn system
prompt, applied at API call time and never written to transcript history:

- **Voice** — full prompt with drip-progress and approval rules.
- **Walkie** — one to three sentences, no markdown.
- **SMS** — hard character cap, plain text only, no emoji.
- **Mission** — the step state machine and what to do with the event.

---

## Tools

All 21 tools register under the `clawtalk` toolset, so you can enable or
disable the whole surface in one place.

| Tool | What it does |
|---|---|
| `clawtalk_call` | Place an outbound call |
| `clawtalk_call_status` | Check a call, or hang it up |
| `clawtalk_sms` | Send a text or MMS |
| `clawtalk_sms_list` | List recent messages |
| `clawtalk_sms_conversations` | List conversations with unread counts |
| `clawtalk_approve` | Ask for approval by push notification, and wait |
| `clawtalk_status` | Connection, version, account, and health |
| `clawtalk_bot_config` | Read/update the bot profile; browse voices |
| `clawtalk_assistants` | CRUD on voice assistants |
| `clawtalk_insights` | AI analysis of a finished call |
| `clawtalk_mission_init` | Create or resume a mission |
| `clawtalk_mission_setup_agent` | Attach an assistant and phone number |
| `clawtalk_mission_schedule` | Queue a call or text against a step |
| `clawtalk_mission_event_status` | Check a scheduled event |
| `clawtalk_mission_cancel_event` | Cancel a scheduled event |
| `clawtalk_mission_update_step` | Advance a plan step |
| `clawtalk_mission_log_event` | Record a note in the event log |
| `clawtalk_mission_memory` | Save, append to, or read mission memory |
| `clawtalk_mission_list` | List local or server-side missions |
| `clawtalk_mission_get_plan` | List step IDs and statuses |
| `clawtalk_mission_complete` | Close a mission with a summary |

Setting `missions.enabled: false` drops the eleven mission tools, so an
install that never runs campaigns does not carry their schemas in every prompt.

The mission playbook ships as a bundled skill:

```
skill_view("clawtalk:missions")
```

---

## Operator surfaces

```bash
hermes clawtalk doctor              # config, credentials, local + server checks
hermes clawtalk logs                # tail the WebSocket traffic log
hermes clawtalk logs --no-follow    # print and exit
hermes clawtalk status              # one-line summary
```

In a session:

```
/clawtalk status
/clawtalk doctor
/clawtalk missions
```

---

## Security

**Authorization is delegated upstream, by design.** The socket is
authenticated with your own API key, and the ClawTalk server applies PIN auth,
caller whitelisting, paranoid mode, and prompt screening *before* it forwards
anything. The sender is a phone number, not a platform account you configure in
Hermes, so the usual `{PLATFORM}_ALLOWED_USERS` allowlist has nothing to match
on — default-denying would drop every legitimate call. The adapter therefore
sets `authorization_is_upstream = True`.

If you want a **local** allowlist on top of that, set `CLAWTALK_ALLOWED_USERS`.
That turns the flag off and hands authorization back to the gateway, which will
then enforce your list.

Other notes:

- Inbound events are dispatched with `allow_gateway_control=False`, so a caller
  or texter can never resolve a gateway slash command such as `/restart`.
- `ws.log` redacts `api_key`, `apiKey`, `authorization`, `token`, and `secret`
  at any depth, rotates at 5 MB, and never logs its own tail requests.
- `hermes clawtalk doctor` refuses a non-HTTPS server, or one whose *hostname*
  does not contain `clawtalk`/`clawdtalk` — your API key travels in every
  request, so a crafted server URL is a credential-exfiltration path.
- The plugin never enables `allow_gateway_injection` or overrides a built-in
  tool.

---

## Development

```bash
pip install -e '.[dev]'
pytest tests
ruff check .
```

The adapter and WebSocket tests need a Hermes checkout importable:

```bash
PYTHONPATH=/path/to/hermes-agent pytest tests
```

Without it those modules skip and the rest of the suite still runs.

Layout:

```
clawtalk/
├── __init__.py            register(ctx): platform, tools, CLI, command, skill
├── adapter.py             gateway platform adapter — chat-id routing
├── ws_service.py          persistent socket: auth, ping/pong, reconnect
├── wire.py                WebSocket protocol: frame builders and event names
├── config.py              env > config.yaml > defaults
├── runtime.py             process-wide service graph
├── cli.py / commands.py   hermes clawtalk … and /clawtalk
├── errors.py              ClawTalkError, ToolError with server fix-hints
├── formatting.py          durations, phone numbers, TTS cleanup
├── ws_logger.py           rotating, redacted traffic log
├── sdk/                   REST client (stdlib only), endpoint map, namespaces
├── services/              voice, approvals, missions, events, observer, doctor
├── tools/                 the 21 agent tools
└── skills/missions/       bundled mission playbook
```

The REST SDK is deliberately synchronous and built on `urllib`, so tool
handlers work on any thread or event loop. `aiohttp` is used only for the
WebSocket, and async callers wrap REST calls in `asyncio.to_thread`.

---

## Differences from the OpenClaw plugin

Behaviour is otherwise a faithful port — same prompts, same protocol, same
mission state machine, same error fix-hints.

1. **No `CoreBridge`.** Agent turns run through the gateway platform pipeline
   instead of a dynamically imported `extensionAPI.js`. This removes the
   OpenClaw port's most fragile piece (walking the filesystem to locate an
   install) and gets session management, authorization, and delivery for free.

2. **The mission observer and the event handler now share a session.** In the
   OpenClaw plugin the observer addressed its session by mission ID while the
   event handler used the slug, so the push and pull halves of the same mission
   talked to two different sessions. Both resolve to the slug here, falling
   back to the mission ID only for a mission with no local state.

3. **Mission state writes are serialised and atomic.** The original did an
   unguarded read-modify-write on `.missions_state.json` from three callers.
   This version takes a lock and writes through a temp file.

4. **Call outcomes go to an `events:calls` session** rather than OpenClaw's
   `enqueueSystemEvent` into the main chat, which has no Hermes equivalent that
   does not require the `allow_gateway_injection` grant. Set
   `report_call_outcomes: false` to turn it off.

5. **No HTTP routes.** OpenClaw's `GET /clawtalk/health` and the stubbed
   `POST /clawtalk/webhook` are gone. Health lives in `hermes clawtalk doctor`,
   `/clawtalk doctor`, and `clawtalk_status`; the webhook stub had no
   implementation to port.

6. **International numbers are shown as E.164.** The TypeScript version used
   `libphonenumber-js`; rather than take that dependency, NANP numbers are
   formatted and everything else is left as the dialable E.164 string.

---

## License

MIT — see [LICENSE](LICENSE).

ClawTalk is a Telnyx product. Hermes Agent is by Nous Research.

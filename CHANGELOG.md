# Changelog

## 0.1.0

Initial release: a Python port of
[`team-telnyx/clawtalk-plugin`](https://github.com/team-telnyx/clawtalk-plugin)
0.2.3 (TypeScript, OpenClaw) to the Hermes Agent plugin system.

### Ported at parity

- Persistent outbound WebSocket with auth, ping/pong keepalive, exponential
  backoff with jitter, and permanent stop on `auth_error` or a duplicate-client
  close (4000).
- Voice: `context_request`, inbound greeting, `deep_tool_request` routing, and
  call-outcome summaries.
- Walkie-talkie push-to-talk.
- SMS and MMS with per-contact sessions and the 300-character reply cap.
- Missions: the full lifecycle, local state file, plan-step state machine,
  memory, real-time `mission.*` events, and the background observer.
- Approvals over push notification, including the `no_devices` /
  `no_devices_reached` distinction and disconnect cleanup.
- All 21 agent tools, with the same names and the same server-error fix-hints.
- Redacted, size-rotated `ws.log` and the server's `request_logs` tail.
- The mission playbook, as the bundled skill `clawtalk:missions`.

### Rearchitected for Hermes

- **`CoreBridge` removed.** Inbound events now run as normal agent turns
  through a gateway platform adapter, which supplies the tool set, memory,
  session isolation, and reply delivery that `CoreBridge` reimplemented by
  dynamically importing OpenClaw's `extensionAPI.js`.
- **Channels are namespaced chat ids** on one `clawtalk` platform
  (`call:`, `walkie:`, `sms:`, `mission:`, `events:calls`), so Hermes derives
  the same per-channel session isolation from its own session keying.
- **Per-turn prompts use `channel_prompt`**, Hermes's ephemeral system prompt,
  which is applied at API call time and never persisted to transcript history.
- **Authorization is declared upstream** (`authorization_is_upstream = True`),
  because ClawTalk gates callers server-side and a phone number is not a
  platform account the operator allowlists. Setting `CLAWTALK_ALLOWED_USERS`
  turns this off and hands authorization back to the gateway.
- **REST SDK is synchronous**, on `urllib` rather than `fetch`, so tool
  handlers are safe on any thread or event loop. `aiohttp` is used only for
  the WebSocket.
- **`libphonenumber-js` and `date-fns` dropped.** Durations are formatted with
  the standard library; NANP numbers are formatted and everything else stays as
  dialable E.164.

### Fixed relative to the original

- The mission observer and the mission event handler now address the **same
  session**. The original keyed the observer by mission ID and the event
  handler by slug, so the push and pull halves of one mission talked past each
  other.
- Mission state writes are **serialised and atomic** (lock plus temp-file
  rename), instead of an unguarded read-modify-write from three callers.
- A **duplicate-client eviction during authentication** is now detected. The
  close-code check previously ran only after a successful auth, so being
  evicted mid-handshake produced an endless reconnect loop fighting the other
  client for the single allowed slot.
- Falsy configuration values (`enabled: false`, `0`) are **no longer silently
  ignored** in favour of the default.

### Not ported

- `GET /clawtalk/health` and the stubbed `POST /clawtalk/webhook`. Health is
  available through `hermes clawtalk doctor`, `/clawtalk doctor`, and
  `clawtalk_status`; the webhook route had no implementation behind it.

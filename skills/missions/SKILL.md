---
name: missions
description: Run a ClawTalk mission - a multi-step outreach campaign of scheduled calls and texts with a plan, memory, and a completion gate. Load this before using any clawtalk_mission_* tool.
---

# ClawTalk Missions

A **mission** is how you run outreach that takes more than one turn: ring three
suppliers for quotes, chase an RSVP over a week, confirm a delivery window.

It gives you four things a bare `clawtalk_call` does not:

- a **plan** of ordered steps with enforced status transitions,
- a dedicated **assistant and phone number** to call and text from,
- **memory** that survives between calls, and
- **events** that wake you up when a call finishes or a text arrives.

For a single call or text right now, do **not** open a mission. Use
`clawtalk_call` or `clawtalk_sms`.

---

## The shape of a mission

```
clawtalk_mission_init          create mission + run + plan   -> slug
clawtalk_mission_setup_agent   assistant + phone number
clawtalk_mission_schedule      queue a call or text per step
        |
        v  (events arrive on their own; you are woken with the transcript)
clawtalk_mission_memory        save what you learned
clawtalk_mission_update_step   advance the step
        |
        v  (repeat until every step is terminal)
clawtalk_mission_complete      summary + cleanup
```

Every tool after `init` takes the **slug** that `init` returned.

---

## 1. Initialise

```json
{
  "name": "Get quotes for the Cork job",
  "instructions": "Ring three suppliers, get a price and lead time for 200 units, report back.",
  "request": "can you find me the best price on 200 units",
  "steps": "[{\"title\": \"Call Murphy Supplies\"}, {\"title\": \"Call Cork Fixings\"}, {\"title\": \"Call Lee Traders\"}]"
}
```

- `steps` is a **JSON string**, not an array.
- The slug is derived from `name`: *"Get quotes for the Cork job"* becomes
  `get-quotes-for-the-cork-job`.
- Step IDs are derived from step titles: *"Call Murphy Supplies"* becomes
  `call-murphy-supplies`.
- Re-running `init` with the same name **resumes** rather than duplicating. If
  you are unsure whether a mission exists, just call `init`.

Write one step per real-world action. A step you cannot mark terminal is a step
that will block completion later.

## 2. Give it a voice and a number

```json
{
  "slug": "get-quotes-for-the-cork-job",
  "name": "Quote runner",
  "instructions": "You are calling suppliers for a trade customer. Ask for the price and lead time on 200 units. Be brief and polite. Do not negotiate.",
  "greeting": "Hi, I'm calling on behalf of a trade customer about a quote."
}
```

The `instructions` here are what the **assistant on the phone** follows - not
what you follow. Write them for someone who will be speaking to a stranger:
what to ask, what not to promise, when to hang up.

Idempotent. If the mission already has an assistant, this returns it unchanged.

## 3. Schedule work

```json
{
  "slug": "get-quotes-for-the-cork-job",
  "channel": "call",
  "to": "+353211234567",
  "scheduled_at": "2026-09-01T09:30:00Z",
  "step_id": "call-murphy-supplies"
}
```

- `channel: "sms"` also requires `text_body`.
- Always pass `step_id`. It is what links the outcome back to the plan.
- `scheduled_at` is ISO 8601 in UTC. For "now", use the current time.

Mark the step `in_progress` when you schedule it.

## 4. React to events

You are woken with the call transcript, the SMS reply, or the failure reason.
When that happens:

1. Read what actually came back. Do not assume the call went as planned.
2. Save anything you will need later with `clawtalk_mission_memory`.
3. Move the step with `clawtalk_mission_update_step`.
4. Decide what is next: schedule the following step, retry, or complete.

Retry-worthy failures (no answer, busy, voicemail) mean reschedule and leave
the step `in_progress`. Terminal failures (disconnected number, refused) mean
mark the step `failed` and move on.

## 5. Remember what you learn

```json
{"action": "save", "slug": "get-quotes-for-the-cork-job", "key": "quote_murphy", "value": "{\"price\": 250, \"lead_time_days\": 5}"}
```

`append` builds a list under one key, which is the right shape for
"everyone I have spoken to so far". `get` reads a key back.

Mission memory is the only thing that carries information between calls. If you
do not save the price you were quoted, it is gone by the next step.

## 6. Complete

```json
{
  "slug": "get-quotes-for-the-cork-job",
  "summary": "Murphy 250 EUR / 5 days, Cork Fixings 240 EUR / 12 days, Lee no answer. Murphy is the best on lead time.",
  "payload": "{\"best\": \"murphy\", \"price\": 250}"
}
```

**Completion is gated.** If any step is still `pending` or `in_progress` the
call is rejected and tells you which ones. Mark them `completed`, `failed`, or
`skipped` first - `skipped` is the honest answer for work that turned out to be
unnecessary.

The summary is what the user reads. Write it for them, not for the log.

---

## The step state machine

```
pending ──▶ in_progress ──▶ completed
   │              │
   │              ├───────▶ failed
   └──────────────┴───────▶ skipped
```

`completed`, `failed`, and `skipped` are **terminal**. Nothing moves out of
them, and nothing moves backwards. The plugin enforces this before the request
reaches the server, so an invalid transition comes back as an explanatory error
rather than a 4xx.

Lost track? `clawtalk_mission_get_plan` lists every step ID with its current
status.

---

## Tool reference

| Tool | Use it to |
|---|---|
| `clawtalk_mission_init` | Create or resume a mission; returns the slug |
| `clawtalk_mission_setup_agent` | Attach an assistant and phone number |
| `clawtalk_mission_schedule` | Queue a call or text against a step |
| `clawtalk_mission_event_status` | Check whether a scheduled event has fired |
| `clawtalk_mission_cancel_event` | Cancel a scheduled event before it fires |
| `clawtalk_mission_update_step` | Move a step through the state machine |
| `clawtalk_mission_memory` | Save, append to, or read mission memory |
| `clawtalk_mission_log_event` | Record a note or outcome in the event log |
| `clawtalk_mission_get_plan` | List step IDs and their status |
| `clawtalk_mission_list` | List local (or, with `server: true`, all) missions |
| `clawtalk_mission_complete` | Close the mission with a summary |
| `clawtalk_insights` | Fetch AI analysis of a finished call by conversation ID |

---

## Failure modes worth knowing

**"No active mission found for slug X."** The slug is wrong or the mission was
already completed - completing clears local state. `clawtalk_mission_list` shows
what is actually live.

**"Mission X has no assistant/phone set up."** You skipped
`clawtalk_mission_setup_agent`, or no phone number was available when it ran.
Its result tells you which.

**"Cannot complete mission: N step(s) still non-terminal."** Exactly what it
says, and it names the steps. This is the guard doing its job - do not work
around it by marking things `completed` that did not happen.

**Nothing is happening.** A background observer checks running missions every
five minutes and will hand you the full mission record if a step looks stuck.
If it has not fired yet, `clawtalk_mission_event_status` tells you whether the
event has even run.

---

## Being a good caller

You are placing real phone calls to real people on the user's behalf.

- Say who you are calling for in the greeting.
- Keep assistant instructions narrow. An assistant told to "help with anything"
  will agree to things the user never authorised.
- Do not schedule calls outside normal hours in the recipient's timezone.
- One call per contact per step. Rescheduling a no-answer is fine; ringing four
  times in ten minutes is not.
- Anything with money or commitment attached goes through `clawtalk_approve`
  first, not through the assistant on the call.

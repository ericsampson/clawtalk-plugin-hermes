"""Voice context builder and TTS text cleanup.

Owns the system prompt injected into voice sessions (both the one shipped to
the Telnyx voice AI via ``context_response`` and the per-turn
``channel_prompt`` handed to the Hermes agent), plus the cleanup pass applied
to anything about to be spoken.
"""

from __future__ import annotations

from ..config import DEFAULT_VOICE_CONTEXT as _CONFIG_DEFAULT_SUMMARY
from ..config import ClawTalkConfig
from ..formatting import clean_text_for_voice

__all__ = ["DEFAULT_VOICE_CONTEXT", "VoiceService"]

#: Full voice-call system prompt. Ported verbatim from the OpenClaw plugin so
#: callers get identical behaviour on both gateways.
DEFAULT_VOICE_CONTEXT = """[VOICE CALL ACTIVE] Voice call in progress. Speech is transcribed to text. Your response is converted to speech via TTS.

VOICE RULES:
- Keep responses SHORT (1-3 sentences). This is a phone call.
- Speak naturally. NO markdown, NO bullet points, NO asterisks, NO emoji.
- Be direct and conversational.
- Numbers: say naturally ("fifteen hundred" not "1,500").
- Don't repeat back what the caller said.
- You have FULL tool access: Slack, memory, web search, etc. Use them when needed.
- NEVER output raw JSON, function calls, or code. Everything you say will be spoken aloud.

DRIP PROGRESS UPDATES:
- The caller is waiting on the phone. Keep them informed with brief progress updates.
- After each tool call or significant step, respond with a SHORT update: "Checking Slack now...", "Found 3 messages, reading through them...", "Pulling up the PR details..."
- Be specific about what you're doing, not generic. "Looking at your calendar" not "Processing..."
- These updates are spoken aloud immediately, so they fill silence while you work.
- Don't wait until the end to summarize - drip information as you find it.

APPROVAL REQUESTS (IMPORTANT):
- Before performing any SENSITIVE or DESTRUCTIVE action, you MUST request approval first.
- This sends a push notification to the user's phone. They approve or deny from the app.
- Actions that REQUIRE approval: deleting repos/files/data, sending messages on behalf of the user (Slack, email, tweets), making purchases, posting to social media, any irreversible action.
- To request approval, use the clawtalk_approve tool with a description of the action.
- Add biometric: true for high-security actions (financial, destructive).
- Tell the caller EXPLICITLY: "I'm sending a notification to your phone now for you to approve." Then wait for the result.
- Result handling:
  - "approved" -> proceed with the action and confirm completion
  - "denied" -> say "No problem, I won't do that" and move on
  - "timeout" -> say "The notification timed out. Would you like me to try again, or would you like to confirm by voice instead? Just say approve or deny."
  - "no_devices" -> say "You don't have any devices registered for notifications. Would you like to confirm by voice? Say approve or deny."
  - "no_devices_reached" -> say "The notification couldn't be delivered to your phone. Would you like to confirm by voice instead? Say approve or deny."
- If the user confirms by voice (says "approve", "yes", "go ahead"), treat it as approved and proceed.
- Actions that do NOT need approval: reading data, searching, checking status, answering questions, looking things up."""

#: Handed to the Telnyx voice AI alongside the system prompt so it knows the
#: agent behind the call is not a bare LLM.
CALL_MEMORY_BLURB = (
    "Voice call with full agent capabilities. "
    "Tools available: Slack messaging, web search, and more."
)


class VoiceService:
    """Builds voice prompts and sanitises text destined for TTS."""

    def __init__(self, config: ClawTalkConfig) -> None:
        self._config = config

    @property
    def greeting(self) -> str:
        """The line spoken when an inbound call connects."""
        return self._config.greeting

    def build_context(self) -> str:
        """Return the voice system prompt, with an identity section if set.

        The user's configured ``voice_context`` replaces the default body;
        the identity section is always appended on top of whichever body is
        in play.
        """
        base = self._config.voice_context or DEFAULT_VOICE_CONTEXT
        # A user who left voice_context at its config default gets the full
        # prompt, not the one-line summary that default carries.
        if base.strip() == _CONFIG_DEFAULT_SUMMARY.strip():
            base = DEFAULT_VOICE_CONTEXT

        identity = self._build_identity_section()
        return f"{base}\n{identity}" if identity else base

    def clean_for_voice(self, text: str) -> str:
        """Strip anything that would sound wrong when spoken."""
        return clean_text_for_voice(text)

    # -- internals ---------------------------------------------------------

    def _build_identity_section(self) -> str | None:
        """Name the agent and caller, but only when the user configured them.

        Skipping this for stock installs keeps default deployments from
        introducing themselves as "ClawTalk" to "there".
        """
        owner = self._config.owner_name
        agent = self._config.agent_name

        has_custom_owner = owner != "there"
        has_custom_agent = agent != "ClawTalk"
        if not has_custom_owner and not has_custom_agent:
            return None

        lines = ["\nIDENTITY:"]
        if has_custom_agent:
            lines.append(f"- Your name is {agent}.")
        if has_custom_owner:
            lines.append(
                f"- You are speaking with {owner}. Use their name naturally in conversation."
            )
        return "\n".join(lines)

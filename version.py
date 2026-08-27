"""Single source of truth for the plugin version.

Sent to the ClawTalk server in the WebSocket ``auth`` frame and the REST
``X-Client-Version`` header, and reported by ``clawtalk_status`` and
``hermes clawtalk doctor``.
"""

__version__ = "0.1.0"

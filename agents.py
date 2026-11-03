"""Agent trigger — writes to queue files picked up by visible worker terminals.

Token clients on other machines (tokens.py) have no shared disk, so their
wake-ups go to an in-memory outbox that client.py fetches by long-poll.
"""

import json
import logging
import threading
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


class RemoteOutbox:
    """Pending wake-ups per token client, fetched via GET /api/agent/wait.

    Entries carry a sequence number; the client acknowledges everything up to
    the last number it processed by passing it as `after` on its next poll.
    `boot` changes on every server start so a client never acknowledges new
    entries with a number left over from the previous run.
    """

    MAX_PENDING = 20

    def __init__(self):
        self._lock = threading.Lock()
        self._queues: dict[str, list[tuple[int, dict]]] = {}
        self._seq = 0
        self.boot = uuid.uuid4().hex[:12]

    def push(self, name: str, entry: dict):
        with self._lock:
            self._seq += 1
            queue = self._queues.setdefault(name, [])
            queue.append((self._seq, entry))
            del queue[:-self.MAX_PENDING]

    def take(self, name: str, after: int = 0, boot: str = "") -> list[tuple[int, dict]]:
        """Drop acknowledged entries (seq <= after) and return the rest."""
        if boot != self.boot:
            after = 0
        with self._lock:
            queue = self._queues.get(name, [])
            queue[:] = [(seq, e) for seq, e in queue if seq > after]
            return list(queue)

    def drop(self, name: str):
        with self._lock:
            self._queues.pop(name, None)


class AgentTrigger:
    def __init__(self, registry, data_dir: str = "./data", remote: RemoteOutbox | None = None):
        self._registry = registry
        self._data_dir = Path(data_dir)
        self._remote = remote

    def is_available(self, name: str) -> bool:
        # Token clients queue while offline and get the wake-up when they reconnect.
        return self._registry.is_registered(name) or self._registry.is_fixed(name)

    def _push_remote(self, agent_name: str, entry: dict) -> bool:
        if self._remote is None or not self._registry.is_fixed(agent_name):
            return False
        self._remote.push(agent_name, entry)
        log.info("Queued @%s remote trigger (ch=%s, job=%s)", agent_name,
                 entry.get("channel"), entry.get("job_id"))
        return True

    def get_status(self) -> dict:
        from mcp_bridge import is_online, is_active, get_role
        instances = self._registry.get_all()
        return {
            name: {
                "available": is_online(name),
                "busy": is_active(name),
                "label": info["label"],
                "color": info["color"],
                "role": get_role(name),
            }
            for name, info in instances.items()
        }

    async def trigger(self, agent_name: str, message: str = "", channel: str = "general",
                      job_id: int | None = None, **kwargs):
        """Write to the agent's queue file. The worker terminal picks it up."""
        queue_file = self._data_dir / f"{agent_name}_queue.jsonl"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        import time
        entry = {
            "sender": message.split(":")[0].strip() if ":" in message else "?",
            "text": message,
            "time": time.strftime("%H:%M:%S"),
            "channel": channel,
        }
        custom_prompt = kwargs.get("prompt", "")
        if isinstance(custom_prompt, str) and custom_prompt.strip():
            entry["prompt"] = custom_prompt.strip()
        if job_id is not None:
            entry["job_id"] = job_id

        if self._push_remote(agent_name, entry):
            return

        with open(queue_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        log.info("Queued @%s trigger (ch=%s, job=%s): %s", agent_name, channel, job_id, message[:80])

    def trigger_sync(self, agent_name: str, message: str = "", channel: str = "general",
                     job_id: int | None = None, **kwargs):
        """Synchronous version of trigger — writes to queue file without async."""
        queue_file = self._data_dir / f"{agent_name}_queue.jsonl"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        import time
        entry = {
            "sender": message.split(":")[0].strip() if ":" in message else "?",
            "text": message,
            "time": time.strftime("%H:%M:%S"),
            "channel": channel,
        }
        custom_prompt = kwargs.get("prompt", "")
        if isinstance(custom_prompt, str) and custom_prompt.strip():
            entry["prompt"] = custom_prompt.strip()
        if job_id is not None:
            entry["job_id"] = job_id

        if self._push_remote(agent_name, entry):
            return

        with open(queue_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        log.info("Queued @%s trigger (ch=%s, job=%s): %s", agent_name, channel, job_id, message[:80])

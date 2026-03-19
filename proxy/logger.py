"""Command audit logging."""

import json
import time
import os


class AuditLogger:
    """Logs every command (allowed or blocked) to commands.jsonl."""

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.commands_file = os.path.join(log_dir, "commands.jsonl")
        self.timeline_file = os.path.join(log_dir, "timeline.jsonl")

    def log_command(self, command: str, allowed: bool, reason: str,
                    result_status: str = None):
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "command": command,
            "allowed": allowed,
            "reason": reason,
            "result_status": result_status,
        }
        self._append(self.commands_file, entry)

    def log_event(self, event_type: str, data: dict):
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event_type,
            **data,
        }
        self._append(self.timeline_file, entry)

    def _append(self, filepath: str, entry: dict):
        with open(filepath, "a") as f:
            f.write(json.dumps(entry) + "\n")

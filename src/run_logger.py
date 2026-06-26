import os
import sys
import json
from datetime import datetime


class _Tee:
    """Duplicates writes to both the original stream and a log file so that
    all print() output goes to the console AND data/runs/.../log.log."""
    def __init__(self, original, log_file):
        self._orig = original
        self._file = log_file

    def write(self, data):
        self._orig.write(data)
        self._file.write(data)
        self._file.flush()

    def flush(self):
        self._orig.flush()
        self._file.flush()

    def isatty(self):
        return False

    def fileno(self):
        return self._orig.fileno()


class RunLogger:
    """Captures per-request artifacts (snapshots, LLM prompts/responses, token & $ costs)
    under a single timestamped directory, so one request's logs can be inspected together."""

    def __init__(self, domain: str, data_dir: str):
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = os.path.join(data_dir, "runs", domain, self.timestamp)
        self.snapshots_dir = os.path.join(self.run_dir, "snapshots")
        self.llm_dir = os.path.join(self.run_dir, "llm")
        os.makedirs(self.snapshots_dir, exist_ok=True)
        os.makedirs(self.llm_dir, exist_ok=True)

        self._iteration = 0
        self.usage_entries = []

        # Tee stdout so every print() in this process also lands in log.log.
        self._log_file = open(os.path.join(self.run_dir, "log.log"), "w", encoding="utf-8")
        self._original_stdout = sys.stdout
        sys.stdout = _Tee(sys.stdout, self._log_file)

    def close(self):
        """Restore stdout and flush/close the log file."""
        if isinstance(sys.stdout, _Tee):
            sys.stdout = self._original_stdout
        self._log_file.close()

    def next_iteration(self) -> int:
        self._iteration += 1
        return self._iteration

    @staticmethod
    def _label(iteration) -> str:
        return f"{iteration:02d}" if isinstance(iteration, int) else str(iteration)

    def log_snapshot(self, iteration: int, snapshot: str):
        path = os.path.join(self.snapshots_dir, f"iter_{self._label(iteration)}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(snapshot)

    def log_llm_call(self, iteration, prompt: str, response_text: str, usage: dict):
        label = self._label(iteration)
        prompt_path = os.path.join(self.llm_dir, f"iter_{label}_prompt.txt")
        response_path = os.path.join(self.llm_dir, f"iter_{label}_response.json")

        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)
        with open(response_path, "w", encoding="utf-8") as f:
            f.write(response_text)

        self.usage_entries.append({"iteration": iteration, **usage})
        self._write_usage()

    def _write_usage(self):
        def total(field):
            return sum(e.get(field) or 0 for e in self.usage_entries)

        summary = {
            "per_call": self.usage_entries,
            "totals": {
                "prompt_token_count": total("prompt_token_count"),
                "candidates_token_count": total("candidates_token_count"),
                "cached_content_token_count": total("cached_content_token_count"),
                "thoughts_token_count": total("thoughts_token_count"),
                "tool_use_prompt_token_count": total("tool_use_prompt_token_count"),
                "total_token_count": total("total_token_count"),
            },
        }
        with open(os.path.join(self.run_dir, "token_usage.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

    def final_screenshot_path(self) -> str:
        return os.path.join(self.run_dir, "final.png")

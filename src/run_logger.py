import os
import json
from datetime import datetime

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

    def next_iteration(self) -> int:
        self._iteration += 1
        return self._iteration

    def log_snapshot(self, iteration: int, snapshot: str):
        path = os.path.join(self.snapshots_dir, f"iter_{iteration:02d}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(snapshot)

    def log_llm_call(self, iteration: int, prompt: str, response_text: str, usage: dict):
        prompt_path = os.path.join(self.llm_dir, f"iter_{iteration:02d}_prompt.txt")
        response_path = os.path.join(self.llm_dir, f"iter_{iteration:02d}_response.json")

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

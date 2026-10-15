"""LLM access. Uses OpenAI chat completions over stdlib HTTP (no extra dependency).

Set OPENAI_API_KEY to enable LLM stages. Without it, every stage uses its
deterministic fallback, so the full pipeline still runs offline.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request


class LLM:
    def __init__(self):
        # Any OpenAI-compatible provider works. Defaults to OpenAI; set SYNERGYRCA_BASE_URL for others.
        self.api_key = os.environ.get("OPENAI_API_KEY")
        self.base_url = os.environ.get("SYNERGYRCA_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.environ.get("SYNERGYRCA_MODEL", "gpt-4o")
        self.calls = 0
        self.total_tokens = 0
        self.last_error = None  # the most recent failure, shown by the harness
        self.disabled = False   # set after a quota failure, so the run stops calling the API

    @property
    def available(self) -> bool:
        return bool(self.api_key) and not self.disabled

    def complete(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}",
                     # Some providers (Groq via Cloudflare) block Python's default User-Agent.
                     "User-Agent": "Mozilla/5.0 (SynergyRCA evaluation client)"},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")[:400]
                if e.code == 429 and "insufficient_quota" not in body and attempt < 2:
                    time.sleep(5 * (attempt + 1))  # plain rate limit: wait and retry
                    continue
                if e.code == 429:
                    self.disabled = True  # quota or billing problem: stop calling for this run
                raise RuntimeError(f"HTTP {e.code} from {self.base_url}: {body}")
        self.calls += 1
        self.total_tokens += data.get("usage", {}).get("total_tokens", 0)
        return data["choices"][0]["message"]["content"]

    def ask_json(self, system: str, user: str):
        """Return parsed JSON from the model, or None if unavailable or malformed."""
        if not self.available:
            return None
        try:
            raw = self.complete(system, user)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return None
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

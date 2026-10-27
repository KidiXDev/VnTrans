"""Model provider adapters: each maps one wire protocol to a neutral request and reply."""

from dataclasses import asdict, dataclass, field
import json
import os
import re
import urllib.error
import urllib.request
from typing import Protocol

REASONING = ("default", "minimal", "low", "high")
# Root object: strict JSON-schema modes reject a top-level array.
SCHEMA = {
    "type": "object",
    "properties": {"translations": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "t": {"type": "string"}},
        "required": ["i", "t"], "additionalProperties": False}}},
    "required": ["translations"], "additionalProperties": False,
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"translations": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "t": {"type": "string"}, "r": {"type": "string"}},
        "required": ["i", "t", "r"], "additionalProperties": False}}},
    "required": ["translations"], "additionalProperties": False,
}
TERMS_SCHEMA = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {
        "type": "object",
        "properties": {"source": {"type": "string"}, "target": {"type": "string"},
                       "kind": {"type": "string", "enum": ["character", "term"]}, "notes": {"type": "string"}},
        "required": ["source", "target", "kind", "notes"], "additionalProperties": False}}},
    "required": ["terms"], "additionalProperties": False,
}


@dataclass
class Profile:
    name: str
    provider: str = "gemini"
    model: str = ""
    keys: list[str] = field(default_factory=list)
    temperature: float = 0.3
    max_output_tokens: int = 8192
    reasoning: str = "default"
    timeout: int = 120
    show_thinking: bool = True

    @classmethod
    def load(cls, value: dict) -> "Profile":
        known = {key: value[key] for key in cls.__dataclass_fields__ if key in value}
        return cls(**known)

    def dump(self) -> dict:
        return asdict(self)

    def api_keys(self) -> list[str]:
        """Keys from VNTRANS_<NAME>_KEY (comma-separated) when set, else the stored keys."""
        variable = "VNTRANS_" + re.sub(r"[^A-Z0-9]+", "_", self.name.upper()).strip("_") + "_KEY"
        keys = os.environ.get(variable, "").split(",") if os.environ.get(variable) else self.keys
        return [key.strip() for key in keys if key.strip()]


@dataclass
class Reply:
    text: str
    finish: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    thinking: str = ""
    latency: float = 0.0


class ProviderError(Exception):
    """kind: auth/request stop a run; rate/transient are retried; blocked is a content failure."""

    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


def classify(status: int, body: str, retry_after: str | None) -> ProviderError:
    try:
        error = json.loads(body).get("error", {})
        message = error.get("message") or body[:300]
    except (ValueError, AttributeError):
        message = body[:300] or f"HTTP {status}"
    delay = None
    if retry_after and retry_after.strip().isdigit():
        delay = float(retry_after)
    elif found := re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', body):
        delay = float(found[1])
    message = f"HTTP {status}: {message}"
    if status in (401, 402) or (status in (400, 403) and "API_KEY" in body):
        return ProviderError("auth", message)
    if status == 403:
        return ProviderError("blocked", message)
    if status == 429:
        return ProviderError("rate", message, delay)
    if status in (408, 409) or status >= 500:
        return ProviderError("transient", message, delay)
    return ProviderError("request", message)


def call(url, *, headers, body=None, timeout=60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise classify(exc.code, exc.read().decode("utf-8", "replace"), exc.headers.get("Retry-After")) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise ProviderError("transient", f"network error: {getattr(exc, 'reason', exc)}") from None
    except ValueError as exc:
        raise ProviderError("transient", f"unreadable response: {exc}") from None


class Provider(Protocol):
    label: str

    def complete(self, profile: Profile, key: str, system: str, user: str, schema: dict = SCHEMA) -> Reply: ...

    def models(self, profile: Profile, key: str) -> list[str]: ...


def upper_types(schema):
    """Gemini's responseSchema uses OpenAPI enum names and has no additionalProperties."""
    if isinstance(schema, dict):
        return {key: (value.upper() if key == "type" else upper_types(value))
                for key, value in schema.items() if key != "additionalProperties"}
    return schema


class Gemini:
    label = "Google Gemini"
    base = "https://generativelanguage.googleapis.com/v1beta"
    safety = ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
              "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
    budgets = dict(minimal=0, low=1024, high=8192)

    def payload(self, profile, system, user, schema=SCHEMA):
        config = dict(temperature=profile.temperature, maxOutputTokens=profile.max_output_tokens,
                      responseMimeType="application/json", responseSchema=upper_types(schema))
        model = profile.model.removeprefix("models/")
        if profile.reasoning != "default":
            # thinkingLevel is rejected by Gemini 2.x models, which take a token budget instead.
            config["thinkingConfig"] = (dict(thinkingBudget=self.budgets[profile.reasoning])
                                        if model.startswith("gemini-2")
                                        else dict(thinkingLevel=profile.reasoning.upper()))
        # Gemini 1.x and 2.0 models reject any thinking config.
        if profile.show_thinking and not re.match(r"gemini-(1\.|2\.0)", model):
            config.setdefault("thinkingConfig", {})["includeThoughts"] = True
        return dict(systemInstruction=dict(parts=[dict(text=system)]),
                    contents=[dict(role="user", parts=[dict(text=user)])],
                    generationConfig=config,
                    safetySettings=[dict(category=c, threshold="BLOCK_NONE") for c in self.safety])

    def parse(self, value) -> Reply:
        feedback = value.get("promptFeedback", {})
        if feedback.get("blockReason"):
            raise ProviderError("blocked", f"prompt blocked: {feedback['blockReason']}")
        candidates = value.get("candidates") or []
        if not candidates:
            raise ProviderError("transient", "empty response")
        candidate = candidates[0]
        reason = candidate.get("finishReason", "STOP")
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        thinking = "".join(part.get("text", "") for part in parts if part.get("thought"))
        if reason not in ("STOP", "MAX_TOKENS") and not text:
            raise ProviderError("blocked", f"reply blocked: {reason}")
        usage = value.get("usageMetadata", {})
        out = (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0)
        return Reply(text, "length" if reason == "MAX_TOKENS" else "stop", usage.get("promptTokenCount"), out or None,
                     thinking.strip())

    def complete(self, profile, key, system, user, schema=SCHEMA):
        model = profile.model.removeprefix("models/")
        return self.parse(call(f"{self.base}/models/{model}:generateContent", headers={"x-goog-api-key": key},
                               body=self.payload(profile, system, user, schema), timeout=profile.timeout))

    def models(self, profile, key):
        names, token = [], ""
        while True:
            value = call(f"{self.base}/models?pageSize=1000" + (f"&pageToken={token}" if token else ""),
                         headers={"x-goog-api-key": key}, timeout=profile.timeout)
            names += [model["name"].removeprefix("models/") for model in value.get("models", [])
                      if "generateContent" in model.get("supportedGenerationMethods", [])]
            token = value.get("nextPageToken")
            if not token:
                return sorted(names)


class OpenRouter:
    label = "OpenRouter"
    base = "https://openrouter.ai/api/v1"
    headers = {"HTTP-Referer": "https://github.com/KidiXDev/VnTrans", "X-OpenRouter-Title": "VnTrans"}

    def payload(self, profile, system, user, schema=SCHEMA):
        body = dict(model=profile.model, temperature=profile.temperature, max_tokens=profile.max_output_tokens,
                    messages=[dict(role="system", content=system), dict(role="user", content=user)],
                    response_format=dict(type="json_schema",
                                         json_schema=dict(name="reply", strict=True, schema=schema)))
        if profile.reasoning != "default":
            body["reasoning"] = dict(effort=profile.reasoning, exclude=not profile.show_thinking)
        elif not profile.show_thinking:
            body["reasoning"] = dict(exclude=True)
        return body

    def parse(self, value) -> Reply:
        if "error" in value:
            error = value["error"]
            raise classify(int(error.get("code") or 502), json.dumps(value), None)
        choices = value.get("choices") or []
        if not choices:
            raise ProviderError("transient", "empty response")
        choice = choices[0]
        if choice.get("error"):
            raise classify(int(choice["error"].get("code") or 502), json.dumps(choice), None)
        message = choice.get("message", {})
        text = message.get("content") or ""
        thinking = message.get("reasoning") or ""
        reason = choice.get("finish_reason") or "stop"
        if reason == "content_filter" and not text:
            raise ProviderError("blocked", "reply blocked by content filter")
        usage = value.get("usage") or {}
        return Reply(text, "length" if reason == "length" else "stop",
                     usage.get("prompt_tokens"), usage.get("completion_tokens"), thinking.strip())

    def complete(self, profile, key, system, user, schema=SCHEMA):
        return self.parse(call(f"{self.base}/chat/completions",
                               headers={"Authorization": f"Bearer {key}", **self.headers},
                               body=self.payload(profile, system, user, schema), timeout=profile.timeout))

    def models(self, profile, key):
        value = call(f"{self.base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=profile.timeout)
        return sorted(model["id"] for model in value.get("data", []))


PROVIDERS: dict[str, Provider] = {"gemini": Gemini(), "openrouter": OpenRouter()}

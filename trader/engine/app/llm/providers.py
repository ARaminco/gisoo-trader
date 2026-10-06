"""LLM back ends behind the advisor.

One contract, three providers. `complete_json(system, user, schema, max_tokens)`
takes the byte-stable system prompt, the user message and the Pydantic model
the answer must satisfy, and returns `(parsed, usage)`. `parsed` is None when
the model answered but the answer was unusable (a refusal, JSON that never
validated). Transport and API failures raise: the advisor decides what a
failure means (try the fallback provider, then fail open).

  * AnthropicProvider - the official SDK's `messages.parse`: the schema is
    enforced server side and the system prompt is cached.
  * OpenAIProvider / DeepSeekProvider - OpenAI-compatible chat completions
    over httpx. JSON mode only guarantees syntax there, so the schema is
    described in the system prompt, the reply is validated locally, and a
    rejected reply is retried once with the validation error attached.

Nothing here logs the key or the prompt: errors carry the HTTP status and the
API's own message, never the request.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.config import settings

log = logging.getLogger(__name__)

Usage = dict[str, Any]

# Sampling for the JSON back ends: the advisor wants the same read of the
# same numbers, not creativity.
TEMPERATURE = 0.2
# Per-request httpx limits. The advisor bounds each call harder with its own
# deadline; these only stop a dead socket from hanging a worker. Connect is
# short on purpose: a black-holed endpoint pays it twice (model listing, then
# the completion) before the fallback provider gets its turn.
HTTP_TIMEOUT = 60.0
CONNECT_TIMEOUT = 5.0
# Longest pause honoured from a Retry-After header before giving up on 429.
MAX_RETRY_AFTER = 10.0
DEFAULT_RETRY_AFTER = 2.0
# OpenAI reasoning models spend max_completion_tokens on thinking too; without
# headroom a low-effort answer can still be cut off mid-JSON.
REASONING_HEADROOM = 4000
# In order of preference; the first one the key can list wins.
OPENAI_PREFERRED_MODELS = ("gpt-5-mini", "gpt-5", "gpt-4.1-mini", "gpt-4o-mini")


class LLMProviderError(RuntimeError):
    """An API or transport failure the caller may want to fall back on."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# schema description for JSON-mode providers
# --------------------------------------------------------------------------


def json_shape(schema: type[BaseModel]) -> str:
    """One-line shape of the model, e.g. {"stance": "risk_on"|"neutral", ...}.

    JSON mode guarantees syntax, not keys, so the model must be told the shape.
    The full JSON Schema would cost several hundred tokens per call; this
    keeps enums, ranges and length caps in a form that reads like an example.
    """
    root = schema.model_json_schema()
    defs = root.get("$defs", {})

    def render(node: dict) -> str:
        if "$ref" in node:
            node = defs[node["$ref"].rsplit("/", 1)[-1]]
        if "enum" in node:
            return "|".join(json.dumps(v) for v in node["enum"])
        if "anyOf" in node:
            return "|".join(render(option) for option in node["anyOf"])
        kind = node.get("type")
        if kind == "object":
            fields = ", ".join(
                f'"{key}": {render(prop)}' for key, prop in node.get("properties", {}).items()
            )
            return "{" + fields + "}"
        if kind == "array":
            cap = node.get("maxItems")
            return f"[{render(node.get('items', {}))}]" + (f" (max {cap} items)" if cap else "")
        if kind in ("number", "integer"):
            low, high = node.get("minimum"), node.get("maximum")
            return kind + (f" {low}..{high}" if low is not None and high is not None else "")
        if kind == "string":
            cap = node.get("maxLength")
            return "string" + (f" (max {cap} chars)" if cap else "")
        return kind or "any"

    return render(root)


def json_shape_instruction(schema: type[BaseModel]) -> str:
    """Appended to the system prompt. Stable per schema, so prefix caches survive."""
    return (
        "Output format: reply with exactly one JSON object and nothing else - "
        "no prose, no markdown fences. Shape (every key required, types as written):\n"
        + json_shape(schema)
    )


def strict_json_schema(schema: type[BaseModel]) -> dict:
    """The Pydantic schema in the dialect OpenAI's strict structured outputs
    accept: every object closed, every property required, length caps dropped."""

    def tighten(node: Any) -> Any:
        if isinstance(node, list):
            return [tighten(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {
            key: tighten(value)
            for key, value in node.items()
            if key not in ("title", "default", "maxLength", "minLength", "examples")
        }
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out

    return tighten(schema.model_json_schema())


def describe_error(exc: BaseException) -> str:
    """str(exc), or the class name when that is empty (httpx timeouts are)."""
    return str(exc) or type(exc).__name__


def _validation_summary(exc: ValidationError) -> str:
    problems = [
        f"{'.'.join(str(part) for part in err.get('loc', ())) or '$'}: {err.get('msg', '')}"
        for err in exc.errors()[:5]
    ]
    return "; ".join(problems)[:400]


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _retry_after(header: str | None) -> float:
    try:
        return min(max(float(header or ""), 0.0), MAX_RETRY_AFTER)
    except ValueError:
        return DEFAULT_RETRY_AFTER


def _api_error(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
        message = error.get("message") if isinstance(error, dict) else error
        if message:
            return str(message)[:200]
    except Exception:  # noqa: BLE001 - not JSON
        pass
    return response.text[:200]


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


class AnthropicProvider:
    """The original back end: server-side schema enforcement, cached system prompt."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str, effort: str, base_url: str = "") -> None:
        import anthropic  # only this path needs the SDK

        self.model = model
        self._effort = effort
        # One retry at most: a degraded API must fail within a couple of
        # minutes, and the trade review is capped harder by its caller.
        # base_url: an Anthropic-compatible gateway such as https://gisoo.pro/api.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=1, timeout=60.0, base_url=base_url or None
        )

    async def complete_json(
        self, system: str, user: str, schema: type[BaseModel], max_tokens: int
    ) -> tuple[BaseModel | None, Usage]:
        response = await self._client.messages.parse(
            model=self.model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={"effort": self._effort},
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        usage = {
            "input_tokens": getattr(response.usage, "input_tokens", 0),
            "output_tokens": getattr(response.usage, "output_tokens", 0),
            "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
        }
        if getattr(response, "stop_reason", None) == "refusal":
            log.warning("anthropic refused the %s request", schema.__name__)
            return None, usage
        return response.parsed_output, usage


# --------------------------------------------------------------------------
# OpenAI-compatible chat completions (OpenAI, DeepSeek)
# --------------------------------------------------------------------------


class ChatCompletionsProvider:
    """Shared `/chat/completions` flow; subclasses shape the payload."""

    name = "openai-compatible"

    def __init__(self, api_key: str, model: str, base_url: str, proxy: str | None) -> None:
        self.model = model
        base = base_url.rstrip("/")
        self._completions_url = f"{base}/chat/completions"
        self._models_url = f"{base}/models"
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "GisooTrader/0.1"},
            timeout=httpx.Timeout(HTTP_TIMEOUT, connect=CONNECT_TIMEOUT),
            proxy=proxy,
        )

    # -- subclass hooks --

    def _payload(self, messages: list[dict], schema: type[BaseModel], max_tokens: int) -> dict:
        raise NotImplementedError

    # -- shared flow --

    async def complete_json(
        self, system: str, user: str, schema: type[BaseModel], max_tokens: int
    ) -> tuple[BaseModel | None, Usage]:
        messages = [
            {"role": "system", "content": system + "\n\n" + json_shape_instruction(schema)},
            {"role": "user", "content": user},
        ]
        usage: Usage = {}
        problem: str | None = None
        for attempt in (1, 2):
            if problem:
                # Second try: the same question with the rejection attached.
                messages[1] = {
                    "role": "user",
                    "content": (
                        f"{user}\n\nYour previous reply was rejected: {problem}\n"
                        "Reply again with one valid JSON object only."
                    ),
                }
            data = await self._post(self._payload(messages, schema, max_tokens))
            usage = self._usage(data)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            if message.get("refusal"):
                log.warning("%s refused the %s request", self.name, schema.__name__)
                return None, usage
            content = _strip_fences(message.get("content") or "")
            if not content:
                problem = "empty reply"
            else:
                try:
                    return schema.model_validate_json(content), usage
                except ValidationError as exc:
                    problem = _validation_summary(exc)
                    if choice.get("finish_reason") == "length":
                        problem = f"cut off at the token limit, be shorter; {problem}"
            log.warning(
                "%s %s attempt %d unusable: %s", self.name, schema.__name__, attempt, problem
            )
        return None, usage

    async def _post(self, payload: dict) -> dict:
        for attempt in (1, 2):
            response = await self._client.post(self._completions_url, json=payload)
            if response.status_code == 429 and attempt == 1:
                delay = _retry_after(response.headers.get("retry-after"))
                log.warning("%s rate limited; retrying in %.1fs", self.name, delay)
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 400:
                raise LLMProviderError(
                    f"{self.name} HTTP {response.status_code}: {_api_error(response)}",
                    status=response.status_code,
                )
            return response.json()
        raise LLMProviderError(f"{self.name} still rate limited after one retry", status=429)

    @staticmethod
    def _usage(data: dict) -> Usage:
        raw = data.get("usage") or {}
        details = raw.get("prompt_tokens_details") or {}
        return {
            "input_tokens": raw.get("prompt_tokens", 0),
            "output_tokens": raw.get("completion_tokens", 0),
            # DeepSeek reports its prefix cache as prompt_cache_hit_tokens,
            # OpenAI as prompt_tokens_details.cached_tokens.
            "cache_read_input_tokens": raw.get("prompt_cache_hit_tokens")
            or details.get("cached_tokens", 0),
        }

    async def list_models(self) -> set[str]:
        response = await self._client.get(self._models_url)
        if response.status_code >= 400:
            raise LLMProviderError(
                f"{self.name} HTTP {response.status_code}: {_api_error(response)}",
                status=response.status_code,
            )
        return {item.get("id") for item in response.json().get("data", []) if item.get("id")}


class DeepSeekProvider(ChatCompletionsProvider):
    """DeepSeek JSON mode: syntax only, hence the shape in the prompt."""

    name = "deepseek"

    def _payload(self, messages: list[dict], schema: type[BaseModel], max_tokens: int) -> dict:
        return {
            "model": self.model,
            "messages": messages,
            "temperature": TEMPERATURE,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }


class GatewayProvider(DeepSeekProvider):
    """An OpenAI-compatible gateway in front of many vendors' models.

    Gisoo (https://gisoo.pro/api/v1) and the NetArz AI API
    (https://netarz.ir/api/ai/v1) route one key to GPT, Claude, Gemini,
    DeepSeek and more, billed in Toman. Strict json_schema support varies by
    the model behind the gateway, so this uses plain JSON mode with the shape
    spelled out in the prompt - the path every model handles.
    """

    def __init__(self, name: str, api_key: str, model: str, base_url: str, proxy: str | None) -> None:
        super().__init__(api_key, model, base_url, proxy)
        self.name = name


def _is_reasoning_model(model: str) -> bool:
    name = model.lower()
    # gpt-5-chat-* is the non-reasoning chat variant and takes temperature.
    return name.startswith(("o1", "o3", "o4")) or (name.startswith("gpt-5") and "chat" not in name)


def _openai_effort(effort: str) -> str:
    # Anthropic's ladder is low/medium/high/xhigh/max; OpenAI's stops at high.
    return effort if effort in ("low", "medium", "high") else "high"


class OpenAIProvider(ChatCompletionsProvider):
    """OpenAI chat completions. Strict structured outputs when the model takes
    them, plain JSON mode otherwise; reasoning models get reasoning_effort and
    no temperature."""

    name = "openai"

    def __init__(
        self, api_key: str, model: str, base_url: str, proxy: str | None, effort: str
    ) -> None:
        super().__init__(api_key, model, base_url, proxy)
        self._effort = effort
        self._discovery = asyncio.Lock()
        # A configured model is taken as is; a discovered or guessed one is
        # confirmed by its first successful completion (see _ensure_model).
        self._model_confirmed = bool(model)
        # None = not yet known; a 400 about response_format flips it to False.
        self._strict_json: bool | None = None

    async def complete_json(
        self, system: str, user: str, schema: type[BaseModel], max_tokens: int
    ) -> tuple[BaseModel | None, Usage]:
        await self._ensure_model()
        try:
            result = await super().complete_json(system, user, schema, max_tokens)
        except LLMProviderError as exc:
            if not (self._strict_json is None and exc.status == 400 and _mentions_schema(str(exc))):
                raise
            log.warning("openai: %s does not take strict json_schema; using json_object", self.model)
            self._strict_json = False
            result = await super().complete_json(system, user, schema, max_tokens)
        if self._strict_json is None:
            self._strict_json = True
        self._model_confirmed = True
        return result

    async def _ensure_model(self) -> None:
        """Pick the model from what the key can actually list.

        Runs until a listing succeeds or a completion goes through on the
        guess: a restricted key may not be allowed to list models at all, and
        a network blip at startup must not pin a wrong guess for good.
        """
        if self._model_confirmed:
            return
        async with self._discovery:
            if self._model_confirmed:
                return
            try:
                available = await self.list_models()
            except Exception as exc:  # noqa: BLE001 - listing is a convenience
                self.model = self.model or OPENAI_PREFERRED_MODELS[0]
                log.warning(
                    "openai: could not list models (%s); trying %s",
                    describe_error(exc)[:200], self.model,
                )
                return
            for candidate in OPENAI_PREFERRED_MODELS:
                if candidate in available:
                    log.info("openai: using %s", candidate)
                    self.model = candidate
                    self._model_confirmed = True
                    return
            self.model = self.model or OPENAI_PREFERRED_MODELS[0]
            log.warning(
                "openai: none of %s listed for this key; trying %s",
                ", ".join(OPENAI_PREFERRED_MODELS), self.model,
            )

    def _payload(self, messages: list[dict], schema: type[BaseModel], max_tokens: int) -> dict:
        payload: dict = {"model": self.model, "messages": messages, "stream": False}
        if _is_reasoning_model(self.model):
            payload["reasoning_effort"] = _openai_effort(self._effort)
            payload["max_completion_tokens"] = max_tokens + REASONING_HEADROOM
        else:
            payload["temperature"] = TEMPERATURE
            payload["max_completion_tokens"] = max_tokens
        if self._strict_json is False:
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": strict_json_schema(schema),
                },
            }
        return payload


def _mentions_schema(message: str) -> bool:
    text = message.lower()
    return any(word in text for word in ("response_format", "json_schema", "schema", "structured"))


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------


def build_provider(name: str, model: str) -> AnthropicProvider | ChatCompletionsProvider:
    """A provider for `name` using the key and URLs from settings."""
    key = settings.llm_api_key(name)
    if not key:
        raise LLMProviderError(f"no API key for {name}")
    proxy = settings.llm_http_proxy_url
    if name == "gisoo":
        return GatewayProvider("gisoo", key, model, settings.gisoo_base_url, proxy)
    if name == "netarz":
        return GatewayProvider("netarz", key, model, settings.netarz_ai_base_url, proxy)
    if name == "anthropic":
        return AnthropicProvider(key, model, settings.llm_effort, settings.anthropic_base_url)
    if name == "openai":
        return OpenAIProvider(key, model, settings.openai_base_url, proxy, settings.llm_effort)
    if name == "deepseek":
        return DeepSeekProvider(key, model, settings.deepseek_base_url, proxy)
    raise LLMProviderError(f"unknown LLM provider {name!r}")

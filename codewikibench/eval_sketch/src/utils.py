import asyncio
import json
import logging
import httpx
from openai import AsyncOpenAI

class _FirstOnlyFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self._seen = False

    def filter(self, record):
        if "HTTP Request" in record.getMessage():
            if self._seen:
                return False
            self._seen = True
        return True

logging.getLogger("httpx").addFilter(_FirstOnlyFilter())
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
import tiktoken

import config


enc = tiktoken.encoding_for_model("gpt-4")

_first_request_logged = False


async def _log_first_request(request: httpx.Request) -> None:
    global _first_request_logged
    if _first_request_logged:
        return
    _first_request_logged = True
    try:
        body = json.loads(request.content)
        preview = {
            "url": str(request.url),
            "model": body.get("model"),
            "messages_count": len(body.get("messages", [])),
            "temperature": body.get("temperature"),
            "max_tokens": body.get("max_tokens"),
            "max_completion_tokens": body.get("max_completion_tokens"),
        }
        print(f"[FIRST API REQUEST] {json.dumps(preview, indent=2)}")
    except Exception:
        pass


_FIREWORKS_MODEL_ALIASES = {
    "kimi-k2-instruct": "accounts/fireworks/models/kimi-k2p5",
}

def _normalize_model_name(model: str, base_url: str) -> str:
    if base_url and "fireworks.ai" in base_url:
        if model in _FIREWORKS_MODEL_ALIASES:
            return _FIREWORKS_MODEL_ALIASES[model]
    if model.startswith("gpt-") and base_url and "api.openai.com" not in base_url:
        return f"openai/{model}"
    return model


def _preferred_token_field(model_name: str) -> str:
    if model_name.startswith("gpt-5"):
        return "max_completion_tokens"
    return "max_tokens"


def _fallback_token_field(field_name: str) -> str:
    return "max_completion_tokens" if field_name == "max_tokens" else "max_tokens"


def _is_unsupported_token_param_error(error: Exception) -> bool:
    message = str(error).lower()
    return "unsupported parameter" in message and ("max_tokens" in message or "max_completion_tokens" in message)

def truncate_tokens(text: str) -> str:
    tokens = enc.encode(text)
    length = len(tokens)
    if length > config.MAX_TOKENS_PER_TOOL_RESPONSE:
        text = enc.decode(tokens[:config.MAX_TOKENS_PER_TOOL_RESPONSE])
        text += "\n... [truncated because it exceeds the max tokens limit, try deeper paths]"
    return text

def get_llm(model: str) -> OpenAIChatModel:
    normalized = _normalize_model_name(model, config.BASE_URL)

    primary_field = _preferred_token_field(normalized)
    secondary_field = _fallback_token_field(primary_field)

    settings = None
    for field_name in (primary_field, secondary_field):
        try:
            settings = OpenAIChatModelSettings(
                temperature=0.0,
                timeout=300,
                **{field_name: 16000},
            )
            break
        except Exception:
            continue

    if settings is None:
        settings = OpenAIChatModelSettings(
            temperature=0.0,
            timeout=300,
        )

    http_client = httpx.AsyncClient(event_hooks={"request": [_log_first_request]})
    openai_client = AsyncOpenAI(
        base_url=config.BASE_URL,
        api_key=config.API_KEY,
        http_client=http_client,
    )

    return OpenAIChatModel(
        model_name=normalized,
        provider=OpenAIProvider(openai_client=openai_client),
        settings=settings
    )

async def run_llm_natively(model: str, prompt: str = None, messages: list[dict] = None) -> str:
    http_client = httpx.AsyncClient(event_hooks={"request": [_log_first_request]})
    client = AsyncOpenAI(
        base_url=config.BASE_URL,
        api_key=config.API_KEY,
        http_client=http_client,
    )

    if messages is None:
        messages = [{"role": "user", "content": prompt}]

    model_name = _normalize_model_name(model, config.BASE_URL)
    primary_field = _preferred_token_field(model_name)
    secondary_field = _fallback_token_field(primary_field)

    response = None
    last_error = None
    for field_name in (primary_field, secondary_field):
        try:
            response = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                **{field_name: 16000},
            )
            break
        except Exception as e:
            last_error = e
            if _is_unsupported_token_param_error(e):
                continue
            raise

    if response is None:
        if last_error is not None:
            raise last_error
        response = await client.chat.completions.create(
            model=model_name,
            messages=messages,
        )


    return response.choices[0].message.content

if __name__ == "__main__":
    result = asyncio.run(run_llm_natively(model="gpt-oss-120b", messages=[{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "Hello, world!"}]))
    print(result)


async def get_embeddings(texts: list[str]) -> list[list[float]]:
    client = AsyncOpenAI(
        base_url=config.BASE_URL,
        api_key=config.API_KEY,
    )
    response = await client.embeddings.create(
        input=texts,
        model=config.EMBEDDING_MODEL,
    )

    return [embedding.embedding for embedding in response.data]

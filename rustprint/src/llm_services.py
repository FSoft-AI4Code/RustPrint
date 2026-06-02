import json
import logging
import os
from typing import Any
import httpx
import openai as openai_lib
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.models.openai import OpenAIModelSettings
from openai import OpenAI

from rustprint.src.config import Config

logger = logging.getLogger(__name__)

_first_request_logged = False
_MODEL_ALIASES = {
    'kimi-k2-instruct':  "accounts/fireworks/models/kimi-k2-instruct-0905",
}



async def _log_first_request(request: httpx.Request) -> None:
    global _first_request_logged
    if _first_request_logged:
        return
    _first_request_logged = True
    try:
        body = json.loads(request.content)
        preview = {
            'url': str(request.url),
            'model': body.get('model'),
            'messages_count': len(body.get('messages', [])),
            'temperature': body.get('temperature'),
            'max_tokens': body.get('max_tokens'),
            'max_completion_tokens': body.get('max_completion_tokens'),
        }
        logger.info(f"[FIRST API REQUEST] {json.dumps(preview, indent=2)}")
    except Exception:
        pass


def is_openai_model(model_name: str) -> bool:
    """Check if model is an OpenAI model (gpt-*)."""
    return model_name.startswith('gpt-') or model_name.startswith('openai/')


def _resolve_model_alias(model: str) -> str:
    return _MODEL_ALIASES.get(model, model)


def _resolve_endpoint_for_model(model: str, base_url: str, api_key: str) -> tuple[str, str]:
    if is_openai_model(model):
        openai_base = os.getenv('OPENAI_BASE_URL', '')
        openai_key = os.getenv('OPENAI_API_KEY', '')
        if openai_base and openai_key:
            logger.info(f"[llm] route model={model} provider=openai source=OPENAI_*")
            return openai_base, openai_key
        logger.info(f"[llm] route model={model} provider=openai source=config")
        return base_url, api_key

    fw_base = os.getenv('FIREWORKS_BASE_URL', '')
    fw_key = os.getenv('FIREWORKS_API_KEY', '')
    if fw_base and fw_key:
        logger.info(f"[llm] route model={model} provider=fireworks source=FIREWORKS_*")
        return fw_base, fw_key

    base_from_generic = os.getenv('LLM_BASE_URL', '')
    key_from_generic = os.getenv('LLM_API_KEY', '')
    if base_from_generic and key_from_generic and 'anthropic.com' not in base_from_generic.lower():
        logger.info(f"[llm] route model={model} provider=fireworks source=LLM_*")
        return base_from_generic, key_from_generic

    raise ValueError(
        f"Non-GPT model '{model}' requires FIREWORKS_BASE_URL and FIREWORKS_API_KEY (or non-Anthropic LLM_BASE_URL/LLM_API_KEY)."
    )


def _normalize_model_name(model: str, base_url: str) -> str:
    if model.startswith('gpt-') and base_url and 'api.openai.com' not in base_url:
        return f'openai/{model}'
    return model


def _preferred_token_field(model_name: str) -> str:
    if model_name.startswith('gpt-5'):
        return 'max_completion_tokens'
    return 'max_tokens'


def _fallback_token_field(token_field: str) -> str:
    return 'max_completion_tokens' if token_field == 'max_tokens' else 'max_tokens'


def _is_unsupported_token_param_error(error: Exception) -> bool:
    message = str(error).lower()
    return 'unsupported parameter' in message and ('max_tokens' in message or 'max_completion_tokens' in message)


def build_openai_model_settings(model_name: str, temperature: float, max_output_tokens: int) -> OpenAIModelSettings:
    primary_field = _preferred_token_field(model_name)
    secondary_field = _fallback_token_field(primary_field)

    for field_name in (primary_field, secondary_field):
        try:
            return OpenAIModelSettings(temperature=temperature, **{field_name: max_output_tokens})
        except Exception:
            continue

    return OpenAIModelSettings(temperature=temperature)


def build_model_settings_dict(model_name: str, temperature: float, max_output_tokens: int) -> dict[str, Any]:
    primary_field = _preferred_token_field(model_name)
    return {
        'temperature': temperature,
        primary_field: max_output_tokens,
    }


def _chat_completion_with_token_fallback(
    client: OpenAI,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_output_tokens: int,
):
    primary_field = _preferred_token_field(model)
    secondary_field = _fallback_token_field(primary_field)
    last_error: Exception | None = None

    for field_name in (primary_field, secondary_field):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                **{field_name: max_output_tokens},
            )
            return response
        except Exception as e:
            last_error = e
            if _is_unsupported_token_param_error(e):
                continue
            raise

    if last_error is not None:
        raise last_error

    return client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
    )


def create_main_model(config: Config):
    """Create the main LLM model from configuration."""
    logger.info(f"[create_main_model] model={config.main_model}  base_url={config.llm_base_url}")

    aliased_model = _resolve_model_alias(config.main_model)
    effective_base_url, effective_api_key = _resolve_endpoint_for_model(
        aliased_model,
        config.llm_base_url,
        config.llm_api_key,
    )
    normalized_name = _normalize_model_name(aliased_model, effective_base_url)
    if normalized_name != config.main_model:
        logger.info(f"[create_main_model] Normalized model name: {config.main_model} -> {normalized_name}")

    try:
        http_client = httpx.AsyncClient(event_hooks={'request': [_log_first_request]})
        openai_async_client = openai_lib.AsyncOpenAI(
            base_url=effective_base_url,
            api_key=effective_api_key,
            http_client=http_client,
        )
        model = OpenAIModel(
            model_name=normalized_name,
            provider=OpenAIProvider(openai_client=openai_async_client),
            settings=build_openai_model_settings(
                model_name=normalized_name,
                temperature=0.0,
                max_output_tokens=16000,
            )
        )
        logger.info(f"[create_main_model] OpenAIModel ready — will log first request when agent runs")
        return model
    except Exception as e:
        logger.error(f"[create_main_model] Failed to create OpenAIModel: {e}")
        raise



def create_openai_client(config: Config, base_url: str | None = None, api_key: str | None = None) -> OpenAI:
    """Create OpenAI client from configuration."""
    return OpenAI(
        base_url=base_url or config.llm_base_url,
        api_key=api_key or config.llm_api_key
    )


def call_llm(
    prompt: str,
    config: Config,
    model: str = None,
    temperature: float = 0.0
) -> str:
    """
    Call LLM with the given prompt.
    Supports OpenAI-compatible and Fireworks-compatible chat completions.
    
    Args:
        prompt: The prompt to send
        config: Configuration containing LLM settings
        model: Model name (defaults to config.main_model)
        temperature: Temperature setting
        
    Returns:
        LLM response text
    """
    if model is None:
        model = config.main_model

    aliased_model = _resolve_model_alias(model)
    effective_base_url, effective_api_key = _resolve_endpoint_for_model(
        aliased_model,
        config.llm_base_url,
        config.llm_api_key,
    )
    normalized_model = _normalize_model_name(aliased_model, effective_base_url)

    global _first_request_logged
    if not _first_request_logged:
        _first_request_logged = True
        preview = {
            'url': effective_base_url,
            'model': normalized_model,
            'temperature': temperature,
        }
        logger.info(f"[FIRST API REQUEST] {json.dumps(preview, indent=2)}")

    if is_openai_model(aliased_model):
        client = create_openai_client(config, base_url=effective_base_url, api_key=effective_api_key)
        response = _chat_completion_with_token_fallback(
            client=client,
            model=normalized_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_output_tokens=32768,
        )
        returned_model = getattr(response, "model", None) or normalized_model
        logger.info(f"[llm] response model: {returned_model}")
        return response.choices[0].message.content

    client = create_openai_client(config, base_url=effective_base_url, api_key=effective_api_key)
    response = _chat_completion_with_token_fallback(
        client=client,
        model=normalized_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_output_tokens=32768,
    )
    returned_model = getattr(response, "model", None) or normalized_model
    print(f"[llm] response model: {returned_model}")
    return response.choices[0].message.content
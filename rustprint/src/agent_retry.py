import asyncio
import logging


logger = logging.getLogger(__name__)


def _is_null_content_error(error: Exception) -> bool:
    message = str(error).lower()
    return "invalid value for 'content'" in message and "expected a string, got null" in message


def _is_provider_unavailable_error(error: Exception) -> bool:
    message = str(error)
    return "2005" in message and "failed to get response from provider" in message.lower()


async def run_agent_with_retry(agent, *args, max_retries: int = 5, **kwargs):
    attempt = 0
    while True:
        try:
            return await agent.run(*args, **kwargs)
        except Exception as error:
            if _is_null_content_error(error):
                attempt += 1
                logger.warning(f"[agent_retry] null content error, retrying attempt {attempt}: {error}")
                await asyncio.sleep(1)
            elif _is_provider_unavailable_error(error):
                attempt += 1
                if attempt > max_retries:
                    raise
                wait = min(2 ** attempt, 60)
                logger.warning(f"[agent_retry] provider unavailable (2005), retrying attempt {attempt}/{max_retries} in {wait}s: {error}")
                await asyncio.sleep(wait)
            else:
                raise

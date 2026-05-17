import base64
import os
import time
from typing import Callable, List, Optional


def _explicit_provider() -> Optional[str]:
    p = os.getenv("LLM_PROVIDER", "").strip().lower()
    if p in ("anthropic", "openai"):
        return p
    return None


def get_provider() -> str:
    explicit = _explicit_provider()
    if explicit:
        return explicit
    has_a = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    has_o = bool(os.getenv("OPENAI_API_KEY", "").strip())
    if has_o and not has_a:
        return "openai"
    return "anthropic"


def get_model(provider: str) -> str:
    custom = os.getenv("LLM_MODEL", "").strip()
    if custom:
        return custom
    if provider == "openai":
        return "gpt-4o-mini"
    return "claude-sonnet-4-20250514"


def get_vision_model(provider: str) -> str:
    custom = os.getenv("LLM_VISION_MODEL", "").strip()
    if custom:
        return custom
    if provider == "openai":
        return "gpt-4o-mini"
    return "claude-sonnet-4-20250514"


def _is_rate_limit_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if "ratelimit" in name:
        return True
    msg = str(exc).lower()
    return "rate limit" in msg or "429" in msg


def _with_rate_limit_retry(fn: Callable, *, attempts: int = 3, base_sleep: float = 30.0):
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            if not _is_rate_limit_error(e) or i == attempts - 1:
                raise
            sleep_for = base_sleep * (i + 1)
            print(f"  [llm] rate limited, sleeping {sleep_for:.0f}s and retrying...")
            time.sleep(sleep_for)
            last_exc = e
    raise last_exc  # pragma: no cover


def complete(system_prompt: Optional[str], user_prompt: str, max_tokens: int) -> str:
    provider = get_provider()
    model = get_model(provider)

    if provider == "anthropic":
        import anthropic

        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "LLM_PROVIDER is anthropic (or default) but ANTHROPIC_API_KEY is not set."
            )
        client = anthropic.Anthropic(api_key=key)
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        response = _with_rate_limit_retry(lambda: client.messages.create(**kwargs))
        return response.content[0].text.strip()

    import openai

    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "LLM_PROVIDER is openai but OPENAI_API_KEY is not set."
        )
    client = openai.OpenAI(api_key=key)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})
    response = _with_rate_limit_retry(
        lambda: client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
        )
    )
    text = response.choices[0].message.content
    return (text or "").strip()


def vision_extract(
    image_bytes_list: List[bytes],
    instruction: str,
    media_type: str = "image/png",
    max_tokens: int = 1500,
) -> str:
    """Send a list of page images to the configured vision model and return text.

    Works with either OpenAI (gpt-4o family) or Anthropic (claude-3+ family),
    selected by the same LLM_PROVIDER / API key auto-detection used by complete().
    """
    if not image_bytes_list:
        return ""
    provider = get_provider()
    model = get_vision_model(provider)

    if provider == "openai":
        import openai

        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        client = openai.OpenAI(api_key=key)
        content = [{"type": "text", "text": instruction}]
        for img in image_bytes_list:
            b64 = base64.b64encode(img).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{b64}"},
            })
        response = _with_rate_limit_retry(
            lambda: client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                max_tokens=max_tokens,
            )
        )
        return (response.choices[0].message.content or "").strip()

    import anthropic

    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    client = anthropic.Anthropic(api_key=key)
    content = []
    for img in image_bytes_list:
        b64 = base64.b64encode(img).decode("ascii")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    content.append({"type": "text", "text": instruction})
    response = _with_rate_limit_retry(
        lambda: client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
        )
    )
    return response.content[0].text.strip()



from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError

from errors import (
    APIRequestError,
    AuthError,
    ConfigError,
    ConnectionFailure,
    InputError,
    ModelOutputError,
    RateLimitError,
    RequestTimeoutError,
    ResumeMatcherError,
    ServerError,
)
from models import GEMINI_RESPONSE_SCHEMA, MatchResult, validate_payload
from prompts import (
    build_correction_content,
    build_user_content,
    count_injection_markers,
    load_system_prompt,
)

try:  
    from dotenv import load_dotenv
except ImportError: 
    load_dotenv = None

log = logging.getLogger("resume_matcher")


DEFAULT_MODEL = "gemini-3.5-flash"
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0


_sleep = time.sleep
_uniform = random.uniform
_monotonic = time.monotonic


@dataclass(frozen=True)
class Config:
    """Runtime settings. The API key is excluded from repr to avoid log leaks."""

    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    request_timeout_s: float = 120.0
    max_retries: int = 4
    deadline_s: float = 300.0
    max_input_chars: int = 20_000

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
    
        env = os.environ if environ is None else environ
        api_key = (env.get("GEMINI_API_KEY") or "").strip()
        if not api_key:
            raise ConfigError(
                "GEMINI_API_KEY is not set. Export it or put it in a local .env file."
            )

        def number(name: str, default: float, cast: type) -> Any:
            raw = env.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                value = cast(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
            if value <= 0 and name != "GEMINI_MAX_RETRIES":
                raise ConfigError(f"{name} must be positive")
            if value < 0:
                raise ConfigError(f"{name} must not be negative")
            return value

        return cls(
            api_key=api_key,
            model=(env.get("GEMINI_MODEL") or DEFAULT_MODEL).strip(),
            request_timeout_s=number("GEMINI_REQUEST_TIMEOUT_S", 30.0, float),
            max_retries=number("GEMINI_MAX_RETRIES", 4, int),
            deadline_s=number("GEMINI_DEADLINE_S", 120.0, float),
            max_input_chars=number("MAX_INPUT_CHARS", 20_000, int),
        )


# inputs
def validate_inputs(resume_text: Any, job_description: Any, max_chars: int) -> tuple[str, str]:
    
    for label, value in (("resume", resume_text), ("job description", job_description)):
        if not isinstance(value, str):
            raise InputError(f"The {label} must be a string, got {type(value).__name__}.")
        if not value.strip():
            raise InputError(f"The {label} is empty or whitespace-only.")
        if len(value) > max_chars:
            raise InputError(
                f"The {label} has {len(value)} characters; the limit is {max_chars} "
                "(configure with MAX_INPUT_CHARS)."
            )
    return resume_text.strip(), job_description.strip()


#  error mapping
def _retry_after_seconds(exc: BaseException) -> float | None:

    try:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if headers is not None:
            raw = headers.get("retry-after") or headers.get("Retry-After")
            if raw is not None:
                return max(0.0, float(raw))  # HTTP-date form is ignored
    except (TypeError, ValueError, AttributeError):
        pass
    try:
        details = (getattr(exc, "details", None) or {}).get("error", {}).get("details", [])
        for item in details:
            delay = item.get("retryDelay") if isinstance(item, dict) else None
            if isinstance(delay, str) and delay.endswith("s"):
                return max(0.0, float(delay[:-1]))
    except (TypeError, ValueError, AttributeError):
        pass
    return None


def classify_exception(exc: BaseException) -> ResumeMatcherError:
    
    if isinstance(exc, ResumeMatcherError):
        return exc
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        status = str(getattr(exc, "status", "") or "")
        message = str(getattr(exc, "message", "") or "")[:200]
        text = f"{status} {message}".lower()
        if code in (401, 403) or "api key" in text or "api_key_invalid" in text:
            return AuthError(f"Authentication failed (HTTP {code}). Check GEMINI_API_KEY.")
        if code == 429:
            return RateLimitError(
                f"Rate limited or quota exhausted (HTTP 429): {message}",
                retry_after=_retry_after_seconds(exc),
            )
        if code == 408:
            return RequestTimeoutError("Server reported a request timeout (HTTP 408).")
        if code == 404:
            return ConfigError(
                f"Model not found (HTTP 404). Check GEMINI_MODEL; run --list-models. {message}"
            )
        if isinstance(code, int) and code >= 500:
            return ServerError(
                f"Gemini server error (HTTP {code}).", retry_after=_retry_after_seconds(exc)
            )
        return APIRequestError(f"API rejected the request (HTTP {code}): {message}")
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return RequestTimeoutError("The request to the Gemini API timed out.")
    if isinstance(exc, (httpx.TransportError, ConnectionError)):
        return ConnectionFailure("Could not connect to the Gemini API.")
    return ResumeMatcherError(f"Unexpected error: {type(exc).__name__}")


#  model IO
def build_client(config: Config) -> genai.Client:
    
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true", "yes")
    return genai.Client(api_key=config.api_key, vertexai=use_vertex)


def _generate_once(
    client: Any, config: Config, system_prompt: str, content: str, timeout_s: float
) -> str:
    """One API call; returns raw response text or raises a mapped error."""
    gen_config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=GEMINI_RESPONSE_SCHEMA,
        http_options=types.HttpOptions(timeout=max(1, int(timeout_s * 1000))),
    )
    response = client.models.generate_content(
        model=config.model, contents=content, config=gen_config
    )
    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text.strip():
        raise ModelOutputError("The model returned no text (empty or blocked response).")
    return text


def call_with_retries(
    client: Any, config: Config, system_prompt: str, content: str, deadline: float
) -> str:
    """Call the model, retrying transient failures with bounded, jittered backoff."""
    attempt = 0
    while True:
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise RequestTimeoutError("Overall deadline exceeded before a successful response.")
        try:
            return _generate_once(
                client, config, system_prompt, content, min(config.request_timeout_s, remaining)
            )
        except Exception as exc:  # noqa: BLE001 - mapped to typed errors below
            error = classify_exception(exc)
            if not error.retryable or attempt >= config.max_retries:
                raise error from exc
            backoff = _uniform(0.0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt))
            delay = max(backoff, error.retry_after or 0.0)
            if delay >= deadline - _monotonic():
                raise error from exc
            log.warning(
                "Transient %s; retry %d/%d in %.1fs",
                error.error_type, attempt + 1, config.max_retries, delay,
            )
            _sleep(delay)
            attempt += 1


# parse 
def _strip_single_fence(text: str) -> str:
    """Safe recovery: remove ONE wrapping Markdown fence, nothing else."""
    lines = text.strip().splitlines()
    if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return text.strip()


def parse_and_validate(raw: str) -> MatchResult:
    """Parse JSON then validate. Raises ModelOutputError with value-free details."""
    try:
        payload = json.loads(_strip_single_fence(raw))
    except json.JSONDecodeError as exc:
        raise ModelOutputError(f"Model output is not valid JSON ({exc.msg} at char {exc.pos}).") from exc
    try:
        return validate_payload(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['type']}"
            for err in exc.errors()
        )
        raise ModelOutputError(f"Model output failed schema validation: {problems}") from exc


#  pipeline
def evaluate(
    resume_text: str,
    job_description: str,
    *,
    config: Config | None = None,
    client: Any = None,
) -> MatchResult:
    """Evaluate a resume against a job description. Raises ResumeMatcherError on failure."""
    config = config or Config.from_env()
    resume, jd = validate_inputs(resume_text, job_description, config.max_input_chars)

    flagged = count_injection_markers(resume, jd)
    if flagged:
        log.warning("Input contains %d instruction-like phrase(s); treated as data.", flagged)

    client = client or build_client(config)
    system_prompt = load_system_prompt()
    content = build_user_content(resume, jd)
    deadline = _monotonic() + config.deadline_s

    raw = call_with_retries(client, config, system_prompt, content, deadline)
    try:
        return parse_and_validate(raw)
    except ModelOutputError as first:
        log.warning("Output rejected (%s); one correction attempt.", first.message)
        corrected_content = build_correction_content(content, raw, first.message)
    raw2 = call_with_retries(client, config, system_prompt, corrected_content, deadline)
    try:
        return parse_and_validate(raw2)
    except ModelOutputError as second:
        raise ModelOutputError(
            f"Output invalid after one correction attempt: {second.message}"
        ) from second



def _read_text(path: str, label: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"Cannot read {label} file {path!r}: {type(exc).__name__}.") from exc


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Match a resume to a job description with Gemini.")
    parser.add_argument("--resume", help="Path to resume text file (UTF-8).")
    parser.add_argument("--jd", help="Path to job description text file (UTF-8).")
    parser.add_argument("--model", help="Override GEMINI_MODEL.")
    parser.add_argument("--list-models", action="store_true", help="List models for your key and exit.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Diagnostics on stderr.")
    return parser.parse_args(argv)


def _list_models(config: Config) -> None:
    client = build_client(config)
    for model in client.models.list():
        print(getattr(model, "name", model), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    if load_dotenv is not None:
        load_dotenv()
    try:
        config = Config.from_env()
        if args.model:
            config = Config(**{**config.__dict__, "model": args.model})
        if args.list_models:
            _list_models(config)
            return 0
        if not args.resume or not args.jd:
            raise InputError("Both --resume and --jd are required.")
        result = evaluate(
            _read_text(args.resume, "resume"), _read_text(args.jd, "job description"),
            config=config,
        )
    except ResumeMatcherError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - last-resort contract guard
        err = classify_exception(exc)
        print(json.dumps(err.to_dict(), ensure_ascii=False), file=sys.stderr)
        return err.exit_code
    print(json.dumps(result.model_dump(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

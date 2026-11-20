"""Single LLM gateway: bounded local replay (default) and an opt-in live provider path.

Replay stays the default (``LLM_FAKE=true``). The live path runs only when
``LLM_FAKE=false``, the caller explicitly asks for ``mode="live"`` and the key and
model ID are configured; otherwise it fails closed with ``live_processing_unavailable``.
All external calls happen in this module. Logs never contain report text or keys.
"""

import asyncio
import base64
import contextvars
import hashlib
import io
import json
import logging
import re
import time
import unicodedata
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_UP, Decimal, InvalidOperation
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)
from pydantic_settings import SettingsConfigDict

from app.catalog import CATEGORIES, CATEGORY_MAP
from app.config import ROOT, ExplicitEnvironmentSettings
from app.services import prompts

logger = logging.getLogger("baroai.llm")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
PROMPT_VERSION = "ticket-replay-v2"
SCHEMA_VERSION = "ticket-draft-v1"
FIXTURE_VERSION = "synthetic-ticket-v1"
MAX_INPUT_CHARS = 4000
MAX_INPUT_BYTES = 16000
MAX_OUTPUT_BYTES = 8192
TIMEOUT_SECONDS = 1.0


class GatewayPolicy(ExplicitEnvironmentSettings):
    # Separate from HTTP so direct worker/service calls also fail closed.
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", hide_input_in_errors=True
    )
    llm_fake: bool = True
    booth_history_replay: bool = False
    # Live provider settings. Model IDs come only from the environment.
    llm_protocol: Literal["v1", "v2"] = "v1"
    llm_max_output_tokens: int = Field(default=200, ge=1, le=1500)
    llm_max_images: int = Field(default=1, ge=1, le=3)
    llm_image_max_side: int = Field(default=512, ge=64, le=4096)
    llm_primary: str = "openai"
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model_text_small: str = ""
    openai_model_vision: str = ""
    llm_chat_reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh"
    ] | None = None
    llm_chat_explicit_cache: bool = False
    llm_timeout_s: float = Field(default=20.0, gt=0, le=120)
    llm_daily_call_cap: int = Field(default=300, ge=0, le=100_000)

    daily_llm_budget_usd: str = ""
    llm_price_table: dict = Field(default_factory=dict)
    llm_price_as_of: str = ""
    llm_price_source: str = ""

    def live_model(self, *, vision: bool) -> str:
        return (self.openai_model_vision if vision else self.openai_model_text_small).strip()

    def live_ready(self) -> bool:
        return (
            not self.llm_fake
            and self.llm_primary == "openai"
            and bool(self.openai_api_key.get_secret_value().strip())
            and bool(self.live_model(vision=False))
            and valid_base_url(self.openai_base_url)
            and pricing_ready(self)
        )


def decimal_value(value):
    try:
        if isinstance(value, bool):
            return None
        number = Decimal(str(value))
        return (
            number
            if number.is_finite()
            and 0 <= number <= Decimal("1000000000")
            and number.as_tuple().exponent >= -12
            else None
        )
    except (InvalidOperation, ValueError, TypeError):
        return None


def rates_for(policy, model):
    rates = policy.llm_price_table.get(model)
    if not isinstance(rates, dict):
        return None
    values = {key: decimal_value(rates.get(key)) for key in ("in", "cached_in", "out")}
    return (
        values
        if all(value is not None for value in values.values())
        and values["cached_in"] <= values["in"]
        else None
    )


def pricing_ready(policy):
    try:
        date.fromisoformat(policy.llm_price_as_of)
    except (ValueError, TypeError):
        return False
    source = urlsplit(policy.llm_price_source)
    return (
        decimal_value(policy.daily_llm_budget_usd) is not None
        and source.scheme == "https"
        and source.hostname in {"openai.com", "platform.openai.com", "developers.openai.com"}
        and source.username is None
        and source.password is None
        and bool(source.path)
        and rates_for(policy, policy.live_model(vision=False)) is not None
        and (
            not policy.live_model(vision=True)
            or rates_for(policy, policy.live_model(vision=True)) is not None
        )
    )


def measured_cost(metadata, rates):
    if metadata.get("usage_valid") is False:
        return None
    values = [metadata.get(k) for k in ("input_tokens", "cached_tokens", "output_tokens")]
    if not all(type(v) is int and 0 <= v <= 1_000_000_000 for v in values):
        return None
    inp, cached, out = values
    reasoning = metadata.get("reasoning_tokens")
    if reasoning is not None and (type(reasoning) is not int or not 0 <= reasoning <= out):
        return None
    usage = metadata.get("usage")
    if isinstance(usage, dict) and "total_tokens" in usage:
        total = usage["total_tokens"]
        if type(total) is not int or total != inp + out:
            return None
    if (
        cached > inp
        or not rates
        or any(decimal_value(rates.get(k)) is None for k in ("in", "cached_in", "out"))
    ):
        return None
    value = (
        (inp - cached) * Decimal(str(rates["in"]))
        + cached * Decimal(str(rates["cached_in"]))
        + out * Decimal(str(rates["out"]))
    ) / 1000000
    return str(value.quantize(Decimal("0.000001"), rounding=ROUND_UP))


def valid_base_url(base: str) -> bool:
    return (base.strip() or "https://api.openai.com/v1").rstrip("/") == "https://api.openai.com/v1"


def runtime_mode(settings) -> Literal["replay", "live", "unavailable"]:
    if settings.llm_fake:
        return "replay"
    secret = getattr(settings, "jwt_secret", None)
    key = secret.get_secret_value() if secret is not None else ""
    ready = len(key.encode("utf-8")) >= 32 and policy_from_settings(settings).live_ready()
    return "live" if ready else "unavailable"


def policy_from_settings(settings) -> GatewayPolicy:
    """Build an explicit policy from application settings (no second .env read wins)."""
    key = getattr(settings, "openai_api_key", None)
    return GatewayPolicy(
        llm_fake=settings.llm_fake,
        booth_history_replay=False,
        llm_protocol=getattr(settings, "llm_protocol", "v1"),
        llm_max_output_tokens=getattr(settings, "llm_max_output_tokens", 200),
        llm_max_images=getattr(settings, "llm_max_images", 1),
        llm_image_max_side=getattr(settings, "llm_image_max_side", 512),
        llm_primary=getattr(settings, "llm_primary", "openai"),
        openai_api_key=key if key is not None else SecretStr(""),
        openai_base_url=getattr(settings, "openai_base_url", "https://api.openai.com/v1"),
        openai_model_text_small=getattr(settings, "openai_model_text_small", ""),
        openai_model_vision=getattr(settings, "openai_model_vision", ""),
        llm_chat_reasoning_effort=getattr(settings, "llm_chat_reasoning_effort", None),
        llm_chat_explicit_cache=getattr(settings, "llm_chat_explicit_cache", False),
        llm_timeout_s=getattr(settings, "llm_timeout_s", 20.0),
        llm_daily_call_cap=getattr(settings, "llm_daily_call_cap", 300),
        daily_llm_budget_usd=getattr(settings, "daily_llm_budget_usd", ""),
        llm_price_table=getattr(settings, "llm_price_table", {}),
        llm_price_as_of=getattr(settings, "llm_price_as_of", ""),
        llm_price_source=getattr(settings, "llm_price_source", ""),
    )


class StructuredDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    category_code: str = Field(min_length=1, max_length=30)
    # Replay can explicitly contain no model urgency judgment.
    ai_urgency: Annotated[StrictInt, Field(ge=0, le=3)] | None

    @field_validator("category_code")
    @classmethod
    def known_category(cls, value):
        if value not in CATEGORY_MAP:
            raise ValueError("unknown_category")
        return value


@dataclass(frozen=True)
class Metadata:
    mode: Literal["replay"] = "replay"
    prompt_version: str = PROMPT_VERSION
    schema_version: str = SCHEMA_VERSION
    fixture_version: str = FIXTURE_VERSION
    fixture_id: str = "unknown"
    model: None = None
    usage: None = None
    attempts: int = 0
    duration_ms: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class LiveMetadata:
    """Attempt metadata for a live call. Never holds input text, image bytes or keys."""

    prompt_version: str
    schema_version: str
    mode: Literal["live"] = "live"
    provider: str = "openai"
    model: str | None = None
    usage: dict | None = None
    usage_valid: bool | None = None
    attempts: int = 0
    duration_ms: float = 0.0
    error: str | None = None
    image_count: int = 0
    images_skipped: str | None = None
    category_rejected: bool = False
    kind: str | None = None
    protocol: str | None = "v1"
    requested_model: str | None = None
    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    image_bytes: int | None = None
    request_bytes: int | None = None
    max_output_tokens: int | None = None
    sent: bool | None = False
    discarded: str | None = None
    skipped: str | None = None
    provider_request_id: str | None = None
    cost_usd: str | None = None
    reserved_usd: None = None
    price_as_of: None = None
    cache_hit: bool | None = None
    cache_key: None = None
    late: None = None
    input_hash: None = None
    result: None = None
    service_tier: str | None = None


@dataclass(frozen=True)
class Draft:
    replay_match: Literal["matched_replay", "unmatched", "live"]
    category_code: str = "UNKNOWN"
    mode: str = "replay"
    prompt_version: str = PROMPT_VERSION
    ai_urgency: int | None = None
    metadata: Metadata | LiveMetadata | None = None
    summary: str | None = None
    urgency_reason: str | None = None
    model: str | None = None
    followup_questions: tuple[dict, ...] = field(default_factory=tuple)
    hazard_signals: tuple[str, ...] = field(default_factory=tuple)
    rule_signal_assessment: tuple[dict, ...] = field(default_factory=tuple)


class GatewayError(Exception):
    """Publicly safe error and metadata; never retains the transport exception or input."""

    def __init__(self, metadata: Metadata | LiveMetadata):
        self.metadata = metadata
        super().__init__(metadata.error)


class ReplayRefusal(Exception):
    """Internal transport signal, never selected by public text."""


@dataclass(frozen=True)
class ReplayFixture:
    id: str
    output: bytes


Transport = Callable[[ReplayFixture], Awaitable[bytes]]


async def _replay_transport(fixture: ReplayFixture) -> bytes:
    return fixture.output


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


async def draft_ticket(
    text: str,
    *,
    mode: str = "replay",
    policy: GatewayPolicy | None = None,
    transport: Transport | None = None,
    categories: list[tuple[str, str]] | None = None,
    images: list["PreprocessedImage"] | None = None,
    live_transport: "LiveTransport | None" = None,
    before_send=None,
    protocol: Literal["v1", "v2"] | None = None,
    require_image: bool = False,
    cache_lookup=None,
    history_replay_context: tuple[str, int] | None = None,
) -> Draft:
    """One bounded attempt. Failures raise; durable job retries belong to the caller.

    Transport injection is internal Python-only test support, not an HTTP option.
    In replay mode raw report text never reaches a transport: only a fixed local
    fixture does. ``mode="live"`` is the only path that sends text to a provider.
    """
    if mode == "live":
        return await _draft_ticket_live(
            text,
            policy=policy,
            transport=live_transport,
            categories=categories,
            images=images,
            before_send=before_send,
            protocol=protocol,
            require_image=require_image,
            cache_lookup=cache_lookup,
        )
    started, attempts = time.monotonic(), 0
    fixture_id = "unknown"
    error = None
    result = None
    try:
        if mode != "replay" or not (policy or GatewayPolicy()).llm_fake:
            error = "live_processing_unavailable"
        elif not isinstance(text, str) or not text or len(text) > MAX_INPUT_CHARS:
            error = "invalid_input"
        else:
            try:
                if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
                    error = "input_too_large"
            except UnicodeError:
                error = "invalid_input"
        if error is None:
            from app.booth_fixtures import ticket_replay_fixture

            fixture_id, category = ticket_replay_fixture(
                text, history=bool(policy and policy.booth_history_replay
                                   and history_replay_context),
                location=history_replay_context[0] if history_replay_context else '',
                photo_count=history_replay_context[1] if history_replay_context else 0,
            )
            exact = fixture_id != "unknown"
            fixture = ReplayFixture(
                fixture_id,
                json.dumps(
                    {"category_code": category, "ai_urgency": None}
                ).encode(),
            )
            attempts = 1
            try:
                async with asyncio.timeout(TIMEOUT_SECONDS):
                    raw = await (transport or _replay_transport)(fixture)
                if not isinstance(raw, bytes):
                    error = "invalid_output"
                elif len(raw) > MAX_OUTPUT_BYTES:
                    error = "output_too_large"
                else:
                    try:
                        parsed = json.loads(raw, object_pairs_hook=_unique_object)
                    except (ValueError, UnicodeError, RecursionError):
                        error = "invalid_json"
                    else:
                        try:
                            result = StructuredDraft.model_validate(parsed)
                        except ValidationError:
                            error = "invalid_schema"
            except TimeoutError:
                error = "timeout"
            except ReplayRefusal:
                error = "refusal"
            except Exception:
                error = "transport_failed"
    except Exception:
        # Invalid local configuration also fails closed, without echoing env values.
        error = "configuration_error"
    metadata = Metadata(
        fixture_id=fixture_id,
        fixture_version=("synthetic-booth-history-v1" if fixture_id == "booth-history-v1"
                         else FIXTURE_VERSION),
        attempts=attempts,
        duration_ms=round((time.monotonic() - started) * 1000, 3),
        error=error,
    )
    logger.info(
        json.dumps(
            {"event": "llm_replay_failed" if error else "llm_replay", **asdict(metadata)},
            separators=(",", ":"),
        )
    )
    if error:
        raise GatewayError(metadata) from None
    assert result is not None
    return Draft(
        replay_match="matched_replay" if exact else "unmatched",
        category_code=result.category_code,
        ai_urgency=result.ai_urgency,
        metadata=metadata,
        followup_questions=({"kind": "since_when", "question": "언제부터 물이 떨어지기 시작했나요?",
                            "options": []},)
        if text == "화장실 세면대 아래에서 물이 떨어집니다." else (),
    )


REPORT_PROMPT_VERSION = "report-replay-v1"
REPORT_SCHEMA_VERSION = "report-structure-v1"
REPORT_FIXTURE_VERSION = "synthetic-report-v1"


@dataclass(frozen=True)
class ReportDraft:
    structure: object
    mode: str = "replay"
    metadata: Metadata | None = None


async def draft_report(
    text,
    result_type,
    *,
    mode="replay",
    policy=None,
    transport=None,
    live_transport=None,
    before_send=None,
    protocol: Literal["v1", "v2"] | None = None,
    cache_lookup=None,
):
    """Bounded text-only replay. Arbitrary narrative stays unverified and empty.

    ``mode="live"`` structures the worker's text through the provider instead.
    """
    from app.schemas import ReportStructure

    if mode == "live":
        return await _draft_report_live(
            text, result_type, policy=policy, transport=live_transport, before_send=before_send,
            protocol=protocol, cache_lookup=cache_lookup,
        )

    started, attempts, error, structure = time.monotonic(), 0, None, None
    try:
        if not (policy or GatewayPolicy()).llm_fake:
            error = "live_processing_unavailable"
        elif not isinstance(text, str) or not text or len(text) > MAX_INPUT_CHARS:
            error = "invalid_input"
        elif len(text.encode()) > MAX_INPUT_BYTES or result_type not in {
            "completed",
            "temporary",
            "revisit",
            "needs_review",
        }:
            error = "invalid_input"
        else:
            fixture = ReplayFixture(
                "report-unverified",
                json.dumps(
                    {
                        "actions": [],
                        "materials_used": [],
                        "residual_issue": "",
                        "revisit_needed": result_type == "revisit",
                        "photo_comparison": "not_performed",
                        "source": "replay",
                    }
                ).encode(),
            )
            attempts = 1
            async with asyncio.timeout(TIMEOUT_SECONDS):
                raw = await (transport or _replay_transport)(fixture)
            if not isinstance(raw, bytes):
                error = "invalid_output"
            elif len(raw) > MAX_OUTPUT_BYTES:
                error = "output_too_large"
            else:
                try:
                    parsed = json.loads(raw, object_pairs_hook=_unique_object)
                except (ValueError, UnicodeError, RecursionError):
                    error = "invalid_json"
                else:
                    try:
                        structure = ReportStructure.model_validate(parsed, strict=True)
                        if structure.source != "replay" or set(parsed) != set(
                            ReportStructure.model_fields
                        ):
                            error = "invalid_schema"
                    except ValidationError:
                        error = "invalid_schema"
    except TimeoutError:
        error = "timeout"
    except ReplayRefusal:
        error = "refusal"
    except Exception:
        error = "gateway_failed"
    metadata = Metadata(
        prompt_version=REPORT_PROMPT_VERSION,
        schema_version=REPORT_SCHEMA_VERSION,
        fixture_version=REPORT_FIXTURE_VERSION,
        fixture_id="report-unverified",
        attempts=attempts,
        duration_ms=round((time.monotonic() - started) * 1000, 3),
        error=error,
    )
    logger.info(
        json.dumps(
            {"event": "report_replay_failed" if error else "report_replay", **asdict(metadata)}
        )
    )
    if error:
        raise GatewayError(metadata) from None
    return ReportDraft(structure=structure, metadata=metadata)


# --------------------------------------------------------------------------------------
# Live provider path (OpenAI-compatible Chat Completions with strict JSON Schema output)
# --------------------------------------------------------------------------------------

LIVE_TICKET_SCHEMA_VERSION = "ticket-followup-schema-v4"
LIVE_REPORT_SCHEMA_VERSION = "report-structure-live-v1"
LIVE_TICKET_V2_SCHEMA_VERSION = "ticket-compact-followup-schema-v4"
LIVE_REPORT_V2_SCHEMA_VERSION = "report-structure-live-v2"
LIVE_MAX_COMPLETION_TOKENS = 1500
MAX_RESPONSE_BYTES = 256_000
MAX_IMAGES = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SUMMARY_MAX = 120
REASON_MAX = 200
_KST_OFFSET = timedelta(hours=9)
# Visit/processing promises are never stored from model output.
PROMISE_PATTERN = re.compile(
    r"방문\s*(?:하겠|할\s*예정|예정|드리겠|하도록)"
    r"|(?:처리|수리|조치|교체)\s*(?:하겠|해\s*드리겠|해드리겠|할\s*예정|완료\s*예정|될\s*예정)"
    r"|(?:오늘|내일|모레|금일|명일|\d+\s*(?:시|일))\s*(?:까지|중|중에|에)\s*(?:방문|처리|수리|조치)"
    r"|곧\s*(?:해결|처리|수리|방문)"
)

LiveTransport = Callable[[dict], Awaitable[dict]]


class ProviderStatusError(Exception):
    """HTTP status only; the response body is never retained or logged."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(str(status))


class _LiveFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PreprocessedImage:
    """Bytes of an approved, ready privacy-processed variant only.

    Built by the job layer after the stored hash matched. Original uploads or simple
    resizes must never be wrapped in this type.
    """

    data: bytes = field(repr=False)
    sha256: str
    pipeline_version: str


_daily_calls = {"day": None, "count": 0}


def reset_daily_counter() -> None:
    _daily_calls.update(day=None, count=0)


def _reserve_call(cap: int) -> bool:
    """Process-local daily cap (KST day). Each process keeps its own counter."""
    day = (datetime.now(UTC) + _KST_OFFSET).date().isoformat()
    if _daily_calls["day"] != day:
        _daily_calls.update(day=day, count=0)
    if _daily_calls["count"] >= cap:
        return False
    _daily_calls["count"] += 1
    return True


def chat_completions_url(policy: GatewayPolicy) -> str:
    if not valid_base_url(policy.openai_base_url):
        raise ValueError("invalid_base_url")
    return "https://api.openai.com/v1/chat/completions"


_observation = contextvars.ContextVar("llm_observation", default=None)


@dataclass
class ReservationGrant:
    job_id: str
    lease_token: str
    attempt_no: int
    request_digest: bytes | None = None
    consumed: bool = False


def _body_bytes(body):
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def http_transport(policy: GatewayPolicy) -> LiveTransport:
    """Default network transport. The key stays in this closure, never in the body."""
    url = chat_completions_url(policy)
    key = policy.openai_api_key.get_secret_value().strip()
    timeout = policy.llm_timeout_s

    async def send(body: dict) -> dict:
        import httpx

        observation = _observation.get()
        grant = observation.pop("budget_grant", None) if observation else None
        if (
            not isinstance(grant, ReservationGrant)
            or grant.consumed
            or (grant.request_digest != hashlib.sha256(_body_bytes(body)).digest())
        ):
            raise _LiveFailure("budget_authorization_required")
        grant.consumed = True
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                url,
                content=_body_bytes(body),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
        request_id = response.headers.get("x-request-id", "")
        observation = _observation.get()
        if observation is not None and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            observation["provider_request_id"] = request_id
        if response.status_code != 200:
            raise ProviderStatusError(response.status_code)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise _LiveFailure("output_too_large")
        return response.json()

    return send


def _status_code(status: int) -> str:
    if status == 429:
        return "provider_rate_limited"
    if status in {401, 403}:
        return "provider_auth_failed"
    if status == 400:
        return "provider_rejected_request"
    return "provider_unavailable" if status >= 500 else "provider_http_error"


def _usage(response: dict) -> dict | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    kept = {
        name: usage[name]
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        if type(usage.get(name)) is int and usage[name] >= 0
    }
    return kept or None


def _usage_valid(response):
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return False
    if "total_tokens" in usage and (
        type(usage["total_tokens"]) is not int or usage["total_tokens"] < 0
    ):
        return False
    if "completion_tokens_details" in usage:
        details = usage["completion_tokens_details"]
        if not isinstance(details, dict):
            return False
        if "reasoning_tokens" in details and (
            type(details["reasoning_tokens"]) is not int or details["reasoning_tokens"] < 0
        ):
            return False
    return True


def _unexpected_cache_write(response):
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return False
    if "prompt_tokens_details" not in usage:
        return False
    details = usage["prompt_tokens_details"]
    if not isinstance(details, dict):
        return True
    if "cache_write_tokens" not in details:
        return False
    count = details["cache_write_tokens"]
    return type(count) is not int or count != 0


def _text_request_options(body, policy):
    if policy.llm_chat_reasoning_effort is not None:
        body["reasoning_effort"] = policy.llm_chat_reasoning_effort
    if policy.llm_chat_explicit_cache:
        body["prompt_cache_options"] = {"mode": "explicit"}


def _token_fields(response):
    usage = response.get("usage") if isinstance(response, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    prompt = usage.get("prompt_tokens_details")
    completion = usage.get("completion_tokens_details")

    def number(value):
        return value if type(value) is int and value >= 0 else None

    return dict(
        input_tokens=number(usage.get("prompt_tokens")),
        output_tokens=number(usage.get("completion_tokens")),
        cached_tokens=number(prompt.get("cached_tokens")) if isinstance(prompt, dict) else None,
        reasoning_tokens=number(completion.get("reasoning_tokens"))
        if isinstance(completion, dict)
        else None,
    )


def _response_model(response: dict, requested: str) -> str:
    model = response.get("model")
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model):
        return model
    return None


def _parse_content(response) -> dict:
    if not isinstance(response, dict):
        raise _LiveFailure("invalid_output")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _LiveFailure("invalid_output")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise _LiveFailure("invalid_output")
    if message.get("refusal"):
        raise _LiveFailure("refusal")
    if choice.get("finish_reason") == "length":
        raise _LiveFailure("output_truncated")
    content = message.get("content")
    if not isinstance(content, str):
        raise _LiveFailure("invalid_output")
    try:
        if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise _LiveFailure("output_too_large")
        parsed = json.loads(content, object_pairs_hook=_unique_object)
    except _LiveFailure:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise _LiveFailure("invalid_json") from None
    if not isinstance(parsed, dict):
        raise _LiveFailure("invalid_schema")
    return parsed


def _one_line(value):
    if isinstance(value, str):
        return " ".join(value.split())
    return value


class RuleSignalAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    hit_id: StrictStr = Field(min_length=1, max_length=100)
    status: Literal["confirmed", "negated", "unrelated"]
    evidence: StrictStr = Field(min_length=1, max_length=40)


class LiveTicketOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    category_code: StrictStr = Field(min_length=1, max_length=80)
    ai_urgency: Annotated[StrictInt, Field(ge=0, le=3)]
    summary: StrictStr = Field(min_length=1, max_length=SUMMARY_MAX)
    urgency_reason: StrictStr = Field(min_length=1, max_length=REASON_MAX)
    hazard_signals: list[StrictStr] = Field(max_length=100)
    rule_signal_assessment: list[RuleSignalAssessment] = Field(max_length=100)

    @field_validator("summary", "urgency_reason", mode="before")
    @classmethod
    def single_line(cls, value, info):
        # Overlong text is shortened by the server rather than failing the draft.
        value = _one_line(value)
        limit = SUMMARY_MAX if info.field_name == "summary" else REASON_MAX
        if isinstance(value, str) and len(value) > limit:
            value = value[: limit - 1].rstrip() + "…"
        return value


class LiveReportOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    actions: list[Annotated[StrictStr, Field(min_length=1, max_length=120)]] = Field(max_length=5)
    materials_used: list[Annotated[StrictStr, Field(min_length=1, max_length=120)]] = Field(
        max_length=5
    )
    residual_issue: StrictStr = Field(max_length=500)
    revisit_needed: StrictBool


def followup_json_schema():
    return {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["kind", "question", "options"], "properties": {
                "kind": {"type": "string"}, "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}}}}}


def safety_json_properties():
    return {
        "hazard_signals": {"type": "array", "items": {"type": "string"}},
        "rule_signal_assessment": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["hit_id", "status", "evidence"], "properties": {
                "hit_id": {"type": "string"},
                "status": {"type": "string", "enum": ["confirmed", "negated", "unrelated"]},
                "evidence": {"type": "string"},
            },
        }},
    }


def ticket_json_schema(codes: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["category_code", "ai_urgency", "summary", "urgency_reason",
                     "followup_questions", "hazard_signals", "rule_signal_assessment"],
        "properties": {
            **safety_json_properties(),
            "category_code": {"type": "string", "enum": codes},
            "ai_urgency": {"type": "integer", "enum": [0, 1, 2, 3]},
            "summary": {"type": "string"},
            "urgency_reason": {"type": "string"},
            "followup_questions": followup_json_schema(),
        },
    }


REPORT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["actions", "materials_used", "residual_issue", "revisit_needed"],
    "properties": {
        "actions": {"type": "array", "items": {"type": "string"}},
        "materials_used": {"type": "array", "items": {"type": "string"}},
        "residual_issue": {"type": "string"},
        "revisit_needed": {"type": "boolean"},
    },
}



class CompactTicketOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    c: StrictInt
    u: Annotated[StrictInt, Field(ge=0, le=3)]
    s: StrictStr
    r: StrictStr
    hazard_signals: list[StrictStr] = Field(max_length=100)
    rule_signal_assessment: list[RuleSignalAssessment] = Field(max_length=100)


class CompactReportOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    a: list[StrictStr]
    m: list[StrictStr]
    x: StrictStr
    v: StrictBool


def compact_ticket_schema(count):
    return {"type": "object", "additionalProperties": False,
            "required": ["c", "u", "s", "r", "q", "hazard_signals",
                         "rule_signal_assessment"], "properties": {
                **safety_json_properties(),
                "c": {"type": "integer", "enum": list(range(count + 1))},
                "u": {"type": "integer", "enum": [0, 1, 2, 3]},
                "s": {"type": "string"}, "r": {"type": "string"}, "q": followup_json_schema()}}


COMPACT_REPORT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["a", "m", "x", "v"],
    "properties": {"a": {"type": "array", "items": {"type": "string"}},
                   "m": {"type": "array", "items": {"type": "string"}},
                   "x": {"type": "string"}, "v": {"type": "boolean"}},
}


def compact_categories(allowed):
    result = []
    for code, label in sorted(allowed):
        if code == "UNKNOWN":
            continue
        # Catalog text is untrusted; strip controls, separators and delimiter characters.
        name = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in label)
        name = prompts.neutralize(" ".join(name.split())[:60])
        result.append((code, name))
    return result


def live_versions(kind, protocol):
    if protocol == "v2":
        return ((prompts.REPORT_V2_PROMPT_VERSION, LIVE_REPORT_V2_SCHEMA_VERSION)
                if kind == "report_analysis" else
                (prompts.TICKET_V2_PROMPT_VERSION, LIVE_TICKET_V2_SCHEMA_VERSION))
    return ((prompts.REPORT_PROMPT_VERSION, LIVE_REPORT_SCHEMA_VERSION)
            if kind == "report_analysis" else
            (prompts.TICKET_PROMPT_VERSION, LIVE_TICKET_SCHEMA_VERSION))


def reject_promises(values):
    if any(PROMISE_PATTERN.search(value) for value in values if isinstance(value, str)):
        raise _LiveFailure("policy_violation")


def compact_line(value, limit):
    return _one_line(value)[:limit].strip()


def compact_items(values, limit):
    placeholders = {"", "-", "없음", "미상", "모름", "해당 없음"}
    return [value for item in values
            if (value := compact_line(item, limit)) not in placeholders][:3]

def _request_body(
    model, system, user_content, schema_name, schema, *, output_cap=LIVE_MAX_COMPLETION_TOKENS
) -> dict:
    return {
        "model": model,
        "service_tier": "default",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
        "max_completion_tokens": output_cap,
    }


def _checked_text(text) -> None:
    if not isinstance(text, str) or not text or len(text) > MAX_INPUT_CHARS:
        raise _LiveFailure("invalid_input")
    try:
        if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            raise _LiveFailure("input_too_large")
    except UnicodeError:
        raise _LiveFailure("invalid_input") from None


def _usable_images(images) -> tuple[list[PreprocessedImage], str | None]:
    usable, skipped = [], None
    for image in images or []:
        if (
            not isinstance(image, PreprocessedImage)
            or not isinstance(image.data, bytes)
            or len(image.data) > MAX_IMAGE_BYTES
            or not image.data.startswith(b"\xff\xd8")
            or hashlib.sha256(image.data).hexdigest() != image.sha256
        ):
            skipped = "image_rejected"
            continue
        if len(usable) >= MAX_IMAGES:
            skipped = skipped or "image_limit"
            continue
        usable.append(image)
    return usable, skipped



def _resize_approved_image(image: PreprocessedImage, max_side: int) -> bytes:
    """Decode a verified privacy variant; never return its bytes on failure."""
    from PIL import Image

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(image.data)) as source:
            if source.format != "JPEG":
                raise ValueError("invalid_image_format")
            source.load()
            converted = source.convert("RGB")
            converted.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            converted.save(output, format="JPEG", quality=80)
            return output.getvalue()


async def _prepare_images(images, policy):
    usable, skipped = _usable_images(images)
    if len(usable) > policy.llm_max_images:
        usable, skipped = usable[:policy.llm_max_images], skipped or "image_limit"
    prepared = []
    for image in usable:
        try:
            data = await asyncio.to_thread(_resize_approved_image, image, policy.llm_image_max_side)
        except Exception:
            skipped = skipped or "image_resize_failed"
            continue
        prepared.append((image, data, hashlib.sha256(data).hexdigest()))
    return prepared, skipped

def _allowed_categories(categories) -> list[tuple[str, str]]:
    source = (
        categories
        if categories is not None
        else [(item["code"], item["label"]) for item in CATEGORIES]
    )
    result, seen = [], set()
    for code, label in source:
        if (
            isinstance(code, str)
            and isinstance(label, str)
            and 0 < len(code) <= 80
            and code not in seen
        ):
            seen.add(code)
            result.append((code, label[:60]))
    if "UNKNOWN" not in seen:
        result.insert(0, ("UNKNOWN", "담당자 확인 필요"))
    return result


async def _send(
    policy, transport, body, observation, before_send, image_count,
    image_digests=None, transfer_digests=None,
):
    """One attempt, bounded by the configured timeout plus a small margin."""
    # Injected transports remain available for isolated tests.
    if transport is None and before_send is None:
        raise _LiveFailure("budget_authorization_required")
    if not _reserve_call(policy.llm_daily_call_cap):
        raise _LiveFailure("daily_cap_exceeded")
    counter_day = _daily_calls["day"]
    sender = transport or http_transport(policy)
    observation["request_bytes"] = len(_body_bytes(body))
    observation["max_output_tokens"] = body["max_completion_tokens"]
    if before_send is not None:
        try:
            args = (observation["request_bytes"], body["model"], image_count,
                    body["max_completion_tokens"], image_digests or [])
            denial = await before_send(*args, transfer_digests) if transfer_digests else (
                await before_send(*args)
            )
        except BaseException:
            if _daily_calls["day"] == counter_day:
                _daily_calls["count"] -= 1
            raise
        if not isinstance(denial, ReservationGrant) or denial.consumed or denial.request_digest:
            if _daily_calls["day"] == counter_day:
                _daily_calls["count"] -= 1
            raise _LiveFailure(
                denial if isinstance(denial, str) else "budget_authorization_required"
            )
        denial.request_digest = hashlib.sha256(_body_bytes(body)).digest()
        observation["budget_grant"] = denial
    token = _observation.set(observation)
    try:
        async with asyncio.timeout(policy.llm_timeout_s + 1):
            observation["sent"] = True
            observation["dispatch_started"] = time.monotonic()
            return await sender(body)
    except _LiveFailure:
        raise
    except ProviderStatusError as exc:
        raise _LiveFailure(_status_code(exc.status)) from None
    except TimeoutError:
        raise _LiveFailure("timeout") from None
    except Exception as exc:
        # httpx timeouts (ReadTimeout, ConnectTimeout, ...) without importing httpx here.
        if "Timeout" in type(exc).__name__:
            raise _LiveFailure("timeout") from None
        raise _LiveFailure("transport_failed") from None
    finally:
        if observation.get("dispatch_started") is not None:
            observation["duration_ms"] = round(
                (time.monotonic() - observation.pop("dispatch_started")) * 1000, 3
            )
        observation.pop("budget_grant", None)
        _observation.reset(token)


def _log(event, metadata):
    logger.info(json.dumps({"event": event, **asdict(metadata)}, separators=(",", ":")))


async def _draft_ticket_live(
    text, *, policy, transport, categories, images, before_send, protocol, require_image,
    cache_lookup,
) -> Draft:
    error = None
    model, usage, image_count, skipped, rejected = None, None, 0, None, False
    result = None
    prepared = []
    observation, token_fields, requested = {"sent": False}, {}, None
    policy = policy or GatewayPolicy()
    protocol = protocol or policy.llm_protocol
    prompt_version, schema_version = live_versions("ticket_analysis", protocol)
    try:
        if protocol not in {"v1", "v2"}:
            raise _LiveFailure("invalid_input")
        if not policy.live_ready():
            raise _LiveFailure("live_processing_unavailable")
        _checked_text(text)
        allowed = _allowed_categories(categories)
        usable, skipped = _usable_images(images)
        prepared = []
        if usable and not policy.live_model(vision=True):
            skipped = "vision_model_unconfigured"
        elif usable:
            prepared, resize_skipped = await _prepare_images(usable, policy)
            skipped = skipped or resize_skipped
        if require_image and not prepared:
            skipped = "short_input"
            raise _LiveFailure("skipped_short")
        vision = bool(prepared)
        model = policy.live_model(vision=vision)
        if not model:
            raise _LiveFailure("live_processing_unavailable")
        chat_completions_url(policy)
        image_count = len(prepared)
        numbered = compact_categories(allowed) if protocol == "v2" else []
        user_text = (prompts.ticket_v2_user(text) if protocol == "v2" else
                     prompts.ticket_user_message(text, allowed, image_count))
        content = [{"type": "text", "text": user_text}] + [
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(data).decode(),
                    "detail": "low",
                },
            }
            for _, data, _ in prepared
        ]
        body = _request_body(
            model,
            prompts.ticket_v2_system(numbered) if protocol == "v2" else prompts.TICKET_SYSTEM,
            content if vision else user_text,
            "ticket_draft",
            compact_ticket_schema(len(numbered)) if protocol == "v2" else
            ticket_json_schema([code for code, _ in allowed]),
            output_cap=(policy.llm_max_output_tokens if protocol == "v2"
                        else LIVE_MAX_COMPLETION_TOKENS),
        )
        if not vision:
            _text_request_options(body, policy)
        requested = (
            model
            if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model)
            else None
        )
        originals = [image.sha256 for image, _, _ in prepared]
        transferred = [digest for _, _, digest in prepared]
        if cache_lookup is not None:
            cached = await cache_lookup(body, originals, transferred)
            if isinstance(cached, Draft):
                return cached
            if cached is not None:
                raise _LiveFailure("lease_lost")
        response = await _send(
            policy, transport, body, observation, before_send, image_count,
            originals, transferred,
        )
        observation["response_received"] = True
        model = _response_model(response, model) if isinstance(response, dict) else None
        usage = _usage(response) if isinstance(response, dict) else None
        observation["usage_valid"] = _usage_valid(response)
        tier = response.get("service_tier") if isinstance(response, dict) else None
        observation["service_tier"] = (
            tier if isinstance(tier, str) and re.fullmatch(r"[a-z_]{1,30}", tier) else "unknown"
        )
        token_fields = _token_fields(response)
        if not vision and policy.llm_chat_explicit_cache and _unexpected_cache_write(response):
            observation["usage_valid"] = False
            raise _LiveFailure("unexpected_cache_write")
        parsed = _parse_content(response)
        from app.followup_questions import clean_questions

        questions = clean_questions(
            parsed.pop("q" if protocol == "v2" else "followup_questions", [])
        )
        try:
            if protocol == "v2":
                compact = CompactTicketOutput.model_validate(parsed)
                reject_promises([compact.s, compact.r])
                category = (numbered[compact.c - 1][0]
                            if 1 <= compact.c <= len(numbered) else "UNKNOWN")
                rejected = compact.c < 0 or compact.c > len(numbered)
                output = LiveTicketOutput.model_validate({"category_code": category,
                    "ai_urgency": compact.u, "summary": compact_line(compact.s, 60),
                    "urgency_reason": compact_line(compact.r, 40),
                    "hazard_signals": compact.hazard_signals,
                    "rule_signal_assessment": [x.model_dump()
                                               for x in compact.rule_signal_assessment]})
            else:
                reject_promises([parsed.get("summary"), parsed.get("urgency_reason")])
                output = LiveTicketOutput.model_validate(parsed)
                category = output.category_code
                if category not in {code for code, _ in allowed}:
                    category, rejected = "UNKNOWN", True
        except ValidationError:
            raise _LiveFailure("invalid_schema") from None
        result = (category, output)
    except _LiveFailure as exc:
        error = exc.code
    except Exception:
        error = "configuration_error"
    metadata = LiveMetadata(
        prompt_version=prompt_version,
        schema_version=schema_version,
        model=model if observation.get("response_received") else None,
        usage=usage,
        attempts=int(observation["sent"]),
        duration_ms=observation.pop("duration_ms", 0.0),
        error=error,
        kind="ticket_analysis",
        protocol=protocol,
        requested_model=requested,
        image_bytes=sum(len(data) for _, data, _ in prepared) if observation["sent"] else None,
        skipped=skipped,
        **{k: v for k, v in observation.items() if k != "response_received"},
        **token_fields,
        image_count=image_count if observation["sent"] else 0,
        images_skipped=skipped,
        category_rejected=rejected,
    )
    _log("llm_live_failed" if error else "llm_live", metadata)
    if error:
        raise GatewayError(metadata) from None
    category, output = result
    return Draft(
        replay_match="live",
        category_code=category,
        mode="live",
        prompt_version=prompt_version,
        ai_urgency=output.ai_urgency,
        metadata=metadata,
        summary=output.summary,
        urgency_reason=output.urgency_reason,
        model=metadata.model,
        followup_questions=tuple(questions),
        hazard_signals=tuple(output.hazard_signals),
        rule_signal_assessment=tuple(x.model_dump() for x in output.rule_signal_assessment),
    )


async def _draft_report_live(
    text, result_type, *, policy, transport, before_send, protocol, cache_lookup
) -> ReportDraft:
    from app.schemas import ReportStructure

    error, model, usage, structure = None, None, None, None
    observation, token_fields, requested = {"sent": False}, {}, None
    policy = policy or GatewayPolicy()
    protocol = protocol or policy.llm_protocol
    prompt_version, schema_version = live_versions("report_analysis", protocol)
    try:
        if protocol not in {"v1", "v2"}:
            raise _LiveFailure("invalid_input")
        if not policy.live_ready():
            raise _LiveFailure("live_processing_unavailable")
        _checked_text(text)
        if result_type not in {"completed", "temporary", "revisit", "needs_review"}:
            raise _LiveFailure("invalid_input")
        model = policy.live_model(vision=False) or policy.live_model(vision=True)
        chat_completions_url(policy)
        body = _request_body(
            model,
            prompts.REPORT_V2_SYSTEM if protocol == "v2" else prompts.REPORT_SYSTEM,
            prompts.report_v2_user(text, result_type) if protocol == "v2" else
            prompts.report_user_message(text, result_type),
            "report_structure",
            COMPACT_REPORT_SCHEMA if protocol == "v2" else REPORT_JSON_SCHEMA,
            output_cap=(policy.llm_max_output_tokens if protocol == "v2"
                        else LIVE_MAX_COMPLETION_TOKENS),
        )
        _text_request_options(body, policy)
        requested = (
            model
            if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model)
            else None
        )
        if cache_lookup is not None:
            cached = await cache_lookup(body, [], [])
            if isinstance(cached, ReportDraft):
                return cached
            if cached is not None:
                raise _LiveFailure("lease_lost")
        response = await _send(policy, transport, body, observation, before_send, 0)
        observation["response_received"] = True
        model = _response_model(response, model) if isinstance(response, dict) else None
        usage = _usage(response) if isinstance(response, dict) else None
        observation["usage_valid"] = _usage_valid(response)
        tier = response.get("service_tier") if isinstance(response, dict) else None
        observation["service_tier"] = (
            tier if isinstance(tier, str) and re.fullmatch(r"[a-z_]{1,30}", tier) else "unknown"
        )
        token_fields = _token_fields(response)
        if policy.llm_chat_explicit_cache and _unexpected_cache_write(response):
            observation["usage_valid"] = False
            raise _LiveFailure("unexpected_cache_write")
        parsed = _parse_content(response)
        try:
            if protocol == "v2":
                compact = CompactReportOutput.model_validate(parsed)
                reject_promises([*compact.a, *compact.m, compact.x])
                output = LiveReportOutput.model_validate({
                    "actions": compact_items(compact.a, 40),
                    "materials_used": compact_items(compact.m, 20),
                    "residual_issue": compact_line(compact.x, 80),
                    "revisit_needed": compact.v,
                })
            else:
                actions = parsed.get("actions")
                materials = parsed.get("materials_used")
                reject_promises([*(actions if isinstance(actions, list) else []),
                                 *(materials if isinstance(materials, list) else []),
                                 parsed.get("residual_issue")])
                output = LiveReportOutput.model_validate(parsed)
            structure = ReportStructure.model_validate(
                {
                    "actions": [_one_line(item) for item in output.actions],
                    "materials_used": [_one_line(item) for item in output.materials_used],
                    "residual_issue": output.residual_issue.strip(),
                    # The worker's declared revisit result is never weakened by the model.
                    "revisit_needed": output.revisit_needed or result_type == "revisit",
                    "photo_comparison": "not_performed",
                    "source": "live",
                },
                strict=True,
            )
        except ValidationError:
            raise _LiveFailure("invalid_schema") from None
    except _LiveFailure as exc:
        error = exc.code
    except Exception:
        error = "configuration_error"
    metadata = LiveMetadata(
        prompt_version=prompt_version,
        schema_version=schema_version,
        model=model if observation.get("response_received") else None,
        usage=usage,
        attempts=int(observation["sent"]),
        duration_ms=observation.pop("duration_ms", 0.0),
        error=error,
        kind="report_analysis",
        protocol=protocol,
        requested_model=requested,
        **{k: v for k, v in observation.items() if k != "response_received"},
        **token_fields,
    )
    _log("report_live_failed" if error else "report_live", metadata)
    if error:
        raise GatewayError(metadata) from None
    return ReportDraft(structure=structure, mode="live", metadata=metadata)

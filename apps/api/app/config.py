import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[3]


def booth_https_origin(value: str) -> bool:
    try:
        origin = urlsplit(value)
        host, port = origin.hostname or "", origin.port
    except ValueError:
        return False
    labels = host.split(".")
    if (
        len(host) > 253 or len(labels) < 2 or not re.search("[a-z]", labels[-1])
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in labels)
        or port in (0, 443)
    ):
        return False
    suffix = f":{port}" if port is not None else ""
    return value == f"https://{host}{suffix}"


def isolated_settings_requested() -> bool:
    value = os.environ.get("BARO_ISOLATED_SETTINGS")
    if value is not None and value != "1":
        raise ValueError("invalid_isolated_settings_flag")
    return value == "1"


class ExplicitEnvironmentSettings(BaseSettings):
    def __init__(self, **values):
        if isolated_settings_requested():
            values["_env_file"] = None
            values["_secrets_dir"] = None
        super().__init__(**values)

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        if isolated_settings_requested():
            return init_settings, env_settings
        return init_settings, env_settings, dotenv_settings, file_secret_settings


class Settings(ExplicitEnvironmentSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", hide_input_in_errors=True
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    deployment_mode: Literal["demo", "service", "prototype", "public_demo"] = "demo"
    service_namespace: str = "baro_service"
    prototype_namespace: str = "baro_prototype"
    public_ip_boundary: Literal["direct_local", "vercel"] = "direct_local"
    public_demo_namespace: str = ""
    public_active_sessions_limit: int = Field(default=100, ge=1)
    public_ip_hourly_limit: int = Field(default=300, ge=1)
    public_ticket_hourly_limit: int = Field(default=10, ge=1)
    public_ticket_storage_limit: int = Field(default=1000, ge=1)
    prototype_admin_password: SecretStr = Field(default=SecretStr(""))
    planning_dataset_path: str = ""
    planning_dataset_sha256: str = ""
    planning_dataset_provider: Literal["file", "db"] = "file"
    location_gps_max_accuracy_m: float = Field(default=150, ge=0, le=100000)
    location_max_distance_m: float = Field(default=300, ge=0, le=100000)
    travel_detour_factor: float = Field(default=1.5, gt=0, le=100, allow_inf_nan=False)
    travel_speed_kmh: float = Field(default=10, gt=0, le=500, allow_inf_nan=False)
    travel_entry_minutes: float = Field(default=4, ge=0, le=1000, allow_inf_nan=False)
    mail_delivery: Literal["local", "smtp", "gmail", "disabled"] = "disabled"
    mail_sink_path: Path = ROOT / ".local/service/mail"
    export_encryption_key: SecretStr = Field(default=SecretStr(""))
    mail_encryption_key: SecretStr = Field(default=SecretStr(""))
    gmail_external_enabled: bool = False
    gmail_credentials_dir: Path = Path.home() / "Library/Application Support/BaroAI/gmail"
    smtp_external_enabled: bool = False
    smtp_host: str = "127.0.0.1"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: SecretStr = Field(default=SecretStr(""))
    smtp_pass: SecretStr = Field(default=SecretStr(""))
    mail_from: str = "baroai@localhost"
    smtp_security: Literal["starttls", "tls", "plain_test"] = "starttls"
    smtp_timeout_seconds: int = Field(default=5, ge=1, le=15)
    otp_pepper: SecretStr = Field(default=SecretStr(""))
    allowed_email_domain: str = "snu.ac.kr"
    secure_cookie: bool = False
    database_url: SecretStr
    redis_url: SecretStr | None = None
    llm_fake: bool = True
    local_staff_auth: bool = False
    demo_auth: bool = False
    booth_synthetic_live_only: bool = False
    booth_free_input_live: bool = False
    auto_confirm: bool = False
    app_base_url: str = "http://localhost:5173"
    service_public_origin: str = ""
    allowed_origin: str = ""
    session_hours: int = Field(default=12, ge=1, le=24)
    worker_token_hours: int = Field(default=24, ge=1, le=168)
    llm_protocol: Literal["v1", "v2"] = "v1"
    llm_max_output_tokens: int = Field(default=200, ge=1, le=1500)
    llm_primary: str = "openai"
    # Live LLM settings (used only when LLM_FAKE=false). Model IDs are env-only.
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model_text_small: str = ""
    openai_model_vision: str = ""
    llm_chat_reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh"
    ] | None = None
    llm_chat_explicit_cache: bool = False
    llm_timeout_s: float = Field(default=20.0, gt=0, le=120)
    llm_photo_wait_s: int = Field(default=20, ge=0, le=300)
    llm_max_images: int = Field(default=1, ge=1, le=3)
    llm_image_max_side: int = Field(default=512, ge=64, le=4096)
    llm_daily_call_cap: int = Field(default=300, ge=0, le=100_000)
    daily_llm_budget_usd: str = ""
    llm_price_table: dict = Field(default_factory=dict)
    llm_price_as_of: str = ""
    llm_price_source: str = ""
    storage_path: Path = ROOT / "data/private/uploads"
    jwt_secret: SecretStr = Field(default=SecretStr(""))

    @property
    def mail_link_origin(self) -> str:
        return self.service_public_origin or self.app_base_url

    @field_validator("allowed_email_domain", mode="before")
    @classmethod
    def normalize_email_domain(cls, value):
        if not isinstance(value, str) or not value.strip().isascii():
            raise ValueError("ALLOWED_EMAIL_DOMAIN must be ASCII")
        return value.strip().casefold()

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            if url.drivername != "postgresql+asyncpg" or not url.database or not url.host:
                raise ValueError
        except Exception:
            raise ValueError("DATABASE_URL must be a PostgreSQL asyncpg connection URL") from None
        return value

    @model_validator(mode="after")
    def environment_guards(self) -> "Settings":
        if self.service_public_origin and (
            self.deployment_mode != "service"
            or self.app_env not in {"dev", "test"}
            or not booth_https_origin(self.service_public_origin)
            or urlsplit(self.service_public_origin).port is not None
        ):
            raise ValueError("Service public access requires one canonical HTTPS origin")
        if self.booth_free_input_live:
            from app.services.llm import decimal_value, valid_base_url

            database = make_url(self.database_url.get_secret_value())
            redis = make_url(self.redis_url.get_secret_value()) if self.redis_url else None
            budget = decimal_value(self.daily_llm_budget_usd or "2.00")
            vision_model = self.openai_model_vision.strip()
            vision_rates = self.llm_price_table.get(vision_model)
            image_bound = (
                vision_rates.get("image_input_token_bound")
                if isinstance(vision_rates, dict) else None
            )
            if vision_model and (
                type(image_bound) is not int or not 0 < image_bound <= 1_000_000_000
            ):
                raise ValueError("Booth vision requires an image input token bound")
            if (
                self.booth_synthetic_live_only
                or self.deployment_mode != "demo"
                or self.app_env != "dev"
                or not self.demo_auth
                or self.planning_dataset_provider != "db"
                or self.llm_fake
                or self.mail_delivery != "disabled"
                or self.gmail_external_enabled
                or self.smtp_external_enabled
                or self.llm_max_images != 1
                or not valid_base_url(self.openai_base_url)
                or budget is None
                or not 0 < budget <= 5
                or not self.secure_cookie
                or not booth_https_origin(self.app_base_url)
                or self.allowed_origin not in {"", self.app_base_url}
                or database.host != "127.0.0.1"
                or not re.fullmatch(r"booth_[0-9a-f]{32}", database.database or "")
                or database.username != database.database
                or redis is None
                or redis.drivername != "redis"
                or redis.host != "127.0.0.1"
                or redis.database != "0"
            ):
                raise ValueError("Booth free-input AI requires isolated HTTPS demo settings")
            self.daily_llm_budget_usd = str(budget)
        if self.booth_synthetic_live_only and (
            self.deployment_mode != "demo" or self.app_env != "dev" or not self.demo_auth
            or self.planning_dataset_provider != "db" or self.llm_fake
            or self.mail_delivery != "disabled" or bool(self.openai_model_vision)
            or self.daily_llm_budget_usd not in {"0.30", "2.00"}
            or self.openai_base_url.rstrip("/") != "https://api.openai.com/v1"
        ):
            raise ValueError("Synthetic booth guard requires isolated text-only live demo")
        if self.local_staff_auth and (
            self.deployment_mode != "service" or self.app_env not in {"dev", "test"}
        ):
            raise ValueError("Local staff auth requires local service dev/test")
        if self.planning_dataset_provider == "db" and (
            self.planning_dataset_path or self.planning_dataset_sha256
        ):
            raise ValueError("DB dataset provider forbids file path and pin")
        if self.deployment_mode == "service":
            if self.allowed_email_domain != "snu.ac.kr":
                raise ValueError("Service authentication requires snu.ac.kr")
            if self.demo_auth:
                raise ValueError("Service mode forbids DEMO_AUTH")
            if self.service_namespace != "baro_service":
                raise ValueError("Unsupported service namespace")
            database = make_url(self.database_url.get_secret_value())
            expected = "baro_service_test" if self.app_env == "test" else "baro_service_dev"
            if database.database != expected:
                raise ValueError("Service mode requires a separate service database")
            if not self.redis_url:
                raise ValueError("Service Redis namespace is required")
            redis = make_url(self.redis_url.get_secret_value())
            if redis.database != ("3" if self.app_env == "test" else "2"):
                raise ValueError("Service Redis database must be separate")
            origin = urlsplit(self.app_base_url)
            if origin.path not in ("", "/") or origin.query or origin.fragment or origin.username:
                raise ValueError("APP_BASE_URL must be an origin")
            if self.app_env != "prod" and (
                origin.scheme != "http"
                or origin.hostname not in {"127.0.0.1", "localhost"}
                or origin.port != 5174
                or database.host != "127.0.0.1"
                or redis.host != "127.0.0.1"
            ):
                raise ValueError("Local service requires its separate loopback origin")
            scope = "test" if self.app_env == "test" else "dev"
            base = (ROOT / ".local/service" / scope).resolve()
            if (
                not self.storage_path.resolve().is_relative_to(base)
                or not self.mail_sink_path.resolve().is_relative_to(base)
                or self.storage_path.resolve().is_relative_to(self.mail_sink_path.resolve())
                or self.mail_sink_path.resolve().is_relative_to(self.storage_path.resolve())
            ):
                raise ValueError("Service storage and mail require separate private paths")
            if (
                len(self.jwt_secret.get_secret_value()) < 32
                or len(self.otp_pepper.get_secret_value()) < 32
            ):
                raise ValueError("Service authentication secrets must be configured")
            if self.jwt_secret == self.otp_pepper:
                raise ValueError("Session and OTP secrets must differ")
            if self.app_env == "prod":
                if origin.scheme != "https" or not self.secure_cookie:
                    raise ValueError("Production service requires HTTPS and secure cookies")
                # Deployment and mail safeguards require review before production use.
                raise ValueError("Production service is not enabled")
            if self.mail_delivery == "disabled":
                raise ValueError("Service mail delivery must be configured")
        if self.deployment_mode == "public_demo":
            origin = urlsplit(self.app_base_url)
            try:
                valid_origin = (
                    origin.scheme == "https"
                    and bool(origin.hostname)
                    and not origin.username
                    and not origin.password
                    and not origin.path
                    and not origin.query
                    and not origin.fragment
                    and not any(c.isspace() or c == "\\" for c in self.app_base_url)
                    and (origin.port is None or 1 <= origin.port <= 65535)
                )
            except ValueError:
                valid_origin = False
            if (
                not valid_origin
                or not self.secure_cookie
                or self.allowed_origin != self.app_base_url
                or not self.public_demo_namespace.startswith("public_demo_")
                or not 20 <= len(self.public_demo_namespace) <= 60
                or not self.public_demo_namespace.isascii()
                or not self.public_demo_namespace.replace("_", "").isalnum()
                or len(self.jwt_secret.get_secret_value()) < 32
                or self.demo_auth
                or self.local_staff_auth
                or not self.llm_fake
                or self.mail_delivery != "disabled"
                or self.gmail_external_enabled
                or self.smtp_external_enabled
                or self.planning_dataset_path
                or self.planning_dataset_sha256
            ):
                raise ValueError("Public demo requires isolated replay-only configuration")
            # No implicit inheritance of the service's 12-hour default.
            if "session_hours" not in self.model_fields_set:
                self.session_hours = 24
        if self.deployment_mode == "prototype":
            if self.app_env not in {"dev", "test"}:
                raise ValueError("Prototype is restricted to dev/test")
            database = make_url(self.database_url.get_secret_value())
            redis = make_url(self.redis_url.get_secret_value()) if self.redis_url else None
            origin = urlsplit(self.app_base_url)
            base = (ROOT / ".local/prototype" / self.app_env).resolve()
            try:
                valid_origin = (
                    bool(origin.hostname)
                    and origin.path == ""
                    and not origin.query
                    and not origin.fragment
                    and not origin.username
                    and not origin.password
                    and not any(c.isspace() or c == "\\" for c in self.app_base_url)
                    and (origin.port is None or 1 <= origin.port <= 65535)
                )
                local_origin = (
                    origin.scheme == "http"
                    and origin.hostname in {"127.0.0.1", "localhost"}
                    and origin.port == 5175
                    and self.allowed_origin in {"", self.app_base_url}
                )
                shared_origin = (
                    origin.scheme == "https"
                    and self.secure_cookie
                    and self.allowed_origin == self.app_base_url
                )
            except ValueError:
                valid_origin = local_origin = shared_origin = False
            if (
                self.prototype_namespace != "baro_prototype"
                or database.database != f"baro_prototype_{self.app_env}"
                or database.host != "127.0.0.1"
                or redis is None
                or redis.host != "127.0.0.1"
                or redis.database != ("5" if self.app_env == "test" else "4")
                or not valid_origin
                or not (local_origin or shared_origin)
                or not self.storage_path.resolve().is_relative_to(base)
                or self.demo_auth
                or not self.llm_fake
                or self.mail_delivery != "disabled"
                or self.planning_dataset_path
                or self.planning_dataset_sha256
            ):
                raise ValueError("Prototype requires isolated synthetic configuration")
            if (
                len(self.jwt_secret.get_secret_value()) < 32
                or len(self.otp_pepper.get_secret_value()) < 32
                or len(self.prototype_admin_password.get_secret_value()) < 12
                or len(
                    {
                        self.jwt_secret.get_secret_value(),
                        self.otp_pepper.get_secret_value(),
                        self.prototype_admin_password.get_secret_value(),
                    }
                )
                != 3
            ):
                raise ValueError("Prototype requires separate configured secrets")
        if self.mail_delivery != "disabled":
            from cryptography.fernet import Fernet

            if self.mail_encryption_key in (self.jwt_secret, self.otp_pepper):
                raise ValueError("Mail encryption key must be separate")
            try:
                Fernet(self.mail_encryption_key.get_secret_value().encode())
            except Exception:
                raise ValueError("MAIL_ENCRYPTION_KEY must be a Fernet key") from None
        if self.mail_delivery == "gmail":
            from app.services.gmail import gmail_policy_error

            error = gmail_policy_error(self)
            if error:
                raise ValueError(f"Gmail configuration rejected: {error}")
        if self.mail_delivery == "smtp":
            from app.smtp_policy import smtp_policy_error

            error = smtp_policy_error(self)
            if error:
                raise ValueError(f"SMTP configuration rejected: {error}")
        if self.export_encryption_key.get_secret_value():
            from cryptography.fernet import Fernet

            if self.export_encryption_key in (
                self.jwt_secret,
                self.otp_pepper,
                self.mail_encryption_key,
            ):
                raise ValueError("Export encryption key must be separate")
            try:
                Fernet(self.export_encryption_key.get_secret_value().encode())
            except Exception:
                raise ValueError("EXPORT_ENCRYPTION_KEY must be a Fernet key") from None
        if self.demo_auth and self.app_env != "dev":
            raise ValueError("DEMO_AUTH is only permitted in dev")
        if self.auto_confirm:
            raise ValueError("AUTO_CONFIRM is unavailable in this foundation")
        return self

"""Application configuration loaded exclusively from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when a required setting is missing or invalid."""


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"Valor booleano inválido: {value!r}")


def _as_int(name: str, value: str | None, default: int | None = None) -> int:
    if value is None or not value.strip():
        if default is not None:
            return default
        raise ConfigurationError(f"Variável obrigatória ausente: {name}")
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} deve ser um número inteiro") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings."""

    api_id: int
    api_hash: str
    channel: str
    telegram_phone: str | None
    telegram_2fa_password: str | None

    twitter_username: str | None
    twitter_password: str | None
    twitter_email: str | None
    x_auto_login: bool
    x_headless: bool
    x_base_url: str
    x_char_limit: int
    x_max_images: int
    x_navigation_timeout_ms: int
    x_upload_timeout_ms: int

    retry_base_seconds: int
    retry_max_seconds: int
    startup_grace_seconds: int
    publish_interval_seconds: int
    recover_pending_on_start: bool
    log_level: str

    project_root: Path
    storage_dir: Path
    logs_dir: Path
    media_dir: Path
    telegram_session_path: Path
    x_profile_dir: Path
    published_path: Path
    pending_path: Path
    rate_limit_path: Path

    @classmethod
    def load(cls, env_file: str | Path | None = None) -> "Settings":
        """Load and validate settings from ``.env`` and the process environment."""

        project_root = Path(__file__).resolve().parent
        load_dotenv(dotenv_path=env_file or project_root / ".env", override=True)

        api_hash = os.getenv("API_HASH", "").strip()
        channel = os.getenv("CHANNEL", "").strip()
        if not api_hash:
            raise ConfigurationError("Variável obrigatória ausente: API_HASH")
        if not channel:
            raise ConfigurationError("Variável obrigatória ausente: CHANNEL")

        storage_dir = project_root / os.getenv("STORAGE_DIR", "storage")
        logs_dir = storage_dir / "logs"
        media_dir = storage_dir / "media"
        x_profile_dir = storage_dir / "x_profile"

        settings = cls(
            api_id=_as_int("API_ID", os.getenv("API_ID")),
            api_hash=api_hash,
            channel=channel,
            telegram_phone=os.getenv("TELEGRAM_PHONE") or None,
            telegram_2fa_password=os.getenv("TELEGRAM_2FA_PASSWORD") or None,
            twitter_username=os.getenv("TWITTER_USERNAME") or None,
            twitter_password=os.getenv("TWITTER_PASSWORD") or None,
            twitter_email=os.getenv("TWITTER_EMAIL") or None,
            x_auto_login=_as_bool(os.getenv("X_AUTO_LOGIN"), False),
            x_headless=_as_bool(os.getenv("X_HEADLESS"), True),
            x_base_url=os.getenv("X_BASE_URL", "https://x.com").rstrip("/"),
            x_char_limit=_as_int("X_CHAR_LIMIT", os.getenv("X_CHAR_LIMIT"), 280),
            x_max_images=_as_int("X_MAX_IMAGES", os.getenv("X_MAX_IMAGES"), 4),
            x_navigation_timeout_ms=_as_int(
                "X_NAVIGATION_TIMEOUT_MS", os.getenv("X_NAVIGATION_TIMEOUT_MS"), 45_000
            ),
            x_upload_timeout_ms=_as_int(
                "X_UPLOAD_TIMEOUT_MS", os.getenv("X_UPLOAD_TIMEOUT_MS"), 300_000
            ),
            retry_base_seconds=_as_int(
                "RETRY_BASE_SECONDS", os.getenv("RETRY_BASE_SECONDS"), 5
            ),
            retry_max_seconds=_as_int(
                "RETRY_MAX_SECONDS", os.getenv("RETRY_MAX_SECONDS"), 300
            ),
            startup_grace_seconds=_as_int(
                "STARTUP_GRACE_SECONDS", os.getenv("STARTUP_GRACE_SECONDS"), 0
            ),
            publish_interval_seconds=_as_int(
                "PUBLISH_INTERVAL_SECONDS",
                os.getenv("PUBLISH_INTERVAL_SECONDS"),
                600,
            ),
            recover_pending_on_start=_as_bool(
                os.getenv("RECOVER_PENDING_ON_START"),
                False,
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            project_root=project_root,
            storage_dir=storage_dir,
            logs_dir=logs_dir,
            media_dir=media_dir,
            telegram_session_path=storage_dir / "telegram",
            x_profile_dir=x_profile_dir,
            published_path=storage_dir / "published.json",
            pending_path=storage_dir / "pending.json",
            rate_limit_path=storage_dir / "rate_limit.json",
        )
        settings.ensure_directories()
        settings.validate()
        return settings

    def ensure_directories(self) -> None:
        """Create all runtime directories if they do not exist."""

        for directory in (
            self.storage_dir,
            self.logs_dir,
            self.media_dir,
            self.x_profile_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        """Validate cross-field constraints."""

        if not self.twitter_username:
            raise ConfigurationError("Variável obrigatória ausente: TWITTER_USERNAME")
        if self.x_char_limit <= 0:
            raise ConfigurationError("X_CHAR_LIMIT deve ser maior que zero")
        if not 1 <= self.x_max_images <= 4:
            raise ConfigurationError("X_MAX_IMAGES deve estar entre 1 e 4")
        if self.x_navigation_timeout_ms <= 0 or self.x_upload_timeout_ms <= 0:
            raise ConfigurationError("Os timeouts do X devem ser maiores que zero")
        if self.retry_base_seconds <= 0:
            raise ConfigurationError("RETRY_BASE_SECONDS deve ser maior que zero")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ConfigurationError(
                "RETRY_MAX_SECONDS deve ser maior ou igual a RETRY_BASE_SECONDS"
            )
        if self.startup_grace_seconds < 0:
            raise ConfigurationError("STARTUP_GRACE_SECONDS não pode ser negativo")
        if self.publish_interval_seconds < 0:
            raise ConfigurationError(
                "PUBLISH_INTERVAL_SECONDS não pode ser negativo"
            )
        if self.x_auto_login and not (self.twitter_username and self.twitter_password):
            raise ConfigurationError(
                "X_AUTO_LOGIN=true exige TWITTER_USERNAME e TWITTER_PASSWORD"
            )


from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "VRM Meter"
    app_version: str = "0.1.0"
    app_env: str = "development"
    debug: bool = False

    host: str = "0.0.0.0"
    port: int = 8000

    base_dir: Path = Path(__file__).resolve().parent.parent
    data_dir: Path = base_dir / "data"

    # SQLite DB URL
    db_url: str = f"sqlite+aiosqlite:///{base_dir / 'data' / 'vrmmeter.db'}"

    # VRM API configuration defaults
    vrm_api_base_url: str = "https://vrmapi.victronenergy.com/v2"
    vrm_api_token: str | None = None  # Fallback if not set in DB
    sync_interval_minutes: int = 30
    scheduler_enabled: bool = True


settings = Settings()

# Ensure data directory exists
settings.data_dir.mkdir(parents=True, exist_ok=True)

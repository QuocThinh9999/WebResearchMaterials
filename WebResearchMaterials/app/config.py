from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    pghost: str = Field(alias="PGHOST")
    pgport: int = Field(default=5432, alias="PGPORT")
    pgdatabase: str = Field(alias="PGDATABASE")
    pguser: str = Field(alias="PGUSER")
    pgpassword: str = Field(alias="PGPASSWORD")
    pgsslmode: str = Field(default="prefer", alias="PGSSLMODE")
    pgconnect_timeout: int = Field(default=10, alias="PGCONNECT_TIMEOUT")
    pgappname: str = Field(default="web_research_materials", alias="PGAPPNAME")

    app_secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(48), alias="APP_SECRET_KEY")
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")
    upload_dir: Path = Field(default=Path("uploads"), alias="UPLOAD_DIR")
    max_upload_mb: int = Field(default=20, alias="MAX_UPLOAD_MB")
    pdf_max_redirects: int = Field(default=3, ge=0, le=10, alias="PDF_MAX_REDIRECTS")
    pdf_max_retries: int = Field(default=3, ge=1, le=20, alias="PDF_MAX_RETRIES")
    pdf_download_workers: int = Field(default=6, ge=1, le=20, alias="PDF_DOWNLOAD_WORKERS")
    pdf_download_per_host: int = Field(default=2, ge=1, le=20, alias="PDF_DOWNLOAD_PER_HOST")
    unpaywall_email: str | None = Field(default=None, alias="UNPAYWALL_EMAIL")
    pdf_browser_enabled: bool = Field(default=True, alias="PDF_BROWSER_ENABLED")
    pdf_browser_cdp_url: str | None = Field(default=None, alias="PDF_BROWSER_CDP_URL")
    pdf_browser_profile_dir: Path = Field(default=Path(".pdf-browser-profile"), alias="PDF_BROWSER_PROFILE_DIR")
    pdf_browser_headless: bool = Field(default=True, alias="PDF_BROWSER_HEADLESS")

    scopus_api_key: str | None = Field(default=None, alias="SCOPUS_API_KEY")
    scopus_inst_token: str | None = Field(default=None, alias="SCOPUS_INST_TOKEN")
    scopus_api_url: str = Field(
        default="https://api.elsevier.com/content/search/scopus",
        alias="SCOPUS_API_URL",
    )
    wos_api_key: str | None = Field(default=None, alias="WOS_API_KEY")
    wos_api_url: str = Field(default="https://api.clarivate.com/api/wos", alias="WOS_API_URL")
    api_timeout_seconds: float = Field(default=30.0, alias="API_TIMEOUT_SECONDS")
    search_max_results: int = Field(default=500, alias="SEARCH_MAX_RESULTS")

    ollama_base_url: str = Field(default="http://127.0.0.1:11434", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field(default="qwen3:4b", alias="OLLAMA_MODEL")
    ollama_embedding_model: str = Field(
        default="qwen3-embedding:0.6b",
        alias="OLLAMA_EMBEDDING_MODEL",
    )
    ollama_timeout_seconds: float = Field(default=900.0, ge=10, alias="OLLAMA_TIMEOUT_SECONDS")
    ai_relevance_threshold: int = Field(default=70, ge=0, le=100, alias="AI_RELEVANCE_THRESHOLD")
    ai_top_chunks: int = Field(default=8, ge=1, le=50, alias="AI_TOP_CHUNKS")
    ai_max_retries: int = Field(default=3, ge=1, le=20, alias="AI_MAX_RETRIES")
    ai_lookup_workers: int = Field(default=2, ge=1, le=4, alias="AI_LOOKUP_WORKERS")
    ai_allow_pdf_vision: bool = Field(default=False, alias="AI_ALLOW_PDF_VISION")

    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.pguser,
            password=self.pgpassword,
            host=self.pghost,
            port=self.pgport,
            database=self.pgdatabase,
            query={
                "sslmode": self.pgsslmode,
                "connect_timeout": str(self.pgconnect_timeout),
                "application_name": self.pgappname,
            },
        )

    @property
    def upload_path(self) -> Path:
        path = self.upload_dir
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Scraper configuration loaded from env."""
import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScraperSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    anthropic_api_key: str
    claude_model: str = "claude-sonnet-4-6"

    scraper_target_city: str = "Montreal"
    scraper_search_terms: str = "peintre,painter,CCQ"
    scraper_max_pages: int = 5

    @property
    def search_terms_list(self) -> list[str]:
        return [t.strip() for t in self.scraper_search_terms.split(",") if t.strip()]


settings = ScraperSettings()

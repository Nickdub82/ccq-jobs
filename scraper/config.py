"""Scraper configuration loaded from env."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScraperSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database & AI
    database_url: str
    anthropic_api_key: str
    claude_model: str = "claude-sonnet-4-6"

    # Scraper target — what a human would type in Indeed's search bar
    scraper_target_city: str = "Montréal"
    scraper_search_terms: str = "peintre ccq,peintre construction,painter ccq,peintre commercial,peintre compagnon"
    scraper_max_pages: int = 2

    # Serper.dev API
    serper_api_key: str = ""

    # Legacy (kept for compat, unused)
    google_api_key: str = ""
    google_search_engine_id: str = ""

    @property
    def search_terms_list(self) -> list[str]:
        return [t.strip() for t in self.scraper_search_terms.split(",") if t.strip()]


settings = ScraperSettings()

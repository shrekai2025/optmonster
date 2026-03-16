from __future__ import annotations

from app.fetching.browser_fallback import BrowserFallbackDataSource
from app.fetching.datasource import TwitterDataSource
from app.fetching.twikit_source import TwikitDataSource
from app.runtime.settings import Settings


class DataSourceFactory:
    def __init__(
        self,
        settings: Settings,
        *,
        primary_source: TwitterDataSource | None = None,
        browser_source: TwitterDataSource | None = None,
    ) -> None:
        self.settings = settings
        self.primary_source = primary_source or TwikitDataSource(settings)
        self.browser_source = browser_source or BrowserFallbackDataSource()

    def get_primary_source(self) -> TwitterDataSource:
        return self.primary_source

    def browser_fallback_available(self) -> bool:
        return self.settings.browser_fallback_enabled

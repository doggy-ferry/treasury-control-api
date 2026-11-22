"""Application settings loaded from environment variables."""

from decimal import Decimal
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "Treasury FX Payment Control API"
APP_VERSION = "1.0.0"


class Settings(BaseSettings):
    """Runtime configuration. All values can be overridden by environment variables."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    ecb_base_url: str = "https://data-api.ecb.europa.eu/service"
    ecb_timeout_seconds: float = 8.0
    ecb_max_attempts: int = Field(default=3, ge=1, le=5)
    ecb_retry_backoff_seconds: float = 0.5
    ecb_lookback_days: int = 10
    user_agent: str = (
        "treasury-fx-payment-control-api/1.0 "
        "(portfolio demo; synthetic data only)"
    )

    # Control parameters (illustrative defaults for the demo).
    allowed_currencies: str = "USD,GBP,JPY,CHF,CAD,AUD,SEK,NOK,CNY"
    default_base_currency: str = "EUR"
    large_payment_threshold: Decimal = Decimal("100000")  # in base currency
    fx_review_tolerance_pct: Decimal = Decimal("2.0")
    fx_block_tolerance_pct: Decimal = Decimal("10.0")
    max_due_date_horizon_days: int = 365
    max_payments_per_request: int = 500

    @property
    def allowed_currency_set(self) -> frozenset[str]:
        return frozenset(
            c.strip().upper() for c in self.allowed_currencies.split(",") if c.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

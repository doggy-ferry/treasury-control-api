"""Client for the ECB Data Portal (SDMX 2.1 RESTful API, EXR dataflow).

ECB euro reference rates are quoted as units of foreign currency per 1 EUR. Cross rates
for a non-EUR base currency are derived from two EUR quotes of the same date.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

SOURCE_NAME = "ECB Data Portal (EXR, euro foreign exchange reference rates)"


class ECBError(Exception):
    """Base class for reference-data failures. Carries a stable code and HTTP status."""

    code = "ECB_ERROR"
    http_status = 502

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ECBUnavailableError(ECBError):
    code = "ECB_UNAVAILABLE"
    http_status = 503


class ECBBadResponseError(ECBError):
    code = "ECB_BAD_RESPONSE"
    http_status = 502


class ECBNoDataError(ECBError):
    code = "ECB_NO_DATA"
    http_status = 404


@dataclass(frozen=True)
class ReferenceRates:
    base_currency: str
    rate_date: date
    rates: dict[str, Decimal]  # units of currency per 1 unit of base_currency
    source: str = SOURCE_NAME


def parse_sdmx_json(payload: dict[str, Any]) -> dict[str, dict[date, Decimal]]:
    """Parse an ECB SDMX-JSON response into {currency: {date: EUR-based rate}}."""
    try:
        structure = payload["structure"]
        series_dims = structure["dimensions"]["series"]
        ccy_pos = next(i for i, d in enumerate(series_dims) if d["id"] == "CURRENCY")
        ccy_values = [v["id"] for v in series_dims[ccy_pos]["values"]]
        periods = [v["id"] for v in structure["dimensions"]["observation"][0]["values"]]
        result: dict[str, dict[date, Decimal]] = {}
        for key, series in payload["dataSets"][0]["series"].items():
            ccy = ccy_values[int(key.split(":")[ccy_pos])]
            obs: dict[date, Decimal] = {}
            for idx, values in series["observations"].items():
                if values and values[0] is not None:
                    obs[date.fromisoformat(periods[int(idx)])] = Decimal(str(values[0]))
            result[ccy] = obs
        return result
    except (KeyError, IndexError, ValueError, TypeError, StopIteration, InvalidOperation) as exc:
        raise ECBBadResponseError(f"Unexpected ECB response structure: {exc!r}") from exc


def select_rates(
    eur_quotes: dict[str, dict[date, Decimal]],
    currencies: list[str],
    base_currency: str,
    rate_date: date | None,
) -> ReferenceRates:
    """Pick the latest date for which every needed currency has an observation."""
    needed = {c for c in [*currencies, base_currency] if c != "EUR"}
    missing = sorted(c for c in needed if not eur_quotes.get(c))
    if missing:
        raise ECBNoDataError(f"ECB returned no reference rate for: {', '.join(missing)}")
    if needed:
        common = set.intersection(*(set(eur_quotes[c]) for c in needed))
    else:
        common = set()
    if rate_date is not None:
        common = {d for d in common if d <= rate_date}
    if needed and not common:
        raise ECBNoDataError("ECB returned no common reference date for the requested currencies")
    chosen = max(common) if common else (rate_date or date.today())

    def eur_rate(ccy: str) -> Decimal:
        return Decimal(1) if ccy == "EUR" else eur_quotes[ccy][chosen]

    base_rate = eur_rate(base_currency)
    rates = {c: (eur_rate(c) / base_rate) for c in currencies if c != base_currency}
    return ReferenceRates(base_currency=base_currency, rate_date=chosen, rates=rates)


class ECBClient:
    """Async ECB reference-rate client with timeout, limited retries and a clear User-Agent."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._settings = settings
        self._transport = transport
        self._sleep = sleep

    async def get_reference_rates(
        self,
        currencies: list[str],
        base_currency: str = "EUR",
        rate_date: date | None = None,
    ) -> ReferenceRates:
        currencies = sorted({c.upper() for c in currencies})
        base_currency = base_currency.upper()
        to_fetch = sorted({c for c in [*currencies, base_currency] if c != "EUR"})
        if not to_fetch:
            return ReferenceRates(base_currency, rate_date or date.today(), {})
        payload = await self._fetch(to_fetch, rate_date)
        return select_rates(parse_sdmx_json(payload), currencies, base_currency, rate_date)

    async def _fetch(self, currencies: list[str], rate_date: date | None) -> dict[str, Any]:
        s = self._settings
        end = rate_date or date.today()
        params = {
            "startPeriod": (end - timedelta(days=s.ecb_lookback_days)).isoformat(),
            "format": "jsondata",
        }
        if rate_date is not None:
            params["endPeriod"] = rate_date.isoformat()
        url = f"{s.ecb_base_url.rstrip('/')}/data/EXR/D.{'+'.join(currencies)}.EUR.SP00.A"
        headers = {"User-Agent": s.user_agent, "Accept": "application/json"}

        last_error = "unknown error"
        async with httpx.AsyncClient(
            timeout=s.ecb_timeout_seconds, transport=self._transport, headers=headers
        ) as client:
            for attempt in range(1, s.ecb_max_attempts + 1):
                try:
                    resp = await client.get(url, params=params)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = f"{type(exc).__name__}"
                else:
                    if resp.status_code == 200:
                        try:
                            return resp.json()
                        except ValueError as exc:
                            raise ECBBadResponseError("ECB returned a non-JSON body") from exc
                    if resp.status_code == 404:
                        raise ECBNoDataError("ECB has no data for the requested currencies/dates")
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_error = f"HTTP {resp.status_code}"
                    else:
                        raise ECBBadResponseError(f"ECB returned HTTP {resp.status_code}")
                logger.warning("ECB request attempt %d/%d failed: %s", attempt, s.ecb_max_attempts, last_error)
                if attempt < s.ecb_max_attempts:
                    await self._sleep(s.ecb_retry_backoff_seconds * attempt)
        raise ECBUnavailableError(
            f"ECB reference data unavailable after {s.ecb_max_attempts} attempts ({last_error})"
        )

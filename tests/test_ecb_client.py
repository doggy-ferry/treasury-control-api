import asyncio
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.services.ecb_client import (
    ECBBadResponseError,
    ECBClient,
    ECBNoDataError,
    ECBUnavailableError,
    parse_sdmx_json,
)


def sdmx(series: dict[str, dict[str, float]], periods=("2026-11-19", "2026-11-20")):
    """Build a minimal SDMX-JSON payload: {currency: {period: value}}."""
    ccys = list(series)
    data = {}
    for i, c in enumerate(ccys):
        data[f"0:{i}:0:0:0"] = {
            "observations": {str(periods.index(p)): [v] for p, v in series[c].items()}
        }
    return {
        "structure": {
            "dimensions": {
                "series": [
                    {"id": "FREQ", "values": [{"id": "D"}]},
                    {"id": "CURRENCY", "values": [{"id": c} for c in ccys]},
                    {"id": "CURRENCY_DENOM", "values": [{"id": "EUR"}]},
                    {"id": "EXR_TYPE", "values": [{"id": "SP00"}]},
                    {"id": "EXR_SUFFIX", "values": [{"id": "A"}]},
                ],
                "observation": [{"id": "TIME_PERIOD", "values": [{"id": p} for p in periods]}],
            }
        },
        "dataSets": [{"series": data}],
    }


def make_client(handler, settings):
    return ECBClient(settings, transport=httpx.MockTransport(handler), sleep=_nosleep)


async def _nosleep(_):
    return None


def run(coro):
    return asyncio.run(coro)


def test_parse_and_success(fast_settings):
    seen = {}

    def handler(request: httpx.Request):
        seen["ua"] = request.headers["user-agent"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json=sdmx({"USD": {"2026-11-20": 1.1}, "GBP": {"2026-11-20": 0.84}}))

    c = make_client(handler, fast_settings)
    ref = run(c.get_reference_rates(["USD", "GBP"], "EUR"))
    assert ref.rate_date == date(2026, 11, 20)
    assert ref.rates["USD"] == Decimal("1.1")
    assert "treasury-fx-payment-control-api" in seen["ua"]
    assert "D.GBP+USD.EUR.SP00.A" in seen["url"]


def test_cross_rate_uses_common_date_and_rate_date_cap(fast_settings):
    payload = sdmx({"USD": {"2026-11-19": 1.0, "2026-11-20": 1.1}, "GBP": {"2026-11-19": 0.8}})
    c = make_client(lambda r: httpx.Response(200, json=payload), fast_settings)
    ref = run(c.get_reference_rates(["GBP"], "USD", date(2026, 11, 20)))
    assert ref.rate_date == date(2026, 11, 19)  # latest common date
    assert ref.rates["GBP"] == Decimal("0.8")


def test_eur_only_needs_no_call(fast_settings):
    def boom(_):
        raise AssertionError("no HTTP call expected")

    ref = run(make_client(boom, fast_settings).get_reference_rates(["EUR"], "EUR"))
    assert ref.rates == {}


def test_timeout_retries_then_unavailable(fast_settings):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ECBUnavailableError):
        run(make_client(handler, fast_settings).get_reference_rates(["USD"]))
    assert len(calls) == 3


def test_retry_then_success(fast_settings):
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=sdmx({"USD": {"2026-11-20": 1.1}}))

    ref = run(make_client(handler, fast_settings).get_reference_rates(["USD"]))
    assert state["n"] == 3 and ref.rates["USD"] == Decimal("1.1")


def test_5xx_exhausted(fast_settings):
    with pytest.raises(ECBUnavailableError, match="HTTP 500"):
        run(make_client(lambda r: httpx.Response(500), fast_settings).get_reference_rates(["USD"]))


def test_404_no_data(fast_settings):
    with pytest.raises(ECBNoDataError):
        run(make_client(lambda r: httpx.Response(404), fast_settings).get_reference_rates(["USD"]))


def test_4xx_not_retried(fast_settings):
    calls = []

    def handler(r):
        calls.append(1)
        return httpx.Response(400)

    with pytest.raises(ECBBadResponseError):
        run(make_client(handler, fast_settings).get_reference_rates(["USD"]))
    assert len(calls) == 1


def test_malformed_payloads(fast_settings):
    with pytest.raises(ECBBadResponseError):
        run(make_client(lambda r: httpx.Response(200, text="<html>"), fast_settings).get_reference_rates(["USD"]))
    with pytest.raises(ECBBadResponseError):
        run(make_client(lambda r: httpx.Response(200, json={"foo": 1}), fast_settings).get_reference_rates(["USD"]))
    with pytest.raises(ECBBadResponseError):
        parse_sdmx_json({})


def test_missing_currency_in_response(fast_settings):
    c = make_client(lambda r: httpx.Response(200, json=sdmx({"USD": {"2026-11-20": 1.1}})), fast_settings)
    with pytest.raises(ECBNoDataError, match="GBP"):
        run(c.get_reference_rates(["USD", "GBP"]))


def test_no_common_date(fast_settings):
    payload = sdmx({"USD": {"2026-11-19": 1.1}, "GBP": {"2026-11-20": 0.8}})
    with pytest.raises(ECBNoDataError):
        run(make_client(lambda r: httpx.Response(200, json=payload), fast_settings).get_reference_rates(["USD", "GBP"]))

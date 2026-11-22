"""Shared fixtures. No test touches the real network."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app, get_ecb_client
from app.services.ecb_client import ECBError, ECBUnavailableError, ReferenceRates

RATE_DATE = date(2026, 11, 20)
EUR_RATES = {"USD": Decimal("1.10"), "GBP": Decimal("0.84"), "JPY": Decimal("174.5"), "CHF": Decimal("0.92")}


class FakeECBClient:
    """Mock client: returns fixed rates or raises a configured error."""

    def __init__(self, error: ECBError | None = None):
        self.error = error
        self.calls = 0

    async def get_reference_rates(self, currencies, base_currency="EUR", rate_date=None):
        self.calls += 1
        if self.error:
            raise self.error
        base = Decimal(1) if base_currency == "EUR" else EUR_RATES[base_currency]
        rates = {
            c: (Decimal(1) if c == "EUR" else EUR_RATES[c]) / base
            for c in currencies
            if c != base_currency
        }
        return ReferenceRates(base_currency, RATE_DATE, rates)


@pytest.fixture
def fake_ecb():
    return FakeECBClient()


@pytest.fixture
def client(fake_ecb):
    app.dependency_overrides[get_ecb_client] = lambda: fake_ecb
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def down_client():
    fake = FakeECBClient(error=ECBUnavailableError("ECB reference data unavailable after 3 attempts (ReadTimeout)"))
    app.dependency_overrides[get_ecb_client] = lambda: fake
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def fast_settings():
    return Settings(ecb_retry_backoff_seconds=0, ecb_max_attempts=3)


def make_payment(**overrides):
    base = {
        "payment_id": "PAY-1",
        "supplier_id": "SUP-1",
        "invoice_number": "INV-1",
        "currency": "USD",
        "amount": 1000,
        "due_date": "2026-12-15",
        "contracted_fx_rate": 1.10,
        "beneficiary_country": "US",
        "supplier_status": "ACTIVE",
    }
    base.update(overrides)
    return base


def precheck_body(*payments, **extra):
    return {"as_of_date": "2026-11-23", "payments": list(payments), **extra}


__all__ = ["get_settings"]

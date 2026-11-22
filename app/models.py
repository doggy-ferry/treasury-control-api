"""Pydantic input/output models."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class Status(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# --------------------------------------------------------------------------- health
class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"])
    version: str = Field(examples=["1.0.0"])
    time_utc: datetime


# --------------------------------------------------------------------------- errors
class ErrorDetail(BaseModel):
    code: str = Field(examples=["ECB_UNAVAILABLE"])
    message: str
    source: str = "ECB Data Portal"


class ErrorResponse(BaseModel):
    error: ErrorDetail


# --------------------------------------------------------------------------- FX rates
class FxRatesResponse(BaseModel):
    base_currency: str = Field(examples=["EUR"])
    rate_date: date
    source: str = Field(examples=["ECB Data Portal (EXR, euro foreign exchange reference rates)"])
    quote_convention: str = Field(
        examples=["Units of each listed currency per 1 unit of base_currency"]
    )
    rates: dict[str, Decimal]
    retrieved_at: datetime


# --------------------------------------------------------------------------- payments
class PaymentIn(BaseModel):
    """A single synthetic foreign-currency payment.

    Fields are deliberately optional at the schema level so that missing values are
    reported per payment by the control rules (MISSING_FIELD) instead of rejecting
    the whole batch with a generic validation error.
    """

    payment_id: str | None = None
    supplier_id: str | None = None
    invoice_number: str | None = None
    currency: str | None = None
    amount: Decimal | None = None
    due_date: date | None = None
    contracted_fx_rate: Decimal | None = Field(
        default=None,
        description="Units of payment currency per 1 unit of the request base currency.",
    )
    beneficiary_country: str | None = Field(default=None, description="ISO 3166-1 alpha-2")
    supplier_status: str | None = Field(
        default=None, description="ACTIVE, PENDING_REVIEW, FROZEN, INACTIVE ..."
    )


_EXAMPLE_PAYMENTS = [
    {
        "payment_id": "PAY-0001",
        "supplier_id": "SUP-1001",
        "invoice_number": "INV-2026-0001",
        "currency": "USD",
        "amount": 12500.00,
        "due_date": "2026-12-15",
        "contracted_fx_rate": 1.10,
        "beneficiary_country": "US",
        "supplier_status": "ACTIVE",
    },
    {
        "payment_id": "PAY-0002",
        "supplier_id": "SUP-1002",
        "invoice_number": "INV-2026-0002",
        "currency": "GBP",
        "amount": 8200.50,
        "due_date": "2026-12-20",
        "contracted_fx_rate": 0.84,
        "beneficiary_country": "GB",
        "supplier_status": "ACTIVE",
    },
    {
        "payment_id": "PAY-0003",
        "supplier_id": "SUP-1003",
        "invoice_number": "INV-2026-0003",
        "currency": "USD",
        "amount": -50.00,
        "due_date": "2026-12-18",
        "contracted_fx_rate": 1.10,
        "beneficiary_country": "US",
        "supplier_status": "FROZEN",
    },
]


class PrecheckRequest(BaseModel):
    base_currency: str = Field(
        default="EUR", description="Reporting currency used for rate quotes and thresholds."
    )
    rate_date: date | None = Field(
        default=None, description="Reference rate date. Defaults to the latest ECB publication."
    )
    as_of_date: date | None = Field(
        default=None, description="Date used for due-date checks. Defaults to today (UTC)."
    )
    payments: list[PaymentIn] = Field(min_length=1)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "base_currency": "EUR",
                    "as_of_date": "2026-11-23",
                    "payments": _EXAMPLE_PAYMENTS,
                }
            ]
        }
    }


class PaymentResult(BaseModel):
    payment_id: str | None
    status: Status
    rule_code: str
    severity: Severity
    message: str
    reference_rate: Decimal | None = None
    fx_deviation_pct: Decimal | None = None
    all_findings: list["Finding"] = Field(default_factory=list)


class Finding(BaseModel):
    status: Status
    rule_code: str
    severity: Severity
    message: str


class ReferenceDataInfo(BaseModel):
    source: str
    rate_date: date | None
    base_currency: str
    available: bool
    note: str | None = None


class PrecheckResponse(BaseModel):
    run_id: str
    processed_at: datetime
    overall_status: Status
    total_payments: int
    pass_count: int
    review_count: int
    block_count: int
    reference_data: ReferenceDataInfo
    results: list[PaymentResult]
    disclaimer: str = (
        "Rule-based pre-payment exception screening only. This API does not authorize or "
        "execute payments; final approval remains a human-controlled process."
    )


PaymentResult.model_rebuild()


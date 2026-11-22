"""FastAPI routes and dependency injection only. Business logic lives in rules.py."""

import re
import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app import rules
from app.config import APP_NAME, APP_VERSION, Settings, get_settings
from app.models import (
    ErrorResponse,
    FxRatesResponse,
    HealthResponse,
    PrecheckRequest,
    PrecheckResponse,
    ReferenceDataInfo,
    Status,
)
from app.services.ecb_client import SOURCE_NAME, ECBClient, ECBError

DESCRIPTION = """
Pre-payment **control and exception identification** demo using synthetic data.

* Reference FX rates come from the official ECB Data Portal API.
* The API flags PASS / REVIEW / BLOCK per payment. It **does not authorize or execute
  payments**; final approval remains a human-controlled process.
* Nothing submitted is stored.
"""

app = FastAPI(title=APP_NAME, version=APP_VERSION, description=DESCRIPTION)

_CCY_RE = re.compile(r"^[A-Z]{3}$")


def get_ecb_client(settings: Annotated[Settings, Depends(get_settings)]) -> ECBClient:
    return ECBClient(settings)


@app.exception_handler(ECBError)
async def ecb_error_handler(_: Request, exc: ECBError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": {"code": exc.code, "message": exc.message, "source": "ECB Data Portal"}},
    )


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=APP_VERSION, time_utc=datetime.now(UTC))


def _parse_codes(raw: str, field: str) -> list[str]:
    codes = [c.strip().upper() for c in raw.split(",") if c.strip()]
    bad = [c for c in codes if not _CCY_RE.match(c)]
    if bad or not codes:
        raise HTTPException(422, detail=f"{field} must be ISO 4217 codes (e.g. USD,GBP); invalid: {bad or raw!r}")
    return codes


@app.get(
    "/api/v1/fx/rates",
    response_model=FxRatesResponse,
    tags=["fx"],
    summary="ECB reference FX rates",
    responses={404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def fx_rates(
    ecb: Annotated[ECBClient, Depends(get_ecb_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    currencies: Annotated[str, Query(description="Comma-separated ISO codes", examples=["USD,GBP,JPY"])],
    base_currency: Annotated[str | None, Query(examples=["EUR"])] = None,
    rate_date: Annotated[date | None, Query(description="Optional; latest publication on/before this date")] = None,
) -> FxRatesResponse:
    codes = _parse_codes(currencies, "currencies")
    if len(codes) > 20:
        raise HTTPException(422, detail="At most 20 currencies per request.")
    base = _parse_codes(base_currency or settings.default_base_currency, "base_currency")[0]
    ref = await ecb.get_reference_rates(codes, base, rate_date)
    return FxRatesResponse(
        base_currency=ref.base_currency,
        rate_date=ref.rate_date,
        source=ref.source,
        quote_convention=f"Units of each listed currency per 1 {ref.base_currency}",
        rates=ref.rates,
        retrieved_at=datetime.now(UTC),
    )


@app.post(
    "/api/v1/payments/precheck",
    response_model=PrecheckResponse,
    tags=["payments"],
    summary="Pre-payment control check (no payment is authorized or executed)",
)
async def payments_precheck(
    req: PrecheckRequest,
    ecb: Annotated[ECBClient, Depends(get_ecb_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PrecheckResponse:
    if len(req.payments) > settings.max_payments_per_request:
        raise HTTPException(422, detail=f"At most {settings.max_payments_per_request} payments per request.")
    base = _parse_codes(req.base_currency, "base_currency")[0]
    cfg = rules.RuleConfig(
        allowed_currencies=settings.allowed_currency_set,
        large_payment_threshold=settings.large_payment_threshold,
        fx_review_tolerance_pct=settings.fx_review_tolerance_pct,
        fx_block_tolerance_pct=settings.fx_block_tolerance_pct,
        max_due_date_horizon_days=settings.max_due_date_horizon_days,
    )

    needed = sorted(
        {
            c
            for p in req.payments
            if p.currency
            and (c := p.currency.strip().upper()) in cfg.allowed_currencies
            and c != base
        }
    )
    ref_date: date | None = None
    ref_info_note: str | None = None
    if needed:
        try:
            ref = await ecb.get_reference_rates(needed, base, req.rate_date)
            fx = rules.FxContext(base_currency=base, rates=ref.rates)
            ref_date = ref.rate_date
        except ECBError as exc:
            fx = rules.FxContext(base_currency=base, rates={}, unavailable_reason=f"{exc.code}: {exc.message}")
            ref_info_note = fx.unavailable_reason
    else:
        fx = rules.FxContext(base_currency=base, rates={})
        ref_info_note = "No external rates required for this batch."

    today = req.as_of_date or datetime.now(UTC).date()
    results = rules.evaluate_batch(req.payments, cfg, fx, today)
    return PrecheckResponse(
        run_id=str(uuid.uuid4()),
        processed_at=datetime.now(UTC),
        overall_status=rules.overall_status(results),
        total_payments=len(results),
        pass_count=sum(r.status == Status.PASS for r in results),
        review_count=sum(r.status == Status.REVIEW for r in results),
        block_count=sum(r.status == Status.BLOCK for r in results),
        reference_data=ReferenceDataInfo(
            source=SOURCE_NAME,
            rate_date=ref_date,
            base_currency=base,
            available=ref_date is not None or not needed,
            note=ref_info_note,
        ),
        results=results,
    )

"""Pure business rules for pre-payment control. No I/O, no framework dependencies."""

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.models import Finding, PaymentIn, PaymentResult, Severity, Status

_SEVERITY = {Status.PASS: Severity.LOW, Status.REVIEW: Severity.MEDIUM, Status.BLOCK: Severity.HIGH}
_RANK = {Status.PASS: 0, Status.REVIEW: 1, Status.BLOCK: 2}
_REQUIRED = (
    "payment_id",
    "supplier_id",
    "invoice_number",
    "currency",
    "amount",
    "due_date",
    "contracted_fx_rate",
    "beneficiary_country",
    "supplier_status",
)
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


@dataclass(frozen=True)
class RuleConfig:
    allowed_currencies: frozenset[str]
    large_payment_threshold: Decimal  # expressed in base currency
    fx_review_tolerance_pct: Decimal
    fx_block_tolerance_pct: Decimal
    max_due_date_horizon_days: int = 365


@dataclass(frozen=True)
class FxContext:
    """Reference rates available to the rules (units of currency per 1 base unit)."""

    base_currency: str
    rates: dict[str, Decimal]
    unavailable_reason: str | None = None

    def rate_for(self, currency: str) -> Decimal | None:
        if currency == self.base_currency:
            return Decimal(1)
        return self.rates.get(currency)


def _f(status: Status, code: str, message: str) -> Finding:
    return Finding(status=status, rule_code=code, severity=_SEVERITY[status], message=message)


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _norm(value: str | None) -> str:
    return (value or "").strip().upper()


def evaluate_payment(
    p: PaymentIn,
    cfg: RuleConfig,
    fx: FxContext,
    today: date,
    duplicate_ids: set[str],
    invoice_first_seen: dict[tuple[str, str], int],
    index: int,
    invoice_counts: Counter,
) -> tuple[list[Finding], Decimal | None, Decimal | None]:
    findings: list[Finding] = []
    ref_rate: Decimal | None = None
    deviation: Decimal | None = None

    # 1. Required fields
    missing = [f for f in _REQUIRED if _blank(getattr(p, f))]
    if missing:
        findings.append(_f(Status.BLOCK, "MISSING_FIELD", f"Missing required field(s): {', '.join(missing)}"))

    # 2. Amount
    if p.amount is not None and p.amount <= 0:
        findings.append(_f(Status.BLOCK, "INVALID_AMOUNT", "Amount must be greater than zero."))

    # 3. Currency whitelist
    ccy = _norm(p.currency)
    ccy_allowed = bool(ccy) and ccy in cfg.allowed_currencies
    if ccy and not ccy_allowed:
        findings.append(_f(Status.BLOCK, "CURRENCY_NOT_ALLOWED", f"Currency {ccy} is not on the approved currency list."))

    # 4. Supplier status
    status = _norm(p.supplier_status)
    if status and status != "ACTIVE":
        if status == "PENDING_REVIEW":
            findings.append(_f(Status.REVIEW, "SUPPLIER_PENDING_REVIEW", "Supplier is pending review; manual confirmation required."))
        else:
            findings.append(_f(Status.BLOCK, "SUPPLIER_NOT_ACTIVE", f"Supplier status is {status}; payments are not permitted."))

    # 5. Duplicates
    pid = _norm(p.payment_id)
    if pid and pid in duplicate_ids:
        findings.append(_f(Status.BLOCK, "DUPLICATE_PAYMENT_ID", f"payment_id {p.payment_id} appears more than once in the batch."))
    if p.supplier_id and p.invoice_number:
        key = (_norm(p.supplier_id), _norm(p.invoice_number))
        if invoice_counts[key] > 1:
            if invoice_first_seen[key] == index:
                findings.append(_f(Status.REVIEW, "DUPLICATE_INVOICE", f"Invoice {p.invoice_number} is submitted more than once for this supplier; this is the first occurrence."))
            else:
                findings.append(_f(Status.BLOCK, "DUPLICATE_INVOICE", f"Invoice {p.invoice_number} duplicates an earlier payment for the same supplier."))

    # 6. Due date
    if p.due_date is not None:
        if p.due_date < today:
            findings.append(_f(Status.REVIEW, "DUE_DATE_PAST", f"Due date {p.due_date} is before {today}; payment is overdue."))
        elif (p.due_date - today).days > cfg.max_due_date_horizon_days:
            findings.append(_f(Status.REVIEW, "DUE_DATE_TOO_FAR", f"Due date is more than {cfg.max_due_date_horizon_days} days in the future."))

    # 7. Beneficiary country format
    if p.beneficiary_country and not _COUNTRY_RE.match(p.beneficiary_country.strip().upper()):
        findings.append(_f(Status.REVIEW, "INVALID_COUNTRY_CODE", "beneficiary_country is not a valid ISO 3166-1 alpha-2 code."))

    # 8. FX reference checks (only meaningful for allowed currencies)
    if ccy_allowed:
        ref_rate = fx.rate_for(ccy)
        if ref_rate is None:
            reason = fx.unavailable_reason or f"no ECB reference rate for {ccy}"
            findings.append(_f(Status.REVIEW, "FX_RATE_UNAVAILABLE", f"Reference rate unavailable ({reason}); manual review required."))
        else:
            if p.amount is not None and p.amount > 0:
                base_equiv = p.amount / ref_rate
                if base_equiv >= cfg.large_payment_threshold:
                    findings.append(_f(Status.REVIEW, "LARGE_PAYMENT", f"Estimated value {base_equiv:,.2f} {fx.base_currency} meets the large-payment threshold of {cfg.large_payment_threshold:,.2f}."))
            if p.contracted_fx_rate is not None:
                if p.contracted_fx_rate <= 0:
                    findings.append(_f(Status.BLOCK, "INVALID_FX_RATE", "contracted_fx_rate must be greater than zero."))
                else:
                    deviation = ((p.contracted_fx_rate - ref_rate) / ref_rate * 100).quantize(Decimal("0.01"))
                    absdev = abs(deviation)
                    if absdev > cfg.fx_block_tolerance_pct:
                        findings.append(_f(Status.BLOCK, "FX_RATE_DEVIATION", f"Contracted rate deviates {deviation}% from reference rate {ref_rate:.6f} (block limit {cfg.fx_block_tolerance_pct}%)."))
                    elif absdev > cfg.fx_review_tolerance_pct:
                        findings.append(_f(Status.REVIEW, "FX_RATE_DEVIATION", f"Contracted rate deviates {deviation}% from reference rate {ref_rate:.6f} (review limit {cfg.fx_review_tolerance_pct}%)."))

    return findings, ref_rate, deviation


def evaluate_batch(
    payments: list[PaymentIn], cfg: RuleConfig, fx: FxContext, today: date
) -> list[PaymentResult]:
    id_counts = Counter(_norm(p.payment_id) for p in payments if p.payment_id)
    duplicate_ids = {k for k, n in id_counts.items() if n > 1}
    invoice_counts: Counter = Counter()
    first_seen: dict[tuple[str, str], int] = {}
    for i, p in enumerate(payments):
        if p.supplier_id and p.invoice_number:
            key = (_norm(p.supplier_id), _norm(p.invoice_number))
            invoice_counts[key] += 1
            first_seen.setdefault(key, i)

    results = []
    for i, p in enumerate(payments):
        findings, ref, dev = evaluate_payment(p, cfg, fx, today, duplicate_ids, first_seen, i, invoice_counts)
        if findings:
            primary = max(findings, key=lambda f: _RANK[f.status])  # first of worst status
        else:
            primary = _f(Status.PASS, "ALL_CHECKS_PASSED", "All pre-payment checks passed.")
        results.append(
            PaymentResult(
                payment_id=p.payment_id,
                status=primary.status,
                rule_code=primary.rule_code,
                severity=primary.severity,
                message=primary.message,
                reference_rate=ref.quantize(Decimal("0.000001")) if ref is not None else None,
                fx_deviation_pct=dev,
                all_findings=findings,
            )
        )
    return results


def overall_status(results: list[PaymentResult]) -> Status:
    if any(r.status == Status.BLOCK for r in results):
        return Status.BLOCK
    if any(r.status == Status.REVIEW for r in results):
        return Status.REVIEW
    return Status.PASS

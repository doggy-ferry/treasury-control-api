# Treasury FX Payment Control API

A small FastAPI service that screens batches of **synthetic** foreign-currency payments before release. It validates data, applies pre-payment control rules, compares contracted FX rates with official ECB reference rates, and returns a `PASS` / `REVIEW` / `BLOCK` result for each payment.

> **All data is synthetic. No employer information is used.**
> The API identifies rule-based exceptions but **does not authorize or execute payments**.

Stack: Python 3.12, FastAPI, Pydantic, HTTPX, Uvicorn, Pytest, Docker, GitHub Actions, Render.

## 1. Business problem

Treasury and finance teams that pay suppliers in foreign currencies need to catch problems *before* a payment file goes to approval: duplicate invoices, frozen suppliers, invalid amounts, unsupported currencies, overdue items, unusually large payments, and contracted FX rates that drift away from the market. Doing this manually in spreadsheets is slow, inconsistent and hard to audit.

## 2. Why an API is appropriate

- The same control logic can be called from an ERP export job, a payment-preparation workflow, a spreadsheet macro or a BI tool, with one versioned contract.
- Rules are deterministic, testable code, not logic buried in individual workbooks.
- External reference data (ECB) is isolated behind one service layer, so failures are handled in one place.
- A stateless JSON interface makes it easy to automate and to test with synthetic fixtures.

## 3. Architecture

```
app/
  main.py                 Routes and dependency injection only
  models.py               Pydantic request/response models (Swagger examples)
  rules.py                Pure business rules (no I/O)
  config.py               Environment-based settings
  services/ecb_client.py  ECB Data Portal client (timeout, retries, User-Agent, errors)
tests/                    Pytest + FastAPI TestClient; all external calls mocked
sample_data/              Synthetic request payloads
```

- No database, no authentication, no storage: submitted data is processed in memory and discarded.
- `GET /api/v1/fx/rates` and `POST /api/v1/payments/precheck` get the client through a FastAPI dependency, which tests override with a mock. Production code never falls back to fabricated rates.
- ECB data: `EXR` dataflow of the [ECB Data Portal API](https://data.ecb.europa.eu/help/api/data), daily euro reference rates (SDMX-JSON). Rates are quoted as units of currency per 1 EUR; cross rates for another base are derived from two quotes of the same date.

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Status, version, UTC time |
| GET | `/api/v1/fx/rates?currencies=USD,GBP&base_currency=EUR&rate_date=2026-11-20` | ECB reference rates (`rate_date` optional) |
| POST | `/api/v1/payments/precheck` | Batch pre-payment control check |
| GET | `/docs` | Swagger UI with example payload |

If ECB is unreachable, `/api/v1/fx/rates` returns a structured error (`503 ECB_UNAVAILABLE`, `502 ECB_BAD_RESPONSE`, `404 ECB_NO_DATA`). `/precheck` still responds, but affected payments become `REVIEW` with rule `FX_RATE_UNAVAILABLE`; nothing is auto-passed.

## 4. Control rules

| Rule code | Condition | Status |
| --- | --- | --- |
| `MISSING_FIELD` | Any required field missing or blank | BLOCK |
| `INVALID_AMOUNT` | `amount <= 0` | BLOCK |
| `CURRENCY_NOT_ALLOWED` | Currency not on whitelist (default USD, GBP, JPY, CHF, CAD, AUD, SEK, NOK, CNY) | BLOCK |
| `SUPPLIER_NOT_ACTIVE` | Supplier status other than `ACTIVE` / `PENDING_REVIEW` (e.g. `FROZEN`) | BLOCK |
| `SUPPLIER_PENDING_REVIEW` | Supplier status `PENDING_REVIEW` | REVIEW |
| `DUPLICATE_PAYMENT_ID` | Same `payment_id` more than once in batch | BLOCK |
| `DUPLICATE_INVOICE` | Same supplier + `invoice_number` more than once; later occurrences BLOCK, first occurrence REVIEW | BLOCK / REVIEW |
| `DUE_DATE_PAST` | Due date before `as_of_date` (default today UTC) | REVIEW |
| `DUE_DATE_TOO_FAR` | Due date more than 365 days ahead | REVIEW |
| `LARGE_PAYMENT` | Estimated value in base currency ≥ 100,000 | REVIEW |
| `FX_RATE_DEVIATION` | Contracted vs ECB rate deviates > 2% (REVIEW) or > 10% (BLOCK) | REVIEW / BLOCK |
| `INVALID_FX_RATE` | `contracted_fx_rate <= 0` | BLOCK |
| `FX_RATE_UNAVAILABLE` | ECB rate unavailable or currency not published | REVIEW |
| `INVALID_COUNTRY_CODE` | `beneficiary_country` not ISO alpha-2 | REVIEW |

Each payment returns its most severe finding as `status`, `rule_code`, `severity` (`LOW`/`MEDIUM`/`HIGH`) and `message`, plus `all_findings`. `overall_status` is the worst status in the batch. Thresholds are illustrative and configurable through environment variables (see `app/config.py`: `LARGE_PAYMENT_THRESHOLD`, `FX_REVIEW_TOLERANCE_PCT`, `FX_BLOCK_TOLERANCE_PCT`, `ALLOWED_CURRENCIES`, ...).

`contracted_fx_rate` is expressed as units of payment currency per 1 unit of `base_currency` (default EUR), the same convention as ECB.

## 5. Example request

```json
{
  "base_currency": "EUR",
  "as_of_date": "2026-11-23",
  "payments": [
    {
      "payment_id": "PAY-0001",
      "supplier_id": "SUP-1001",
      "invoice_number": "INV-2026-0001",
      "currency": "USD",
      "amount": 12500.00,
      "due_date": "2026-12-15",
      "contracted_fx_rate": 1.10,
      "beneficiary_country": "US",
      "supplier_status": "ACTIVE"
    },
    {
      "payment_id": "PAY-0002",
      "supplier_id": "SUP-1002",
      "invoice_number": "INV-2026-0002",
      "currency": "USD",
      "amount": 20000.00,
      "due_date": "2026-12-15",
      "contracted_fx_rate": 1.30,
      "beneficiary_country": "US",
      "supplier_status": "FROZEN"
    }
  ]
}
```

More payloads: [`sample_data/sample_all_pass.json`](sample_data/sample_all_pass.json) and [`sample_data/sample_with_exceptions.json`](sample_data/sample_with_exceptions.json). Note: the "all pass" sample uses rates close to ECB rates at the time of writing; because the live reference rate moves, it may later raise an FX deviation `REVIEW`. That is expected behavior.

## 6. Example response

```json
{
  "run_id": "6f1c0c1e-8a52-4d0e-9b8c-0f6c1f0b2a11",
  "processed_at": "2026-11-23T09:30:00Z",
  "overall_status": "BLOCK",
  "total_payments": 2,
  "pass_count": 1,
  "review_count": 0,
  "block_count": 1,
  "reference_data": {
    "source": "ECB Data Portal (EXR, euro foreign exchange reference rates)",
    "rate_date": "2026-11-20",
    "base_currency": "EUR",
    "available": true,
    "note": null
  },
  "results": [
    {
      "payment_id": "PAY-0001",
      "status": "PASS",
      "rule_code": "ALL_CHECKS_PASSED",
      "severity": "LOW",
      "message": "All pre-payment checks passed.",
      "reference_rate": "1.099000",
      "fx_deviation_pct": "0.09",
      "all_findings": []
    },
    {
      "payment_id": "PAY-0002",
      "status": "BLOCK",
      "rule_code": "SUPPLIER_NOT_ACTIVE",
      "severity": "HIGH",
      "message": "Supplier status is FROZEN; payments are not permitted.",
      "reference_rate": "1.099000",
      "fx_deviation_pct": "18.29",
      "all_findings": ["... (includes FX_RATE_DEVIATION, BLOCK)"]
    }
  ],
  "disclaimer": "Rule-based pre-payment exception screening only. ..."
}
```

(Response abbreviated for readability.)

## 7. How to run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

On macOS/Linux, activate with `source .venv/bin/activate`. Open <http://localhost:8000/docs>. The server reads the `PORT` environment variable when started through Docker or Render.

```powershell
curl.exe -X POST http://localhost:8000/api/v1/payments/precheck -H "Content-Type: application/json" --data-binary "@sample_data/sample_with_exceptions.json"
```

## 8. How to run tests

```powershell
pytest
```

Tests use `pytest` and FastAPI `TestClient`. ECB is mocked (fake client and `httpx.MockTransport`); no test needs network access. Coverage is enforced at ≥ 80% (`pytest.ini`) and currently about 99%. GitHub Actions runs the suite on every push and pull request.

## 9. Docker usage

```powershell
docker build -t treasury-fx-control-api .
docker run --rm -p 8000:8000 -e PORT=8000 treasury-fx-control-api
```

## 10. Render deployment

1. Push this repository to GitHub.
2. In Render, choose **New → Blueprint** and select the repository (it reads `render.yaml`), or create a **Web Service** with runtime **Docker**.
3. Use the free plan; health check path `/health`.
4. After deploy, open `https://<your-service>.onrender.com/docs`.

No secrets or API keys are required (the ECB API is public). The container listens on `0.0.0.0` and `$PORT`. Free instances sleep when idle, so the first request can be slow.

## 11. Data and privacy statement

- All companies, suppliers, accounts, payments, invoices and amounts are fictitious and synthetic.
- No employer information, internal field names, rules or file structures are used or inferred.
- The service stores nothing: no database, no persisted request bodies. Do not submit real payment or personal data to a public demo instance.
- The only outbound call is to the public ECB Data Portal for reference rates.

## 12. Known limitations

- ECB reference rates are published once per working day (around 16:00 CET), may be stale, and are **not** dealable rates; they are unsuitable for transaction execution or revaluation.
- Duplicate detection only covers a single submitted batch (no history or database).
- Supplier status, thresholds and tolerances are caller-supplied or illustrative defaults, not validated against any master data.
- No authentication, rate limiting, sanctions screening, bank-detail validation or audit log.
- Currency whitelist is limited to currencies ECB publishes.
- The Docker image and Render blueprint were written but the Render deployment must be performed by the project owner.

## 13. Human review boundary

This API only flags rule-based exceptions. A `PASS` means "no configured rule fired", not "approved". `REVIEW` and `BLOCK` items need investigation by a person, and **final payment approval remains a human-controlled process** performed in the organization's own authorized systems. The API never initiates, authorizes or executes payments.

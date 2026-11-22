import json
from pathlib import Path

from tests.conftest import make_payment, precheck_body

URL = "/api/v1/payments/precheck"
SAMPLES = Path(__file__).parent.parent / "sample_data"


def codes(resp, i=0):
    return {f["rule_code"] for f in resp.json()["results"][i]["all_findings"]}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["version"] and body["time_utc"]


def test_docs_and_openapi_example(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert schema["components"]["schemas"]["PrecheckRequest"]["examples"]


def test_clean_payment_passes(client):
    r = client.post(URL, json=precheck_body(make_payment()))
    assert r.status_code == 200
    body = r.json()
    assert body["overall_status"] == "PASS"
    assert (body["pass_count"], body["review_count"], body["block_count"]) == (1, 0, 0)
    assert body["results"][0]["rule_code"] == "ALL_CHECKS_PASSED"
    assert body["reference_data"]["rate_date"] == "2026-11-20"
    assert "ECB" in body["reference_data"]["source"]
    assert body["run_id"] and body["processed_at"]


def test_duplicate_invoice(client):
    body = precheck_body(make_payment(), make_payment(payment_id="PAY-2"))
    r = client.post(URL, json=body)
    res = r.json()["results"]
    assert res[0]["status"] == "REVIEW" and res[1]["status"] == "BLOCK"
    assert res[1]["rule_code"] == "DUPLICATE_INVOICE"


def test_duplicate_payment_id(client):
    body = precheck_body(make_payment(), make_payment(invoice_number="INV-2"))
    r = client.post(URL, json=body)
    assert all(x["rule_code"] == "DUPLICATE_PAYMENT_ID" for x in r.json()["results"])
    assert r.json()["overall_status"] == "BLOCK"


def test_negative_amount(client):
    r = client.post(URL, json=precheck_body(make_payment(amount=-5)))
    assert r.status_code == 200
    assert r.json()["results"][0]["rule_code"] == "INVALID_AMOUNT"
    assert r.json()["results"][0]["status"] == "BLOCK"


def test_unsupported_currency(client, fake_ecb):
    r = client.post(URL, json=precheck_body(make_payment(currency="XYZ")))
    assert r.json()["results"][0]["rule_code"] == "CURRENCY_NOT_ALLOWED"
    assert fake_ecb.calls == 0  # no rate lookup for unsupported currency


def test_frozen_supplier(client):
    r = client.post(URL, json=precheck_body(make_payment(supplier_status="FROZEN")))
    assert r.json()["results"][0]["rule_code"] == "SUPPLIER_NOT_ACTIVE"
    assert r.json()["results"][0]["severity"] == "HIGH"


def test_pending_supplier_is_review(client):
    r = client.post(URL, json=precheck_body(make_payment(supplier_status="PENDING_REVIEW")))
    assert r.json()["results"][0]["status"] == "REVIEW"


def test_fx_deviation_review_and_block(client):
    r = client.post(
        URL,
        json=precheck_body(
            make_payment(contracted_fx_rate=1.14),  # ~3.6%
            make_payment(payment_id="P2", invoice_number="I2", contracted_fx_rate=1.30),  # ~18%
        ),
    )
    res = r.json()["results"]
    assert (res[0]["status"], res[0]["rule_code"]) == ("REVIEW", "FX_RATE_DEVIATION")
    assert (res[1]["status"], res[1]["rule_code"]) == ("BLOCK", "FX_RATE_DEVIATION")
    assert float(res[0]["fx_deviation_pct"]) > 2


def test_missing_fields_blocked_not_422(client):
    r = client.post(URL, json=precheck_body({"payment_id": "X"}))
    assert r.status_code == 200
    assert r.json()["results"][0]["rule_code"] == "MISSING_FIELD"


def test_due_date_rules(client):
    r = client.post(
        URL,
        json=precheck_body(
            make_payment(due_date="2026-01-01"),
            make_payment(payment_id="P2", invoice_number="I2", due_date="2030-01-01"),
        ),
    )
    assert codes(r, 0) == {"DUE_DATE_PAST"}
    assert codes(r, 1) == {"DUE_DATE_TOO_FAR"}


def test_large_payment_and_bad_country_and_bad_rate(client):
    r = client.post(
        URL,
        json=precheck_body(
            make_payment(amount=500000, beneficiary_country="USA"),
            make_payment(payment_id="P2", invoice_number="I2", contracted_fx_rate=0),
        ),
    )
    assert {"LARGE_PAYMENT", "INVALID_COUNTRY_CODE"} <= codes(r, 0)
    assert "INVALID_FX_RATE" in codes(r, 1)


def test_base_currency_same_as_payment_currency(client):
    r = client.post(URL, json=precheck_body(make_payment(contracted_fx_rate=1), base_currency="USD"))
    assert r.json()["results"][0]["status"] == "PASS"


def test_ecb_down_sends_to_review_not_pass(down_client):
    r = down_client.post(URL, json=precheck_body(make_payment()))
    body = r.json()
    assert r.status_code == 200
    assert body["results"][0]["status"] == "REVIEW"
    assert body["results"][0]["rule_code"] == "FX_RATE_UNAVAILABLE"
    assert body["reference_data"]["available"] is False
    assert body["overall_status"] == "REVIEW"


def test_batch_limit_and_bad_base(client):
    assert client.post(URL, json=precheck_body(make_payment(), base_currency="EU")).status_code == 422
    assert client.post(URL, json={"payments": []}).status_code == 422


def test_sample_files(client):
    ok = client.post(URL, json=json.loads((SAMPLES / "sample_all_pass.json").read_text()))
    assert ok.json()["overall_status"] == "PASS"
    bad = client.post(URL, json=json.loads((SAMPLES / "sample_with_exceptions.json").read_text())).json()
    assert bad["overall_status"] == "BLOCK"
    assert bad["review_count"] >= 1 and bad["block_count"] >= 1


def test_fx_rates_endpoint(client):
    r = client.get("/api/v1/fx/rates", params={"currencies": "usd,gbp", "base_currency": "EUR"})
    assert r.status_code == 200
    body = r.json()
    assert body["rates"]["USD"] == "1.10" and body["rate_date"] == "2026-11-20"


def test_fx_rates_validation(client):
    assert client.get("/api/v1/fx/rates", params={"currencies": "US1"}).status_code == 422
    assert client.get("/api/v1/fx/rates", params={"currencies": ",".join(["USD"] * 21)}).status_code == 422
    assert client.get("/api/v1/fx/rates").status_code == 422


def test_fx_rates_ecb_unavailable_structured_error(down_client):
    r = down_client.get("/api/v1/fx/rates", params={"currencies": "USD"})
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == "ECB_UNAVAILABLE" and "rates" not in r.json()

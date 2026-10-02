"""端到端 HTTP 冒烟：鉴权、隔离、超收 409、完整交付-结算流程。"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from crop_settlement.api import serve_in_thread  # noqa: E402
from tests.support import World  # noqa: E402


class Client:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url
        self.token = token

    def call(self, method: str, path: str, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read().decode("utf-8")
                return resp.status, json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8")
            return exc.code, json.loads(payload) if payload else {}


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World()
        self.server, self.thread, self.url = serve_in_thread(self.w.store)
        self.coop = Client(self.url, "t-coop")
        self.ws1 = Client(self.url, "t-ws1")
        self.ws2 = Client(self.url, "t-ws2")
        self.f1 = Client(self.url, "t-f1")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_health_without_token(self):
        status, body = Client(self.url).call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_missing_token_forbidden(self):
        status, body = Client(self.url).call("GET", "/commitments")
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "forbidden")

    def test_workshop_lists_only_own_commitments(self):
        status, body = self.ws1.call("GET", "/commitments")
        self.assertEqual(status, 200)
        ids = {c["id"] for c in body["commitments"]}
        self.assertEqual(ids, {"com1"})

    def test_cross_workshop_settlement_is_404(self):
        status, body = self.ws2.call("GET", "/commitments/com1/settlement")
        self.assertEqual(status, 404)

    def test_delivery_to_settlement_flow(self):
        # com2 的窗口覆盖今天，ws2 在自己站点称重 100kg 花生
        status, d = self.ws2.call("POST", "/deliveries", {
            "id": "dlv-x", "commitment_id": "com2", "station_id": "st2",
            "gross_kg": "105", "tare_kg": "5", "ticket_no": "TX",
        })
        self.assertEqual(status, 201)
        self.assertEqual(d["net_kg"], "100")

        status, sample = self.ws2.call("POST", "/deliveries/dlv-x/samples", {
            "id": "smp-x", "sealed_code": "SEAL-X",
        })
        self.assertEqual(status, 201)

        status, grading = self.ws2.call("POST", "/samples/smp-x/gradings", {
            "grade": "A", "moisture_pct": "8.5", "impurity_pct": "0.7",
        })
        self.assertEqual(status, 201)

        # 农户在交付后即可看到数量、等级、预计付款日
        status, stmt = Client(self.url, "t-f2").call("GET", "/deliveries/dlv-x")
        self.assertEqual(status, 200)
        self.assertEqual(stmt["net_kg"], "100")
        self.assertEqual(stmt["grade"]["grade"], "A")
        self.assertIn("projected_pay_at", stmt)

        # 超收登记 -> 409
        status, body = self.ws2.call("POST", "/deliveries/dlv-x/dispositions", {
            "kind": "accept", "quantity_kg": "120", "grade": "A",
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "quantity_overflow")

        # 部分接收 90 + 退货 10，配平
        status, _ = self.ws2.call("POST", "/deliveries/dlv-x/dispositions", {
            "kind": "accept", "quantity_kg": "90", "grade": "A",
        })
        self.assertEqual(status, 201)
        status, _ = self.ws2.call("POST", "/deliveries/dlv-x/dispositions", {
            "kind": "return", "quantity_kg": "10", "note": "霉变",
        })
        self.assertEqual(status, 201)

        # 农户能看到退货理由
        status, stmt = Client(self.url, "t-f2").call("GET", "/deliveries/dlv-x")
        kinds = {x["kind"] for x in stmt["dispositions"]}
        self.assertEqual(kinds, {"accept", "return"})

        # 合作社关账
        status, settlement = self.coop.call("POST", "/commitments/com2/close", {})
        self.assertEqual(status, 200)
        self.assertEqual(settlement["accepted_kg"], "90")
        self.assertEqual(settlement["graded_payable"], "810")  # 90*9

    def test_alternate_requires_consent_over_http(self):
        status, d = self.ws2.call("POST", "/deliveries", {
            "id": "dlv-y", "commitment_id": "com2", "station_id": "st2",
            "gross_kg": "50", "tare_kg": "0", "ticket_no": "TY",
        })
        self.assertEqual(status, 201)
        # com2 条款禁止替代去向
        status, body = self.ws2.call("POST", "/deliveries/dlv-y/dispositions", {
            "kind": "alternate", "quantity_kg": "50",
            "alternate_destination": "别处", "farmer_consent": True,
        })
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()

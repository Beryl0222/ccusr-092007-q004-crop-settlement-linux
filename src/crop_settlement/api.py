"""HTTP API（仅用标准库，线程化）。

鉴权：请求头 ``Authorization: Bearer <token>``，令牌在参与方登记时分配；
任何工坊令牌只能访问自己的采购关系——跨关系访问统一得到 404，
不泄露其他采购关系是否存在（见 :meth:`Store.visible_commitment`）。

并发：所有写操作都在仓储的全局事务内完成；需要乐观锁的接口接受
``expected_version`` 字段（交付/承诺的版本号），过期返回 409。
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .errors import DomainError
from .models import (
    CommitmentTerms,
    Deduction,
    DateWindow,
    Party,
    PriceTier,
    to_dict,
)
from .services import SettlementService
from .store import Store


# ---------------------------------------------------------------- 请求工具


def _decimal(body: dict, key: str, default=None) -> Decimal:
    value = body.get(key, default)
    if value is None:
        if default is not None:
            return Decimal(str(default))
        raise ValueError(f"缺少字段 {key}")
    return Decimal(str(value))


def _date(body: dict, key: str) -> date:
    return date.fromisoformat(body[key])


def _terms(body: dict) -> CommitmentTerms:
    t = body["terms"]
    return CommitmentTerms(
        floor_price_per_kg=Decimal(str(t["floor_price_per_kg"])),
        grade_prices=[
            PriceTier(grade=g["grade"], price_per_kg=Decimal(str(g["price_per_kg"])))
            for g in t["grade_prices"]
        ],
        prepayment_amount=Decimal(str(t["prepayment_amount"])),
        delivery_window=DateWindow(
            start=date.fromisoformat(t["delivery_window"]["start"]),
            end=date.fromisoformat(t["delivery_window"]["end"]),
        ),
        payment_terms_days=int(t.get("payment_terms_days", 7)),
        alternate_allowed=bool(t.get("alternate_allowed", True)),
        alternate_handling_fee_per_kg=Decimal(str(t.get("alternate_handling_fee_per_kg", "0"))),
        notes=t.get("notes", ""),
    )


# ---------------------------------------------------------------- 处理器


class _Handler(BaseHTTPRequestHandler):
    server_version = "CropSettlement/1.0"
    service: SettlementService
    store: Store

    # 静音默认访问日志，测试输出保持干净
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    # -- 鉴权 ----------------------------------------------------------

    def _party(self) -> Party:
        auth = self.headers.get("Authorization", "")
        token = ""
        if auth.startswith("Bearer "):
            token = auth[7:]
        token = self.headers.get("X-Auth-Token", token)
        for p in self.store.parties.values():
            if p.token and p.token == token:
                return p
        from .errors import PermissionDenied

        raise PermissionDenied("无效或缺失的访问令牌")

    # -- 读写 ----------------------------------------------------------

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            from .errors import ValidationError

            raise ValidationError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            from .errors import ValidationError

            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- 路由 ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path.strip("/")
            parts = path.split("/") if path else []
            body = self._read_json() if method == "POST" else {}
            handler = self._route(method, parts)
            if handler is None:
                self._send(HTTPStatus.NOT_FOUND, {"code": "not_found", "message": f"无此路径：/{path}"})
                return
            # health 不需要身份；其余端点统一鉴权
            if parts == ["health"]:
                party = None
            else:
                party = self._party()
            result = handler(party, body, parts)
            if result is None:
                result = {"ok": True}
            status, payload = result
            self._send(status, payload)
        except DomainError as exc:
            self._send(exc.http_status, {"code": exc.code, "message": str(exc)})
        except (KeyError, ValueError) as exc:
            self._send(HTTPStatus.UNPROCESSABLE_ENTITY, {"code": "validation_error", "message": str(exc)})

    def _route(self, method: str, parts: list[str]) -> Optional[Callable]:
        svc = self.service
        routes: dict[tuple[str, tuple[str, ...]], Callable] = {
            ("GET", ("health",)): lambda p, b, x: (200, {"status": "ok"}),
            ("GET", ("commitments",)): self._list_commitments,
            ("POST", ("commitments",)): self._create_commitment,
            ("POST", ("commitments", None, "agree")): self._agree_commitment,
            ("POST", ("commitments", None, "revisions")): self._revise_commitment,
            ("POST", ("commitments", None, "advances")): self._pay_advance,
            ("GET", ("commitments", None, "settlement")): self._get_settlement,
            ("POST", ("commitments", None, "close")): self._close_settlement,
            ("POST", ("deliveries",)): self._register_delivery,
            ("GET", ("deliveries", None)): self._delivery_statement,
            ("POST", ("deliveries", None, "samples")): self._seal_sample,
            ("POST", ("deliveries", None, "dispositions")): self._record_disposition,
            ("POST", ("samples", None, "gradings")): self._grade_sample,
            ("POST", ("gradings", None, "corrections")): self._correct_grade,
            ("POST", ("fm-events",)): self._record_fm,
            ("POST", ("negotiations",)): self._open_negotiation,
            ("POST", ("negotiations", None, "resolution")): self._resolve_negotiation,
            ("POST", ("appeals",)): self._open_appeal,
            ("POST", ("appeals", None, "resolution")): self._resolve_appeal,
            ("POST", ("consumptions",)): self._record_consumption,
            ("GET", ("products", None, "trace")): self._trace_product,
        }
        # 精确匹配优先，其次带一个路径参数的模板
        if (method, tuple(parts)) in routes:
            return routes[(method, tuple(parts))]
        for (m, pattern), fn in routes.items():
            if m != method or len(pattern) != len(parts):
                continue
            if all(wildcard is None or wildcard == part for wildcard, part in zip(pattern, parts)):
                return fn
        return None

    # ================================================================
    # 各端点
    # ================================================================

    @staticmethod
    def _require_type(party: Party, type_value: str) -> None:
        if party is None or party.type.value != type_value:
            from .errors import PermissionDenied

            raise PermissionDenied(f"该操作要求身份 {type_value}")

    def _list_commitments(self, party: Party, body: dict, parts: list):
        if party.type.value == "coop":
            items = list(self.store.commitments.values())
        elif party.type.value == "workshop":
            items = [c for c in self.store.commitments.values() if c.workshop_id == party.id]
        else:
            items = [c for c in self.store.commitments.values() if c.farmer_id == party.id]
        return 200, {"commitments": [self._commitment_summary(c) for c in items]}

    @staticmethod
    def _commitment_summary(c) -> dict:
        cur = c.current
        return {
            "id": c.id,
            "farmer_id": c.farmer_id,
            "workshop_id": c.workshop_id,
            "batch_id": c.batch_id,
            "crop": c.crop.value,
            "status": c.status.value,
            "settled": c.settled,
            "version": c.version,
            "current_version_no": cur.version_no,
            "floor_quantity_kg": str(cur.floor_quantity_kg),
            "agreed": cur.status.value == "agreed",
        }

    def _create_commitment(self, party: Party, body: dict, parts: list):
        self._require_type(party, "workshop")
        if party.id != body["workshop_id"]:
            from .errors import PermissionDenied

            raise PermissionDenied("只能以本工坊名义创建承诺")
        commitment = self.service.create_commitment(
            commitment_id=body["id"],
            farmer_id=body["farmer_id"],
            workshop_id=body["workshop_id"],
            batch_id=body["batch_id"],
            crop=body["crop"],
            floor_quantity_kg=_decimal(body, "floor_quantity_kg"),
            terms=_terms(body),
            created_by=party.id,
        )
        return 201, to_dict(commitment)

    def _agree_commitment(self, party: Party, body: dict, parts: list):
        self._require_type(party, "farmer")
        c = self.service.agree_commitment(
            parts[1], party, body.get("expected_version")
        )
        return 200, self._commitment_summary(c)

    def _revise_commitment(self, party: Party, body: dict, parts: list):
        self._require_type(party, "workshop")
        self.store.require_workshop_relation(parts[1], party)
        c = self.service.revise_commitment(
            commitment_id=parts[1],
            terms=_terms(body),
            created_by=party.id,
            change_summary=body.get("change_summary", ""),
            expected_version=body.get("expected_version"),
            floor_quantity_kg=(
                _decimal(body, "floor_quantity_kg") if "floor_quantity_kg" in body else None
            ),
        )
        return 200, to_dict(c)

    def _pay_advance(self, party: Party, body: dict, parts: list):
        self._require_type(party, "workshop")
        a = self.service.pay_advance(
            commitment_id=parts[1],
            amount=_decimal(body, "amount"),
            workshop=party,
            note=body.get("note", ""),
            advance_id=body.get("id"),
        )
        return 201, to_dict(a)

    def _register_delivery(self, party: Party, body: dict, parts: list):
        self._require_type(party, "workshop")
        d = self.service.register_delivery(
            commitment_id=body["commitment_id"],
            station_id=body["station_id"],
            gross_kg=_decimal(body, "gross_kg"),
            tare_kg=_decimal(body, "tare_kg"),
            weighed_by=party.id,
            ticket_no=body.get("ticket_no", ""),
            delivery_id=body.get("id"),
        )
        return 201, to_dict(d)

    def _delivery_statement(self, party: Party, body: dict, parts: list):
        return 200, self.service.delivery_statement(parts[1], party)

    def _seal_sample(self, party: Party, body: dict, parts: list):
        self._require_type(party, "workshop")
        s = self.service.seal_sample(
            delivery_id=parts[1],
            sealed_code=body["sealed_code"],
            sampled_by=party.id,
            sample_id=body.get("id"),
        )
        return 201, to_dict(s)

    def _grade_sample(self, party: Party, body: dict, parts: list):
        self._require_type(party, "workshop")
        g = self.service.grade_sample(
            sample_id=parts[1],
            grade=body["grade"],
            moisture_pct=_decimal(body, "moisture_pct"),
            impurity_pct=_decimal(body, "impurity_pct"),
            graded_by=party.id,
            is_lab_result=bool(body.get("is_lab_result", False)),
            note=body.get("note", ""),
        )
        return 201, to_dict(g)

    def _correct_grade(self, party: Party, body: dict, parts: list):
        self._require_type(party, "workshop")
        g = self.service.correct_grade(
            previous_grading_id=parts[1],
            new_grade=body["grade"],
            new_moisture_pct=_decimal(body, "moisture_pct"),
            new_impurity_pct=_decimal(body, "impurity_pct"),
            corrected_by=party.id,
            note=body.get("note", ""),
        )
        return 201, to_dict(g)

    def _record_disposition(self, party: Party, body: dict, parts: list):
        self._require_type(party, "workshop")
        deductions = [
            Deduction(
                reason_code=d["reason_code"],
                amount=Decimal(str(d["amount"])),
                note=d.get("note", ""),
            )
            for d in body.get("deductions", [])
        ]
        d = self.service.record_disposition(
            delivery_id=parts[1],
            kind=body["kind"],
            quantity_kg=_decimal(body, "quantity_kg"),
            actor=party,
            grade=body.get("grade"),
            deductions=deductions,
            note=body.get("note", ""),
            alternate_destination=body.get("alternate_destination", ""),
            farmer_consent=bool(body.get("farmer_consent", False)),
            disposition_id=body.get("id"),
            expected_delivery_version=body.get("expected_version"),
        )
        return 201, to_dict(d)

    def _record_fm(self, party: Party, body: dict, parts: list):
        self._require_type(party, "coop")
        e = self.service.record_fm_event(
            event_id=body["id"],
            type_=body["type"],
            title=body["title"],
            started_at=date.fromisoformat(body["started_at"]),
            ended_at=date.fromisoformat(body["ended_at"]) if body.get("ended_at") else None,
            region=body["region"],
            evidence_refs=body.get("evidence_refs", []),
            recorded_by=party.id,
        )
        return 201, to_dict(e)

    def _open_negotiation(self, party: Party, body: dict, parts: list):
        # 农户或工坊均可发起；服务层校验其属于该采购关系
        if party.type.value not in ("farmer", "workshop"):
            from .errors import PermissionDenied

            raise PermissionDenied("只有采购关系双方可以发起协商")
        n = self.service.open_negotiation(
            negotiation_id=body["id"],
            commitment_id=body["commitment_id"],
            fm_event_id=body["fm_event_id"],
            opened_by=party,
            evidence_refs=body.get("evidence_refs", []),
            shortfall_total_kg=_decimal(body, "shortfall_total_kg"),
        )
        return 201, to_dict(n)

    def _resolve_negotiation(self, party: Party, body: dict, parts: list):
        self._require_type(party, "coop")
        n = self.service.resolve_negotiation(
            negotiation_id=parts[1],
            fm_quantity_kg=_decimal(body, "fm_quantity_kg"),
            nonperformance_quantity_kg=_decimal(body, "nonperformance_quantity_kg"),
            resolved_by=party,
            farmer_agreed=bool(body.get("farmer_agreed", False)),
            coop_witness=body.get("coop_witness", ""),
            advance_recovery_amount=_decimal(body, "advance_recovery_amount", "0"),
            advance_relief_amount=_decimal(body, "advance_relief_amount", "0"),
            cost_share_amount=_decimal(body, "cost_share_amount", "0"),
            resolution_note=body.get("resolution_note", ""),
        )
        return 200, to_dict(n)

    def _get_settlement(self, party: Party, body: dict, parts: list):
        s = self.service.compute_settlement(parts[1], party)
        return 200, to_dict(s)

    def _close_settlement(self, party: Party, body: dict, parts: list):
        self._require_type(party, "coop")
        s = self.service.close_settlement(parts[1], party)
        return 200, to_dict(s)

    def _open_appeal(self, party: Party, body: dict, parts: list):
        self._require_type(party, "farmer")
        a = self.service.open_appeal(
            appeal_id=body["id"],
            commitment_id=body["commitment_id"],
            farmer=party,
            target_type=body["target_type"],
            target_ref=body["target_ref"],
            reason=body["reason"],
            evidence_refs=body.get("evidence_refs"),
        )
        return 201, to_dict(a)

    def _resolve_appeal(self, party: Party, body: dict, parts: list):
        self._require_type(party, "coop")
        a = self.service.resolve_appeal(
            appeal_id=parts[1],
            coop=party,
            uphold=body["uphold"],
            resolution_note=body["resolution_note"],
            adjustment_amount=_decimal(body, "adjustment_amount", "0"),
        )
        return 200, to_dict(a)

    def _record_consumption(self, party: Party, body: dict, parts: list):
        self._require_type(party, "workshop")
        lines = [
            (line["batch_id"], line.get("delivery_id"), Decimal(str(line["quantity_kg"])))
            for line in body["lines"]
        ]
        c = self.service.record_consumption(
            consumption_id=body["id"],
            product_lot=body["product_lot"],
            lines=lines,
            workshop=party,
        )
        return 201, to_dict(c)

    def _trace_product(self, party: Party, body: dict, parts: list):
        return 200, {"product_lot": parts[1], "trace": self.service.trace_product(parts[1], party)}


# ---------------------------------------------------------------- 服务器


def build_server(store: Store, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    service = SettlementService(store)

    class Handler(_Handler):
        pass

    Handler.service = service
    Handler.store = store
    server = ThreadingHTTPServer((host, port), Handler)
    return server


def serve_in_thread(store: Store, host: str = "127.0.0.1", port: int = 0):
    """返回 ``(server, thread, base_url)``，供测试与脚本使用。"""

    server = build_server(store, host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    return server, thread, base_url

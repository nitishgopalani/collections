"""In-memory simulated tool backend for Sprint 3 (TOOLS_MODE=simulate)."""

import copy
import json
import logging
import re
import uuid
from typing import Any

from app.clients.tools_fixtures import BORROWER_FIXTURES
from app.exceptions import ToolInvocationError

logger = logging.getLogger(__name__)

READ_TOOLS = frozenset({"check_last_payment", "get_balance", "get_borrower", "verify_identity"})
WRITE_TOOLS = frozenset(
    {
        "create_payment_link",
        "send_payment_link",
        "raise_dispute_ticket",
        "schedule_followup",
        "log_disposition",
    }
)


class FakeToolClient:
    """ToolClient backed by mutable borrower fixtures + idempotency cache."""

    def __init__(self) -> None:
        self._borrowers: dict[str, dict[str, Any]] = {
            borrower_id: copy.deepcopy(data) for borrower_id, data in BORROWER_FIXTURES.items()
        }
        self._idempotency: dict[str, dict[str, Any]] = {}
        self._write_effects: dict[str, int] = {}

    @property
    def is_stub(self) -> bool:
        return False

    @property
    def mode(self) -> str:
        return "simulate"

    def reset(self) -> None:
        self._borrowers = {
            borrower_id: copy.deepcopy(data) for borrower_id, data in BORROWER_FIXTURES.items()
        }
        self._idempotency.clear()
        self._write_effects.clear()

    async def ping(self) -> bool:
        return True

    def write_effect_count(self, tool: str) -> int:
        return self._write_effects.get(tool, 0)

    async def invoke(
        self,
        tool: str,
        args: dict[str, Any],
        tenant_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _ = tenant_id
        if idempotency_key and idempotency_key in self._idempotency:
            return copy.deepcopy(self._idempotency[idempotency_key])

        if args.get("__simulate_error"):
            raise ToolInvocationError(f"simulated failure for tool {tool}")

        borrower_id = str(args.get("borrower_id", ""))
        record = self._borrowers.get(borrower_id)
        if record is None:
            result_body: dict[str, Any] = {"error": "borrower_not_found"}
            if tool in READ_TOOLS:
                result_body["found"] = False
            if tool == "raise_dispute_ticket":
                result_body["dispute_logged"] = False
            response = {"ok": True, "result": result_body}
            if idempotency_key and tool in WRITE_TOOLS:
                self._idempotency[idempotency_key] = copy.deepcopy(response)
            return response

        if record.get("simulate_errors"):
            raise ToolInvocationError(f"borrower {borrower_id} flagged for simulated errors")

        if tool in WRITE_TOOLS:
            result = self._invoke_write(tool, args, record)
            response = {"ok": True, "result": result}
            if idempotency_key:
                self._idempotency[idempotency_key] = copy.deepcopy(response)
            self._write_effects[tool] = self._write_effects.get(tool, 0) + 1
            return response

        if tool in READ_TOOLS:
            result = self._invoke_read(tool, args, record)
            return {"ok": True, "result": result}

        response = {"ok": False, "error": f"unknown_tool:{tool}"}
        return response

    def _invoke_read(
        self,
        tool: str,
        args: dict[str, Any],
        record: dict[str, Any],
    ) -> dict[str, Any]:
        if tool == "check_last_payment":
            payments = record.get("payments") or []
            if payments:
                last = payments[-1]
                return {
                    "found": True,
                    "payment_id": last.get("payment_id"),
                    "amount": last.get("amount"),
                    "date": last.get("date"),
                    "status": last.get("status"),
                    "nach_status": last.get("nach_status") or record.get("nach_status"),
                    "nach_failure_side": last.get("nach_failure_side")
                    or record.get("nach_failure_side"),
                }
            return {
                "found": False,
                "nach_status": record.get("nach_status"),
                "nach_failure_side": record.get("nach_failure_side"),
            }

        if tool == "get_balance":
            return {
                "loan_id": record.get("loan_id"),
                "amount_due": record.get("amount_due"),
                "dpd": record.get("dpd"),
                "bucket": record.get("bucket"),
                "principal": record.get("principal"),
                "interest": record.get("interest"),
                "charges": record.get("charges"),
                "due_date": record.get("due_date"),
                "loan_status": record.get("loan_status", "active"),
                "nach_status": record.get("nach_status"),
                "nach_failure_side": record.get("nach_failure_side"),
            }

        if tool == "get_borrower":
            return {
                "borrower_id": args.get("borrower_id"),
                "loan_id": record.get("loan_id"),
                "amount_due": record.get("amount_due"),
                "dpd": record.get("dpd"),
                "bucket": record.get("bucket"),
                "vulnerable": record.get("vulnerable", False),
                "due_date": record.get("due_date"),
                "loan_tenure_months": record.get("loan_tenure_months"),
                "interest_rate_pct": record.get("interest_rate_pct"),
            }

        if tool == "verify_identity":
            return self._verify_identity(args, record)

        return {}

    def _verify_identity(
        self,
        args: dict[str, Any],
        record: dict[str, Any],
    ) -> dict[str, Any]:
        identity = record.get("identity") or {}
        response = str(args.get("identity_response") or "").lower().strip()
        normalized = re.sub(r"[\s\-/]", "", response)

        last4 = str(identity.get("last4") or "")
        dob = str(identity.get("dob") or "")
        name = str(identity.get("name") or "").lower()

        verified = False
        if last4 and last4 in normalized:
            verified = True
        if dob:
            dob_compact = dob.replace("-", "")
            if dob_compact in normalized or dob in response:
                verified = True
        if name and name in response:
            verified = True

        return {"identity_verified": verified}

    def _invoke_write(
        self,
        tool: str,
        args: dict[str, Any],
        record: dict[str, Any],
    ) -> dict[str, Any]:
        if tool == "create_payment_link":
            link_id = uuid.uuid4().hex[:12]
            rail = str(args.get("rail") or "default")
            link = f"https://pay.sim.example/{rail}/{link_id}"
            record.setdefault("payment_links", []).append(link)
            return {"payment_link": link, "link_id": link_id, "rail": rail}

        if tool == "send_payment_link":
            call_id = str(args.get("call_id") or "unknown")
            phone = str(args.get("to") or args.get("phone") or "unknown")
            amount = args.get("amount") or 350
            link = f"https://pay.example/test/{call_id}"
            payload = {
                "tool": "send_payment_link",
                "status": "SIMULATED",
                "to": phone,
                "amount": amount,
                "link": link,
            }
            logger.info(json.dumps(payload, ensure_ascii=False))
            record.setdefault("payment_links", []).append(
                {"link": link, "amount": amount, "channel": "whatsapp", "status": "SIMULATED"}
            )
            # TOOLS_MODE=live would invoke the real WhatsApp sender here.
            return {
                "payment_link": link,
                "link": link,
                "status": "SIMULATED",
                "channel": "whatsapp",
            }

        if tool == "raise_dispute_ticket":
            reason = args.get("reason")
            if not reason:
                return {"ticket_id": None, "dispute_logged": False}
            ticket_id = f"DISP-{uuid.uuid4().hex[:8].upper()}"
            record["dispute_open"] = True
            record["dispute_reason"] = args.get("reason")
            return {"ticket_id": ticket_id, "dispute_logged": True}

        if tool == "schedule_followup":
            record["followup_scheduled"] = True
            return {"scheduled": True, "followup_date": args.get("followup_date")}

        if tool == "log_disposition":
            disposition = args.get("disposition")
            record["disposition"] = disposition
            return {"logged": True, "disposition": disposition}

        return {}

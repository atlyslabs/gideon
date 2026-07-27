"""
Ramp Tools — interact with Ramp finance API for expense management.

Direct REST API calls to api.ramp.com/developer/v1/.
"""

from typing import Any

import httpx
import structlog

from tools.base import BaseTool

logger = structlog.get_logger()

RAMP_API_BASE = "https://api.ramp.com/developer/v1"


async def _ramp_get(access_token: str, path: str, params: dict | None = None) -> dict:
    """Make a GET request to Ramp API."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"{RAMP_API_BASE}{path}",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params=params,
        )

    if response.status_code == 401:
        raise Exception("401: Ramp token expired or invalid")

    if response.status_code != 200:
        logger.error("Ramp API error", status=response.status_code, path=path, body=response.text[:500])
        return {"error": f"Ramp API error: {response.status_code}"}

    return response.json()


def _format_amount(amount_data: dict | None) -> str:
    """Format Ramp amount object (amount in cents + currency_code)."""
    if not amount_data:
        return ""
    cents = amount_data.get("amount", 0)
    currency = amount_data.get("currency_code", "USD")
    return f"{cents / 100:.2f} {currency}"


class ListRampTransactions(BaseTool):
    @property
    def name(self) -> str:
        return "list_ramp_transactions"

    @property
    def description(self) -> str:
        return "List recent transactions/expenses from Ramp. Returns merchant, amount, date, category, and memo for each transaction."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "from_date": {
                    "type": "string",
                    "description": "Filter transactions from this date (ISO 8601, e.g. 2026-01-01T00:00:00Z). Optional.",
                },
                "to_date": {
                    "type": "string",
                    "description": "Filter transactions up to this date (ISO 8601). Optional.",
                },
            },
        }

    @property
    def provider(self) -> str:
        return "ramp"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        query: dict[str, str] = {}
        if params.get("from_date"):
            query["from_date"] = params["from_date"]
        if params.get("to_date"):
            query["to_date"] = params["to_date"]

        data = await _ramp_get(access_token, "/transactions", query or None)
        if "error" in data:
            return data

        transactions = data.get("data", [])
        results = []
        for t in transactions[:50]:
            results.append({
                "id": t.get("id", ""),
                "merchant": t.get("merchant_descriptor", "") or t.get("merchant_name", ""),
                "amount": _format_amount(t.get("amount")),
                "date": t.get("user_transaction_time", ""),
                "category": t.get("sk_category_name", ""),
                "memo": t.get("memo", ""),
                "card_holder": t.get("card_holder", {}).get("first_name", "") + " " + t.get("card_holder", {}).get("last_name", ""),
                "state": t.get("state", ""),
            })

        return {"transactions": results, "count": len(results), "total": len(transactions)}


class ListRampBills(BaseTool):
    @property
    def name(self) -> str:
        return "list_ramp_bills"

    @property
    def description(self) -> str:
        return "List bills and payments from Ramp. Returns vendor, amount, status, and due date."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    @property
    def provider(self) -> str:
        return "ramp"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        data = await _ramp_get(access_token, "/bills")
        if "error" in data:
            return data

        bills = data.get("data", [])
        results = []
        for b in bills[:30]:
            results.append({
                "id": b.get("id", ""),
                "vendor": b.get("vendor_name", ""),
                "amount": _format_amount(b.get("amount")),
                "status": b.get("payment_status", {}).get("type", "") if isinstance(b.get("payment_status"), dict) else b.get("status", ""),
                "due_date": b.get("due_at", ""),
                "invoice_number": b.get("invoice_number", ""),
            })

        return {"bills": results, "count": len(results)}


class ListRampReimbursements(BaseTool):
    @property
    def name(self) -> str:
        return "list_ramp_reimbursements"

    @property
    def description(self) -> str:
        return "List reimbursement requests from Ramp. Returns amount, status, merchant, and requester."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    @property
    def provider(self) -> str:
        return "ramp"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        data = await _ramp_get(access_token, "/reimbursements")
        if "error" in data:
            return data

        reimbursements = data.get("data", [])
        results = []
        for r in reimbursements[:30]:
            results.append({
                "id": r.get("id", ""),
                "amount": _format_amount(r.get("amount")),
                "merchant": r.get("merchant", ""),
                "status": r.get("status", ""),
                "user": r.get("user", {}).get("first_name", "") + " " + r.get("user", {}).get("last_name", "") if isinstance(r.get("user"), dict) else "",
                "created_at": r.get("created_at", ""),
            })

        return {"reimbursements": results, "count": len(results)}


class ListRampUsers(BaseTool):
    @property
    def name(self) -> str:
        return "list_ramp_users"

    @property
    def description(self) -> str:
        return "List team members on Ramp with their roles and status."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    @property
    def provider(self) -> str:
        return "ramp"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        data = await _ramp_get(access_token, "/users")
        if "error" in data:
            return data

        users = data.get("data", [])
        results = []
        for u in users[:50]:
            results.append({
                "id": u.get("id", ""),
                "name": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(),
                "email": u.get("email", ""),
                "role": u.get("role", ""),
                "department": u.get("department_name", ""),
                "status": u.get("status", ""),
            })

        return {"users": results, "count": len(results)}


class GetRampBalance(BaseTool):
    @property
    def name(self) -> str:
        return "get_ramp_balance"

    @property
    def description(self) -> str:
        return "Get the company's Ramp balance information including card limits, available balance, and statement data."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    @property
    def provider(self) -> str:
        return "ramp"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        data = await _ramp_get(access_token, "/business/balance")
        if "error" in data:
            return data
        return {"balance": data, "source": "ramp_api"}


class ListRampDepartments(BaseTool):
    @property
    def name(self) -> str:
        return "list_ramp_departments"

    @property
    def description(self) -> str:
        return "List departments on Ramp."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    @property
    def provider(self) -> str:
        return "ramp"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        data = await _ramp_get(access_token, "/departments")
        if "error" in data:
            return data

        departments = data.get("data", [])
        results = []
        for d in departments:
            results.append({
                "id": d.get("id", ""),
                "name": d.get("name", ""),
            })

        return {"departments": results, "count": len(results)}

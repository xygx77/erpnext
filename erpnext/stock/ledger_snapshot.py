"""Golden-master snapshot harness for ledger characterization tests.

Captures the General Ledger *and* Stock Ledger entries produced by a submitted
voucher in a normalized, deterministic form and compares them against a stored
golden snapshot. Volatile fields (name, creation, voucher number, serial/batch
bundle id) are stripped so the snapshot is stable across runs.

This is the Phase 0 safety net for the stock_controller refactor: every later
phase must keep these snapshots byte-identical. Regenerate goldens with::

    REGEN_LEDGER_SNAPSHOTS=1 bench run-tests --site test-erpnext-v17 \\
        --module erpnext.stock.test_ledger_characterization
"""

import json
import os
from pathlib import Path

import frappe
from frappe.utils import flt

SNAPSHOT_DIR = Path(__file__).parent / "ledger_snapshots"
REGEN_ENV = "REGEN_LEDGER_SNAPSHOTS"
GL_PRECISION = 2
QTY_PRECISION = 6
RATE_PRECISION = 4


class GLSnapshot:
	"""Normalized, order-stable view of a voucher's GL entries."""

	def __init__(self, voucher_type: str, voucher_no: str) -> None:
		self.voucher_type = voucher_type
		self.voucher_no = voucher_no

	def capture(self) -> list[dict]:
		rows = [self._normalize(row) for row in self._fetch_rows()]
		# Sort on the full normalized row so ordering never depends on the DB's
		# return order.
		return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))

	def _fetch_rows(self) -> list[dict]:
		gl = frappe.qb.DocType("GL Entry")
		query = (
			frappe.qb.from_(gl)
			.select(
				gl.account,
				gl.party_type,
				gl.party,
				gl.debit,
				gl.credit,
				gl.debit_in_account_currency,
				gl.credit_in_account_currency,
				gl.account_currency,
				gl.against,
				gl.cost_center,
				gl.is_opening,
				gl.posting_date,
			)
			.where(
				(gl.voucher_type == self.voucher_type)
				& (gl.voucher_no == self.voucher_no)
				& (gl.is_cancelled == 0)
			)
			.orderby(gl.account, gl.party, gl.debit, gl.credit)
		)
		return query.run(as_dict=True)

	def _normalize(self, row: dict) -> dict:
		return {
			"account": row.account,
			"party_type": row.party_type or None,
			"party": row.party or None,
			"debit": flt(row.debit, GL_PRECISION),
			"credit": flt(row.credit, GL_PRECISION),
			"debit_in_account_currency": flt(row.debit_in_account_currency, GL_PRECISION),
			"credit_in_account_currency": flt(row.credit_in_account_currency, GL_PRECISION),
			"account_currency": row.account_currency,
			"against": self._normalize_against(row.against),
			"cost_center": row.cost_center,
			"is_opening": row.is_opening,
			"posting_date": str(row.posting_date),
		}

	def _normalize_against(self, against: str | None) -> str | None:
		"""`against` is a comma-joined account list whose order is not stable."""
		if not against:
			return None
		return ", ".join(sorted(part.strip() for part in against.split(",")))


class SLSnapshot:
	"""Normalized, order-stable view of a voucher's Stock Ledger entries."""

	def __init__(self, voucher_type: str, voucher_no: str) -> None:
		self.voucher_type = voucher_type
		self.voucher_no = voucher_no

	def capture(self) -> list[dict]:
		rows = [self._normalize(row) for row in self._fetch_rows()]
		return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))

	def _fetch_rows(self) -> list[dict]:
		sle = frappe.qb.DocType("Stock Ledger Entry")
		query = (
			frappe.qb.from_(sle)
			.select(
				sle.item_code,
				sle.warehouse,
				sle.stock_uom,
				sle.actual_qty,
				sle.qty_after_transaction,
				sle.incoming_rate,
				sle.valuation_rate,
				sle.stock_value,
				sle.stock_value_difference,
				sle.posting_date,
			)
			.where(
				(sle.voucher_type == self.voucher_type)
				& (sle.voucher_no == self.voucher_no)
				& (sle.is_cancelled == 0)
			)
			.orderby(sle.item_code, sle.warehouse, sle.actual_qty)
		)
		return query.run(as_dict=True)

	def _normalize(self, row: dict) -> dict:
		return {
			"item_code": row.item_code,
			"warehouse": row.warehouse,
			"stock_uom": row.stock_uom,
			"actual_qty": flt(row.actual_qty, QTY_PRECISION),
			"qty_after_transaction": flt(row.qty_after_transaction, QTY_PRECISION),
			"incoming_rate": flt(row.incoming_rate, RATE_PRECISION),
			"valuation_rate": flt(row.valuation_rate, RATE_PRECISION),
			"stock_value": flt(row.stock_value, RATE_PRECISION),
			"stock_value_difference": flt(row.stock_value_difference, RATE_PRECISION),
			"posting_date": str(row.posting_date),
		}


def capture_ledger_snapshot(voucher_type: str, voucher_no: str) -> dict:
	"""Combined GL + SLE snapshot for a single voucher."""
	return {
		"gl": GLSnapshot(voucher_type, voucher_no).capture(),
		"sle": SLSnapshot(voucher_type, voucher_no).capture(),
	}


def assert_ledger_snapshot(test_case, name: str, voucher_type: str, voucher_no: str) -> None:
	"""Compare a voucher's GL + SLE entries against the golden snapshot ``name``.

	In regen mode (``REGEN_LEDGER_SNAPSHOTS`` set) the golden file is written
	instead of asserted, so the same scenarios both produce and verify the goldens.
	"""
	actual = capture_ledger_snapshot(voucher_type, voucher_no)
	path = SNAPSHOT_DIR / f"{name}.json"

	if os.environ.get(REGEN_ENV):
		SNAPSHOT_DIR.mkdir(exist_ok=True)
		path.write_text(json.dumps(actual, indent="\t", sort_keys=True) + "\n")
		return

	test_case.assertTrue(
		path.exists(),
		f"Golden snapshot {path} missing. Run with {REGEN_ENV}=1 to create it.",
	)
	expected = json.loads(path.read_text())
	test_case.assertEqual(expected, actual, f"Ledger snapshot mismatch for '{name}'")

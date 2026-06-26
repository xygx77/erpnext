# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.report.stock_ledger_invariant_check.stock_ledger_invariant_check import execute
from erpnext.tests.utils import ERPNextTestSuite

WAREHOUSE = "Stores - _TC"
COMPANY = "_Test Company"
ITEM = "_Test Item"


class TestStockLedgerInvariantCheck(ERPNextTestSuite):
	def run_report(self, **extra):
		filters = frappe._dict({"company": COMPANY, "warehouse": WAREHOUSE})
		filters.update(extra)
		return execute(filters)[1]

	def make_movements(self) -> str:
		frappe.db.set_value("Item", ITEM, "valuation_method", "FIFO")
		make_stock_entry(item_code=ITEM, to_warehouse=WAREHOUSE, qty=10, rate=100, posting_date="2026-06-01")
		make_stock_entry(item_code=ITEM, to_warehouse=WAREHOUSE, qty=5, rate=120, posting_date="2026-06-02")
		make_stock_entry(item_code=ITEM, from_warehouse=WAREHOUSE, qty=4, rate=0, posting_date="2026-06-03")
		return ITEM

	def test_diagnostic_rows_have_no_discrepancy(self):
		item = self.make_movements()

		data = self.run_report(item_code=item)

		self.assertEqual(len(data), 3)
		for row in data:
			self.assertLess(abs(row.difference_in_qty), 0.01)
			self.assertLess(abs(row.fifo_qty_diff), 0.01)
			self.assertLess(abs(row.diff_value_diff), 0.01)

	def test_running_balance_matches(self):
		item = self.make_movements()

		data = self.run_report(item_code=item)

		self.assertEqual(data[-1].qty_after_transaction, 11)

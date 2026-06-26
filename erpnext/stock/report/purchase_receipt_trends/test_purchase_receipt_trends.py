# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.stock.doctype.purchase_receipt.test_purchase_receipt import make_purchase_receipt
from erpnext.stock.report.purchase_receipt_trends.purchase_receipt_trends import execute
from erpnext.tests.utils import ERPNextTestSuite


class TestPurchaseReceiptTrends(ERPNextTestSuite):
	def run_report(self, **extra):
		filters = frappe._dict(
			{
				"company": "_Test Company",
				"fiscal_year": "_Test Fiscal Year 2026",
				"period": "Yearly",
				"based_on": "Item",
				"group_by": "",
			}
		)
		filters.update(extra)
		return execute(filters)[1]

	def get_item_totals(self, data, item):
		# Row layout for based_on="Item", Yearly, no group_by:
		# [item_code, item_name, currency, FY(Qty), FY(Amt), Total(Qty), Total(Amt)]
		row = next((r for r in data if r[0] == item), None)
		if row is None:
			return 0, 0
		return row[3], row[4]

	def test_receipt_qty_in_trend(self):
		item = "_Test Item"

		# The report sums ALL purchase receipts for the item in the fiscal year, so capture
		# any pre-existing committed baseline and assert only this receipt's contribution.
		base_qty, base_amt = self.get_item_totals(self.run_report(), item)

		make_purchase_receipt(
			item_code=item, qty=10, rate=100, company="_Test Company", posting_date="2026-06-01"
		)

		qty, amt = self.get_item_totals(self.run_report(), item)
		self.assertEqual(qty - base_qty, 10)  # fiscal-year qty
		self.assertEqual(amt - base_amt, 1000)  # fiscal-year amount (10 * 100)

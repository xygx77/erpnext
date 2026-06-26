# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.stock.doctype.item.test_item import make_item
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

	def test_receipt_qty_in_trend(self):
		item = make_item(properties={"is_stock_item": 1, "is_purchase_item": 1}).name
		make_purchase_receipt(
			item_code=item, qty=10, rate=100, company="_Test Company", posting_date="2026-06-01"
		)

		data = self.run_report()

		# Row layout for based_on="Item", Yearly, no group_by:
		# [item_code, item_name, currency, FY(Qty), FY(Amt), Total(Qty), Total(Amt)]
		row = next(r for r in data if r[0] == item)
		self.assertEqual(row[3], 10)  # fiscal-year qty
		self.assertEqual(row[4], 1000)  # fiscal-year amount (10 * 100)
		self.assertEqual(row[5], 10)  # Total(Qty)
		self.assertEqual(row[6], 1000)  # Total(Amt)

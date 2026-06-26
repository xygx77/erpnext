# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.stock.doctype.purchase_receipt.test_purchase_receipt import make_purchase_receipt
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.report.item_prices.item_prices import execute
from erpnext.tests.utils import ERPNextTestSuite

# Positional columns returned by the report (it returns string-format columns,
# not dicts, so rows are plain lists indexed by position).
ITEM_CODE = 0
LAST_PURCHASE_RATE = 6
VALUATION_RATE = 7
SALES_PRICE_LIST = 8
PURCHASE_PRICE_LIST = 9


class TestItemPrices(ERPNextTestSuite):
	"""Correctness tests for the Item Prices report."""

	def run_report(self, **extra):
		filters = frappe._dict({"items": "Enabled Items only", **extra})
		return execute(filters)[1]

	def row_for(self, data, item_code):
		for row in data:
			if row[ITEM_CODE] == item_code:
				return row
		self.fail(f"No report row found for item {item_code}")

	def test_item_selling_price_listed(self):
		"""A Standard Selling Item Price shows up in the Sales Price List column."""
		item = "_Test Item"
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item,
				"price_list": "Standard Selling",
				"price_list_rate": 250,
			}
		).insert()

		row = self.row_for(self.run_report(), item)
		self.assertIn("250.0", row[SALES_PRICE_LIST])
		self.assertIn("Standard Selling", row[SALES_PRICE_LIST])
		# A selling price must not leak into the buying column.
		self.assertNotIn("250.0", row[PURCHASE_PRICE_LIST] or "")
		self.assertNotIn("Standard Selling", row[PURCHASE_PRICE_LIST] or "")

	def test_item_buying_price_listed(self):
		"""A Standard Buying Item Price shows up in the Purchase Price List column."""
		item = "_Test Item 2"
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item,
				"price_list": "Standard Buying",
				"price_list_rate": 175,
			}
		).insert()

		row = self.row_for(self.run_report(), item)
		self.assertIn("175.0", row[PURCHASE_PRICE_LIST])
		self.assertIn("Standard Buying", row[PURCHASE_PRICE_LIST])
		# A buying price must not leak into the selling column.
		self.assertNotIn("175.0", row[SALES_PRICE_LIST] or "")
		self.assertNotIn("Standard Buying", row[SALES_PRICE_LIST] or "")

	def test_last_purchase_rate_from_receipt(self):
		"""The latest purchase rate (from a Purchase Receipt) shows in the Last Purchase Rate column."""
		item = "_Test Item"
		make_purchase_receipt(
			item_code=item, qty=5, rate=500, company="_Test Company", posting_date="2026-06-01"
		)

		row = self.row_for(self.run_report(), item)
		self.assertEqual(row[LAST_PURCHASE_RATE], 500)

	def test_valuation_rate_from_stock(self):
		"""The Bin valuation rate shows in the Valuation Rate column."""
		# _Test FG Item has no opening-stock baseline, so its valuation reflects only this receipt
		item = "_Test FG Item"
		make_stock_entry(
			item_code=item, to_warehouse="Stores - _TC", qty=10, rate=250, posting_date="2026-06-01"
		)

		row = self.row_for(self.run_report(), item)
		self.assertEqual(row[VALUATION_RATE], 250)

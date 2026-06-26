# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.report.item_prices.item_prices import execute
from erpnext.tests.utils import ERPNextTestSuite

# Positional columns returned by the report (it returns string-format columns,
# not dicts, so rows are plain lists indexed by position).
ITEM_CODE = 0
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
		item = make_item(properties={"is_stock_item": 1}).name
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
		self.assertFalse(row[PURCHASE_PRICE_LIST])

	def test_item_buying_price_listed(self):
		"""A Standard Buying Item Price shows up in the Purchase Price List column."""
		item = make_item(properties={"is_stock_item": 1}).name
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
		self.assertFalse(row[SALES_PRICE_LIST])

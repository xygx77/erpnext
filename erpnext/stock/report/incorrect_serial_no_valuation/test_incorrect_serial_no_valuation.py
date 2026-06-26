# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.report.incorrect_serial_no_valuation.incorrect_serial_no_valuation import execute
from erpnext.tests.utils import ERPNextTestSuite


class TestIncorrectSerialNoValuation(ERPNextTestSuite):
	def run_report(self, **extra):
		filters = frappe._dict({"company": "_Test Company"})
		filters.update(extra)
		return execute(filters)[1]

	def make_serial_item(self):
		return make_item(
			properties={
				"is_stock_item": 1,
				"has_serial_no": 1,
				"serial_no_series": "ISV-.#####",
			}
		).name

	def test_healthy_serial_item_not_flagged(self):
		item = self.make_serial_item()

		make_stock_entry(
			item_code=item,
			to_warehouse="_Test Warehouse - _TC",
			qty=3,
			rate=100,
			posting_date="2026-06-01",
		)
		make_stock_entry(
			item_code=item,
			from_warehouse="_Test Warehouse - _TC",
			qty=1,
			posting_date="2026-06-02",
		)

		data = self.run_report(item_code=item)

		flagged_items = {row.get("item_code") for row in data if isinstance(row, dict)}
		self.assertNotIn(item, flagged_items)

	def test_only_balance_row_when_filtered_to_healthy_item(self):
		item = self.make_serial_item()

		make_stock_entry(
			item_code=item,
			to_warehouse="_Test Warehouse - _TC",
			qty=3,
			rate=100,
			posting_date="2026-06-01",
		)

		data = self.run_report(item_code=item)

		# The report always appends a single "Balance" summary row. A healthy
		# serial item contributes no detail rows, so only that summary remains.
		self.assertEqual(len(data), 1)
		self.assertEqual(data[-1].get("qty"), 0)
		self.assertEqual(data[-1].get("valuation_rate"), 0)

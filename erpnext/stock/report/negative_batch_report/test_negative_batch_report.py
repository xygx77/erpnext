# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.tests.utils import ERPNextTestSuite


class TestNegativeBatchReport(ERPNextTestSuite):
	def run_report(self, item_code):
		from erpnext.stock.report.negative_batch_report.negative_batch_report import execute

		return execute(
			frappe._dict(
				{
					"company": "_Test Company",
					"warehouse": "_Test Warehouse - _TC",
					"item_code": item_code,
				}
			)
		)[1]

	def test_healthy_batch_not_negative(self):
		from erpnext.stock.doctype.item.test_item import make_item
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		item = make_item(
			properties={
				"is_stock_item": 1,
				"has_batch_no": 1,
				"create_new_batch": 1,
				"batch_number_series": "NBR-.#####",
			}
		).name

		make_stock_entry(
			item_code=item,
			to_warehouse="_Test Warehouse - _TC",
			qty=10,
			rate=100,
			posting_date="2026-06-01",
		)
		make_stock_entry(
			item_code=item,
			from_warehouse="_Test Warehouse - _TC",
			qty=4,
			posting_date="2026-06-02",
		)

		data = self.run_report(item)

		# The batch was only received (10) before being issued (4), so its running
		# balance never goes negative; the report must not list this item's batch.
		self.assertFalse([row for row in data if row.get("item_code") == item])

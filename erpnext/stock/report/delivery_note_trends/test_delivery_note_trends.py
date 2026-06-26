# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.tests.utils import ERPNextTestSuite


class TestDeliveryNoteTrends(ERPNextTestSuite):
	def run_report(self, **extra):
		from erpnext.stock.report.delivery_note_trends.delivery_note_trends import execute

		filters = frappe._dict(
			{
				"company": "_Test Company",
				"fiscal_year": "_Test Fiscal Year 2026",
				"period": "Yearly",
				"based_on": "Item",
			}
		)
		filters.update(extra)
		return execute(filters)[1]

	def test_delivery_qty_in_trend(self):
		# based_on="Item" + period="Yearly": each row is
		# [item_code, item_name, currency, yearly_qty, yearly_amt, total_qty, total_amt].
		# A submitted Delivery Note of qty 5 @ rate 200 should sum to qty 5 / amount 1000
		# (base_net_amount) in both the yearly bucket and the Total columns.
		from erpnext.stock.doctype.delivery_note.test_delivery_note import create_delivery_note
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		item = "_Test Item"

		make_stock_entry(
			item_code=item,
			to_warehouse="Stores - _TC",
			qty=20,
			rate=100,
			posting_date="2026-06-01",
		)
		create_delivery_note(
			item_code=item,
			warehouse="Stores - _TC",
			qty=5,
			rate=200,
			customer="_Test Customer",
			company="_Test Company",
			posting_date="2026-06-01",
		)

		data = self.run_report()

		item_rows = [row for row in data if row[0] == item]
		self.assertEqual(len(item_rows), 1)

		row = item_rows[0]
		self.assertEqual(row[3], 5)  # yearly qty bucket
		self.assertEqual(row[4], 1000)  # yearly amount bucket (base_net_amount)
		self.assertEqual(row[5], 5)  # Total(Qty)
		self.assertEqual(row[6], 1000)  # Total(Amt)

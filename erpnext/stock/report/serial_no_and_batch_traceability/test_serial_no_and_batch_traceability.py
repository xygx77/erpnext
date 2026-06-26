# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.stock.doctype.delivery_note.test_delivery_note import create_delivery_note
from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.report.serial_no_and_batch_traceability.serial_no_and_batch_traceability import (
	execute,
)
from erpnext.tests.utils import ERPNextTestSuite


class TestSerialNoAndBatchTraceability(ERPNextTestSuite):
	def run_report(self, **extra):
		filters = frappe._dict({"company": "_Test Company"})
		filters.update(extra)
		return execute(filters)[1]

	def make_serial_item(self):
		return make_item(
			properties={
				"is_stock_item": 1,
				"has_serial_no": 1,
				"serial_no_series": "SNT-.#####",
			}
		).name

	def test_serial_movements_traced(self):
		"""Backward trace should surface the receipt voucher the serial came in through."""
		item = self.make_serial_item()
		receipt = make_stock_entry(
			item_code=item,
			to_warehouse="_Test Warehouse - _TC",
			qty=2,
			rate=100,
			posting_date="2026-06-01",
			company="_Test Company",
		)
		serial_no = frappe.get_all("Serial No", {"item_code": item}, pluck="name")[0]

		rows = self.run_report(
			item_code=item,
			serial_nos=[serial_no],
			traceability_direction="Backward",
		)

		traced = {row["reference_name"]: row for row in rows if row.get("reference_name")}
		self.assertIn(receipt.name, traced)

		receipt_row = traced[receipt.name]
		self.assertEqual(receipt_row["serial_no"], serial_no)
		self.assertEqual(receipt_row["item_code"], item)
		self.assertEqual(receipt_row["reference_doctype"], "Stock Entry")
		self.assertEqual(receipt_row["warehouse"], "_Test Warehouse - _TC")
		self.assertEqual(receipt_row["direction"], "Backward")
		self.assertGreater(receipt_row["qty"], 0)

	def test_forward_and_backward_directions(self):
		"""'Both' should trace backward to the receipt and forward to the outward delivery."""
		item = self.make_serial_item()
		receipt = make_stock_entry(
			item_code=item,
			to_warehouse="_Test Warehouse - _TC",
			qty=2,
			rate=100,
			posting_date="2026-06-01",
			company="_Test Company",
		)
		serial_no = frappe.get_all("Serial No", {"item_code": item}, pluck="name")[0]

		delivery_note = create_delivery_note(
			item_code=item,
			qty=1,
			serial_no=[serial_no],
			warehouse="_Test Warehouse - _TC",
			posting_date="2026-06-03",
			company="_Test Company",
		)

		rows = self.run_report(
			item_code=item,
			serial_nos=[serial_no],
			traceability_direction="Both",
		)

		traced = {row["reference_name"]: row for row in rows if row.get("reference_name")}

		self.assertIn(receipt.name, traced)
		self.assertEqual(traced[receipt.name]["direction"], "Backward")

		self.assertIn(delivery_note.name, traced)
		forward_row = traced[delivery_note.name]
		self.assertEqual(forward_row["reference_doctype"], "Delivery Note")
		self.assertEqual(forward_row["serial_no"], serial_no)
		self.assertEqual(forward_row["direction"], "Forward")
		self.assertEqual(forward_row["customer"], delivery_note.customer)
		self.assertLess(forward_row["qty"], 0)

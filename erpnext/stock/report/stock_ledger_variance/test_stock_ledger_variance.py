# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.tests.utils import ERPNextTestSuite


class TestStockLedgerVariance(ERPNextTestSuite):
	def run_report(self, **extra):
		from erpnext.stock.report.stock_ledger_variance.stock_ledger_variance import execute

		filters = {"company": "_Test Company"}
		filters.update(extra)

		return execute(frappe._dict(filters))[1]

	def test_healthy_stock_has_no_variance(self):
		from erpnext.stock.doctype.item.test_item import make_item
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		item = make_item(properties={"is_stock_item": 1, "valuation_method": "Moving Average"}).name

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

		# A clean receipt followed by a clean issue keeps the ledger consistent,
		# so the corruption detector must not flag any entry for this item.
		data = self.run_report(item_code=item)
		self.assertFalse([row for row in data if row.get("item_code") == item])

		qty_data = self.run_report(item_code=item, difference_in="Qty")
		self.assertFalse([row for row in qty_data if row.get("item_code") == item])

	def test_multiple_clean_movements_no_variance(self):
		from erpnext.stock.doctype.item.test_item import make_item
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		item = make_item(properties={"is_stock_item": 1, "valuation_method": "Moving Average"}).name

		make_stock_entry(
			item_code=item,
			to_warehouse="_Test Warehouse - _TC",
			qty=10,
			rate=100,
			posting_date="2026-06-01",
		)
		make_stock_entry(
			item_code=item,
			to_warehouse="_Test Warehouse - _TC",
			qty=5,
			rate=120,
			posting_date="2026-06-02",
		)
		make_stock_entry(
			item_code=item,
			to_warehouse="_Test Warehouse - _TC",
			qty=8,
			rate=90,
			posting_date="2026-06-03",
		)
		make_stock_entry(
			item_code=item,
			from_warehouse="_Test Warehouse - _TC",
			qty=6,
			posting_date="2026-06-04",
		)

		# Several receipts at different rates plus an issue still produce a
		# self-consistent ledger, so no variance rows are expected.
		data = self.run_report(item_code=item)
		self.assertFalse([row for row in data if row.get("item_code") == item])

# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe

from erpnext.stock.report.bom_search.bom_search import execute
from erpnext.tests.utils import ERPNextTestSuite


class TestBomSearch(ERPNextTestSuite):
	def run_report(self, **extra):
		filters = frappe._dict({"search_sub_assemblies": 0})
		filters.update(extra)
		return execute(filters)[1]

	def test_bom_found_by_contained_item(self):
		raw_material = "_Test Item"
		finished_good = "_Test FG Item"

		bom = frappe.get_doc(doctype="BOM", item=finished_good, company="_Test Company", currency="INR")
		bom.append("items", {"item_code": raw_material, "qty": 1})
		bom.insert()
		bom.submit()

		rows = self.run_report(item1=raw_material)
		bom_names = [row[0] for row in rows]
		self.assertIn(bom.name, bom_names)

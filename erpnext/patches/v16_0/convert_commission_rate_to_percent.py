import frappe
from frappe.utils import flt


def execute():
	"""Sanitize the free-text commission_rate values before the Data -> Percent column change.

	Sales Person and Sales Team stored ``commission_rate`` as Data (varchar). This runs in
	pre_model_sync so the values are clean numeric strings by the time the schema sync alters
	the column to Percent; empty or non-numeric values become 0.
	"""
	for doctype in ("Sales Person", "Sales Team"):
		if not frappe.db.has_column(doctype, "commission_rate"):
			continue

		# Percent maps to a NOT NULL decimal column, so empty/NULL/non-numeric text must become 0
		# as well, otherwise the column type change fails under strict SQL mode.
		rows = frappe.db.get_all(doctype, fields=["name", "commission_rate"])
		for row in rows:
			cleaned = flt(row.commission_rate)
			if str(row.commission_rate) != str(cleaned):
				frappe.db.set_value(doctype, row.name, "commission_rate", cleaned, update_modified=False)

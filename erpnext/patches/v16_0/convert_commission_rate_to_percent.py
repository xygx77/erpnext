import frappe
from frappe.utils import flt


def execute():
	"""Sanitize the free-text commission_rate values before the Data -> Percent column change.

	Sales Person and Sales Team stored ``commission_rate`` as Data (varchar). This runs in
	pre_model_sync so the values are clean numbers by the time the schema sync alters the column
	to Percent: a trailing percent sign (e.g. "20%" / "20 %") is stripped, and empty / NULL /
	non-numeric values become 0.
	"""
	for doctype in ("Sales Person", "Sales Team"):
		if not frappe.db.has_column(doctype, "commission_rate"):
			continue

		# Percent maps to a NOT NULL decimal column, so empty/NULL/non-numeric text must become 0
		# as well, otherwise the column type change fails under strict SQL mode.
		rows = frappe.db.get_all(doctype, fields=["name", "commission_rate"])
		for row in rows:
			cleaned = flt(_strip_percent_sign(row.commission_rate))
			if str(row.commission_rate) != str(cleaned):
				frappe.db.set_value(doctype, row.name, "commission_rate", cleaned, update_modified=False)


def _strip_percent_sign(value):
	"""Drop a trailing percent sign so "20%" / "20 %" parse as 20 instead of 0."""
	if isinstance(value, str):
		return value.replace("%", "").strip()
	return value

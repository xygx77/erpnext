# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.utils import flt

from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	get_depr_schedule,
)


class AssetService:
	"""Keeps Assets in sync with the Journal Entries that depreciate, dispose or
	adjust them.

	On submit of a Depreciation Entry it reduces the asset value and links the
	depreciation schedule; on submit of an Asset Disposal it marks the asset
	disposed. On cancel it reverses those links. It also guards cancellation of
	Journal Entries tied to asset scrapping or value adjustments.
	"""

	def __init__(self, doc):
		self.doc = doc

	def validate_depr_account_and_depr_entry_voucher_type(self):
		for d in self.doc.get("accounts"):
			if d.account_type == "Depreciation":
				if self.doc.voucher_type != "Depreciation Entry":
					frappe.throw(
						_("Journal Entry type should be set as Depreciation Entry for asset depreciation")
					)

				if frappe.get_cached_value("Account", d.account, "root_type") != "Expense":
					frappe.throw(_("Account {0} should be of type Expense").format(d.account))

	def has_asset_adjustment_entry(self):
		if self.doc.flags.get("via_asset_value_adjustment"):
			return

		asset_value_adjustment = frappe.db.get_value(
			"Asset Value Adjustment", {"docstatus": 1, "journal_entry": self.doc.name}, "name"
		)
		if asset_value_adjustment:
			frappe.throw(
				_(
					"Cannot cancel this document as it is linked with the submitted Asset Value Adjustment <b>{0}</b>. Please cancel the Asset Value Adjustment to continue."
				).format(frappe.utils.get_link_to_form("Asset Value Adjustment", asset_value_adjustment))
			)

	def update_asset_value(self):
		self.update_asset_on_depreciation()
		self.update_asset_on_disposal()

	def update_asset_on_depreciation(self):
		if self.doc.voucher_type != "Depreciation Entry":
			return

		for d in self.doc.get("accounts"):
			if (
				d.reference_type == "Asset"
				and d.reference_name
				and frappe.get_cached_value("Account", d.account, "root_type") == "Expense"
				and d.debit
			):
				asset = frappe.get_cached_doc("Asset", d.reference_name)

				if asset.calculate_depreciation:
					self.update_journal_entry_link_on_depr_schedule(asset, d)
					self.update_value_after_depreciation(asset, d.debit)

				asset.db_set("value_after_depreciation", asset.value_after_depreciation - d.debit)
				asset.set_status()
				asset.set_total_booked_depreciations()

	def update_value_after_depreciation(self, asset, depr_amount):
		fb_idx = 1
		if self.doc.finance_book:
			for fb_row in asset.get("finance_books"):
				if fb_row.finance_book == self.doc.finance_book:
					fb_idx = fb_row.idx
					break
		fb_row = asset.get("finance_books")[fb_idx - 1]
		fb_row.value_after_depreciation -= depr_amount
		frappe.db.set_value(
			"Asset Finance Book", fb_row.name, "value_after_depreciation", fb_row.value_after_depreciation
		)

	def update_journal_entry_link_on_depr_schedule(self, asset, je_row):
		depr_schedule = get_depr_schedule(asset.name, "Active", self.doc.finance_book)
		for d in depr_schedule or []:
			if (
				d.schedule_date == self.doc.posting_date
				and not d.journal_entry
				and d.depreciation_amount == flt(je_row.debit)
			):
				frappe.db.set_value("Depreciation Schedule", d.name, "journal_entry", self.doc.name)

	def update_asset_on_disposal(self):
		if self.doc.voucher_type == "Asset Disposal":
			disposed_assets = []
			for d in self.doc.get("accounts"):
				if (
					d.reference_type == "Asset"
					and d.reference_name
					and d.reference_name not in disposed_assets
				):
					frappe.db.set_value(
						"Asset",
						d.reference_name,
						{
							"disposal_date": self.doc.posting_date,
							"journal_entry_for_scrap": self.doc.name,
						},
					)
					asset_doc = frappe.get_doc("Asset", d.reference_name)
					asset_doc.set_status()
					disposed_assets.append(d.reference_name)

	def unlink_asset_reference(self):
		for d in self.doc.get("accounts"):
			if (
				self.doc.voucher_type == "Depreciation Entry"
				and d.reference_type == "Asset"
				and d.reference_name
				and frappe.get_cached_value("Account", d.account, "root_type") == "Expense"
				and d.debit
			):
				asset = frappe.get_doc("Asset", d.reference_name)

				if asset.calculate_depreciation:
					je_found = False

					for fb_row in asset.get("finance_books"):
						if je_found:
							break

						depr_schedule = get_depr_schedule(asset.name, "Active", fb_row.finance_book)

						for s in depr_schedule or []:
							if s.journal_entry == self.doc.name:
								s.db_set("journal_entry", None)

								fb_row.value_after_depreciation += d.debit
								fb_row.db_update()

								je_found = True
								break
					if not je_found:
						fb_idx = 1
						if self.doc.finance_book:
							for fb_row in asset.get("finance_books"):
								if fb_row.finance_book == self.doc.finance_book:
									fb_idx = fb_row.idx
									break

						fb_row = asset.get("finance_books")[fb_idx - 1]
						fb_row.value_after_depreciation += d.debit
						fb_row.db_update()
				asset.db_set("value_after_depreciation", asset.value_after_depreciation + d.debit)
				asset.set_status()
				asset.set_total_booked_depreciations()
			elif (
				self.doc.voucher_type == "Journal Entry" and d.reference_type == "Asset" and d.reference_name
			):
				journal_entry_for_scrap = frappe.db.get_value(
					"Asset", d.reference_name, "journal_entry_for_scrap"
				)

				if journal_entry_for_scrap == self.doc.name:
					frappe.throw(
						_("Journal Entry for Asset scrapping cannot be cancelled. Please restore the Asset.")
					)

	def unlink_asset_adjustment_entry(self):
		AssetValueAdjustment = frappe.qb.DocType("Asset Value Adjustment")
		(
			frappe.qb.update(AssetValueAdjustment)
			.set(AssetValueAdjustment.journal_entry, None)
			.where(AssetValueAdjustment.journal_entry == self.doc.name)
		).run()

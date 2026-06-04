# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import json
from collections import defaultdict

import frappe
from frappe import _, bold
from frappe.query_builder.functions import Sum
from frappe.utils import cint, cstr, flt, get_link_to_form, getdate

import erpnext
from erpnext.accounts.general_ledger import (
	make_gl_entries,
	make_reverse_gl_entries,
)
from erpnext.accounts.utils import cancel_exchange_gain_loss_journal
from erpnext.controllers.accounts_controller import AccountsController
from erpnext.controllers.sales_and_purchase_return import (
	available_serial_batch_for_return,
	filter_serial_batches,
	make_serial_batch_bundle_for_return,
)
from erpnext.setup.doctype.brand.brand import get_brand_defaults
from erpnext.setup.doctype.item_group.item_group import get_item_group_defaults
from erpnext.stock import get_warehouse_account_map
from erpnext.stock.doctype.item.item import get_item_defaults
from erpnext.stock.stock_ledger import get_items_to_be_repost


class QualityInspectionRequiredError(frappe.ValidationError):
	pass


class QualityInspectionRejectedError(frappe.ValidationError):
	pass


class QualityInspectionNotSubmittedError(frappe.ValidationError):
	pass


class BatchExpiredError(frappe.ValidationError):
	pass


class StockController(AccountsController):
	def validate(self):
		super().validate()

		if self.docstatus == 0:
			for table_name in ["items", "packed_items", "supplied_items"]:
				self.validate_duplicate_serial_and_batch_bundle(table_name)

		if not self.get("is_return"):
			self.validate_inspection()

		self.validate_warehouse_of_sabb()
		self.validate_serialized_batch()
		self.clean_serial_nos()
		self.validate_customer_provided_item()
		self.set_rate_of_stock_uom()
		self.validate_internal_transfer()
		self.validate_putaway_capacity()
		self.reset_conversion_factor()

	def on_update(self):
		super().on_update()
		self.check_zero_rate()

	def validate_warehouse_of_sabb(self):
		from erpnext.stock.services.serial_batch_bundle import SerialBatchBundleService

		return SerialBatchBundleService(self).validate_warehouse_of_sabb()

	def reset_conversion_factor(self):
		for row in self.get("items"):
			if row.uom != row.stock_uom:
				continue

			if row.conversion_factor != 1.0:
				row.conversion_factor = 1.0
				frappe.msgprint(
					_(
						"Conversion factor for item {0} has been reset to 1.0 as the uom {1} is same as stock uom {2}."
					).format(bold(row.item_code), bold(row.uom), bold(row.stock_uom)),
					alert=True,
				)

	def check_zero_rate(self):
		if self.doctype in [
			"POS Invoice",
			"Purchase Invoice",
			"Sales Invoice",
			"Delivery Note",
			"Purchase Receipt",
			"Stock Entry",
			"Stock Reconciliation",
		]:
			for item in self.get("items"):
				if (
					(
						item.get("valuation_rate") == 0
						or (item.get("incoming_rate") == 0 and self.get("update_stock", 1))
					)
					and item.get("allow_zero_valuation_rate") == 0
					and frappe.get_cached_value("Item", item.item_code, "is_stock_item")
				):
					frappe.toast(
						_("Row #{0}: Item {1} has zero rate but '{2}' is not enabled.").format(
							item.idx,
							frappe.bold(item.item_code),
							item.meta.get_label("allow_zero_valuation_rate"),
						),
						indicator="orange",
					)

	def validate_items_exist(self):
		if not self.get("items"):
			return

		items = [d.item_code for d in self.get("items")]

		exists_items = frappe.get_all("Item", filters={"name": ("in", items)}, pluck="name")
		non_exists_items = set(items) - set(exists_items)

		if non_exists_items:
			frappe.throw(_("Items {0} do not exist in the Item master.").format(", ".join(non_exists_items)))

	def validate_duplicate_serial_and_batch_bundle(self, table_name):
		from erpnext.stock.services.serial_batch_bundle import SerialBatchBundleService

		return SerialBatchBundleService(self).validate_duplicate_serial_and_batch_bundle(table_name)

	def get_item_wise_inventory_account_map(self):
		inventory_account_map = frappe._dict()
		for table in ["items", "packed_items", "supplied_items"]:
			if not self.get(table):
				continue

			_map = get_item_wise_inventory_account_map(self.get(table), self.company)
			inventory_account_map.update(_map)

		return inventory_account_map

	@property
	def use_item_inventory_account(self):
		return frappe.get_cached_value("Company", self.company, "enable_item_wise_inventory_account")

	def get_inventory_account_dict(self, row, inventory_account_map, warehouse_field=None):
		account_dict = frappe._dict()

		if isinstance(row, dict):
			row = frappe._dict(row)

		if self.use_item_inventory_account:
			item_code = (
				row.rm_item_code if hasattr(row, "rm_item_code") and row.rm_item_code else row.item_code
			)

			account_dict = inventory_account_map.get(item_code)

			if not account_dict:
				frappe.throw(
					_(
						"Please set default inventory account for item {0}, or their item group or brand."
					).format(bold(item_code))
				)

			return account_dict

		if not warehouse_field:
			warehouse_field = "warehouse"

		warehouse = row.get(warehouse_field)
		if not warehouse:
			warehouse = self.get(warehouse_field)

		if warehouse and warehouse in inventory_account_map:
			account_dict = inventory_account_map[warehouse]

		return account_dict

	def get_inventory_account_map(self):
		if self.use_item_inventory_account:
			return self.get_item_wise_inventory_account_map()

		return get_warehouse_account_map(self.company)

	def make_gl_entries(self, gl_entries=None, from_repost=False, via_landed_cost_voucher=False):
		if self.docstatus == 2:
			make_reverse_gl_entries(voucher_type=self.doctype, voucher_no=self.name)

		provisional_accounting_for_non_stock_items = cint(
			frappe.get_cached_value(
				"Company", self.company, "enable_provisional_accounting_for_non_stock_items"
			)
		)

		is_asset_pr = any(d.get("is_fixed_asset") for d in self.get("items"))
		need_inventory_map = (self.get_stock_items() or self.get("packed_items")) and (
			cint(erpnext.is_perpetual_inventory_enabled(self.company))
		)

		inventory_account_map = frappe._dict()
		if need_inventory_map:
			inventory_account_map = self.get_inventory_account_map()

		if need_inventory_map or provisional_accounting_for_non_stock_items or is_asset_pr:
			if self.docstatus == 1:
				if not gl_entries:
					gl_entries = (
						self.get_gl_entries(inventory_account_map, via_landed_cost_voucher)
						if self.doctype == "Purchase Receipt"
						else self.get_gl_entries(inventory_account_map)
					)
				make_gl_entries(gl_entries, from_repost=from_repost)

	def validate_serialized_batch(self):
		from erpnext.stock.services.serial_batch_bundle import SerialBatchBundleService

		return SerialBatchBundleService(self).validate_serialized_batch()

	def clean_serial_nos(self):
		from erpnext.stock.services.serial_batch_bundle import SerialBatchBundleService

		return SerialBatchBundleService(self).clean_serial_nos()

	def make_bundle_using_old_serial_batch_fields(self, table_name=None, via_landed_cost_voucher=False):
		from erpnext.stock.services.serial_batch_bundle import SerialBatchBundleService

		return SerialBatchBundleService(self).make_bundle_using_old_serial_batch_fields(
			table_name, via_landed_cost_voucher
		)

	def make_bundle_for_sales_purchase_return(self, table_name=None):
		from erpnext.stock.services.serial_batch_bundle import SerialBatchBundleService

		return SerialBatchBundleService(self).make_bundle_for_sales_purchase_return(table_name)

	def set_use_serial_batch_fields(self):
		from erpnext.stock.services.serial_batch_bundle import SerialBatchBundleService

		return SerialBatchBundleService(self).set_use_serial_batch_fields()

	def get_gl_entries(
		self, inventory_account_map=None, default_expense_account=None, default_cost_center=None
	):
		from erpnext.stock.services.base_stock_gl_composer import BaseStockGLComposer

		return BaseStockGLComposer(self).compose(
			inventory_account_map, default_expense_account, default_cost_center
		)

	def get_items_and_warehouses(self) -> tuple[list[str], list[str]]:
		from erpnext.stock.services.stock_ledger import StockLedgerService

		return StockLedgerService(self).get_items_and_warehouses()

	def get_stock_ledger_details(self):
		from erpnext.stock.services.stock_ledger import StockLedgerService

		return StockLedgerService(self).get_stock_ledger_details()

	def delete_auto_created_batches(self):
		from erpnext.stock.services.serial_batch_bundle import SerialBatchBundleService

		return SerialBatchBundleService(self).delete_auto_created_batches()

	def set_serial_and_batch_bundle(self, table_name=None, ignore_validate=False):
		from erpnext.stock.services.serial_batch_bundle import SerialBatchBundleService

		return SerialBatchBundleService(self).set_serial_and_batch_bundle(table_name, ignore_validate)

	def make_package_for_transfer(
		self, serial_and_batch_bundle, warehouse, type_of_transaction=None, do_not_submit=None, qty=0
	):
		from erpnext.stock.services.serial_batch_bundle import SerialBatchBundleService

		return SerialBatchBundleService(self).make_package_for_transfer(
			serial_and_batch_bundle, warehouse, type_of_transaction, do_not_submit, qty
		)

	def get_sl_entries(self, d, args):
		from erpnext.stock.services.stock_ledger import StockLedgerService

		return StockLedgerService(self).get_sl_entries(d, args)

	def set_landed_cost_voucher_amount(self):
		for d in self.get("items"):
			lcv_item = frappe.qb.DocType("Landed Cost Item")
			query = (
				frappe.qb.from_(lcv_item)
				.select(Sum(lcv_item.applicable_charges), lcv_item.cost_center)
				.where((lcv_item.docstatus == 1) & (lcv_item.receipt_document == self.name))
			)

			if self.doctype == "Stock Entry":
				query = query.where(lcv_item.stock_entry_item == d.name)
			else:
				query = query.where(lcv_item.purchase_receipt_item == d.name)

			lc_voucher_data = query.run(as_list=True)

			d.landed_cost_voucher_amount = lc_voucher_data[0][0] if lc_voucher_data else 0.0
			if not d.cost_center and lc_voucher_data and lc_voucher_data[0][1]:
				d.db_set("cost_center", lc_voucher_data[0][1])

	def has_landed_cost_amount(self):
		for row in self.items:
			if row.get("landed_cost_voucher_amount"):
				return True

		return False

	def get_item_account_wise_lcv_entries(self):
		if not self.has_landed_cost_amount():
			return

		landed_cost_vouchers = frappe.get_all(
			"Landed Cost Purchase Receipt",
			fields=["parent"],
			filters={"receipt_document": self.name, "docstatus": 1},
		)

		if not landed_cost_vouchers:
			return

		item_account_wise_cost = {}

		row_fieldname = "purchase_receipt_item"
		if self.doctype == "Stock Entry":
			row_fieldname = "stock_entry_item"

		for lcv in landed_cost_vouchers:
			landed_cost_voucher_doc = frappe.get_doc("Landed Cost Voucher", lcv.parent)

			based_on_field = "applicable_charges"
			# Use amount field for total item cost for manually cost distributed LCVs
			if landed_cost_voucher_doc.distribute_charges_based_on != "Distribute Manually":
				based_on_field = frappe.scrub(landed_cost_voucher_doc.distribute_charges_based_on)

			total_item_cost = 0

			if based_on_field:
				for item in landed_cost_voucher_doc.items:
					total_item_cost += item.get(based_on_field)

			for item in landed_cost_voucher_doc.items:
				if item.receipt_document == self.name:
					for account in landed_cost_voucher_doc.taxes:
						exchange_rate = account.exchange_rate or 1
						item_account_wise_cost.setdefault((item.item_code, item.get(row_fieldname)), {})
						item_account_wise_cost[(item.item_code, item.get(row_fieldname))].setdefault(
							account.expense_account, {"amount": 0.0, "base_amount": 0.0}
						)

						item_row = item_account_wise_cost[(item.item_code, item.get(row_fieldname))][
							account.expense_account
						]

						if total_item_cost > 0:
							item_row["amount"] += account.amount * item.get(based_on_field) / total_item_cost

							item_row["base_amount"] += (
								account.base_amount * item.get(based_on_field) / total_item_cost
							)
						else:
							item_row["amount"] += item.applicable_charges / exchange_rate
							item_row["base_amount"] += item.applicable_charges

		return item_account_wise_cost

	def update_inventory_dimensions(self, row, sl_dict) -> None:
		from erpnext.stock.services.stock_ledger import StockLedgerService

		return StockLedgerService(self).update_inventory_dimensions(row, sl_dict)

	def make_sl_entries(self, sl_entries, allow_negative_stock=False, via_landed_cost_voucher=False):
		from erpnext.stock.services.stock_ledger import StockLedgerService

		return StockLedgerService(self).make_sl_entries(
			sl_entries, allow_negative_stock, via_landed_cost_voucher
		)

	def make_gl_entries_on_cancel(self, from_repost=False):
		if not from_repost:
			cancel_exchange_gain_loss_journal(frappe._dict(doctype=self.doctype, name=self.name))
		if frappe.db.sql(
			"""select name from `tabGL Entry` where voucher_type=%s
			and voucher_no=%s""",
			(self.doctype, self.name),
		):
			self.make_gl_entries()

	def validate_warehouse(self):
		from erpnext.stock.utils import validate_disabled_warehouse, validate_warehouse_company

		warehouses = list(set(d.warehouse for d in self.get("items") if getattr(d, "warehouse", None)))

		target_warehouses = list(
			set([d.target_warehouse for d in self.get("items") if getattr(d, "target_warehouse", None)])
		)

		warehouses.extend(target_warehouses)

		from_warehouse = list(
			set([d.from_warehouse for d in self.get("items") if getattr(d, "from_warehouse", None)])
		)

		warehouses.extend(from_warehouse)

		for w in warehouses:
			validate_disabled_warehouse(w)
			validate_warehouse_company(w, self.company)

	def update_billing_percentage(self, update_modified=True):
		target_ref_field = "amount"
		if self.doctype == "Delivery Note":
			total_amount = total_returned = 0
			for item in self.items:
				total_amount += flt(item.amount)
				total_returned += flt(item.returned_qty * item.rate)

			if total_returned < total_amount:
				target_ref_field = {"SUB": ["amount", {"MUL": ["returned_qty", "rate"]}], "as": "ref_amount"}

		self._update_percent_field(
			{
				"target_dt": self.doctype + " Item",
				"target_parent_dt": self.doctype,
				"target_parent_field": "per_billed",
				"target_ref_field": target_ref_field,
				"target_field": "billed_amt",
				"name": self.name,
			},
			update_modified,
		)

	def validate_inspection(self):
		"""Checks if quality inspection is set/ is valid for Items that require inspection."""
		inspection_fieldname_map = {
			"Purchase Receipt": "inspection_required_before_purchase",
			"Purchase Invoice": "inspection_required_before_purchase",
			"Subcontracting Receipt": "inspection_required_before_purchase",
			"Sales Invoice": "inspection_required_before_delivery",
			"Delivery Note": "inspection_required_before_delivery",
		}
		inspection_required_fieldname = inspection_fieldname_map.get(self.doctype)

		# return if inspection is not required on document level
		if (
			(not inspection_required_fieldname and self.doctype != "Stock Entry")
			or (self.doctype == "Stock Entry" and not self.inspection_required)
			or (self.doctype in ["Sales Invoice", "Purchase Invoice"] and not self.update_stock)
		):
			return

		for row in self.get("items"):
			qi_required = False
			if inspection_required_fieldname and frappe.get_cached_value(
				"Item", row.item_code, inspection_required_fieldname
			):
				qi_required = True
			elif self.doctype == "Stock Entry" and row.t_warehouse:
				qi_required = True  # inward stock needs inspection

			if row.get("secondary_item_type") or row.get("is_legacy_scrap_item"):
				continue

			if qi_required:  # validate row only if inspection is required on item level
				if self.doctype in [
					"Purchase Receipt",
					"Purchase Invoice",
					"Sales Invoice",
					"Delivery Note",
				] and frappe.get_single_value(
					"Stock Settings", "allow_to_make_quality_inspection_after_purchase_or_delivery"
				):
					return

				self.validate_qi_presence(row)
				if self.docstatus == 1:
					self.validate_qi_submission(row)
					self.validate_qi_rejection(row)

	def validate_qi_presence(self, row):
		"""Check if QI is present on row level. Warn on save and stop on submit if missing."""
		if not row.quality_inspection:
			msg = _("Row #{0}: Quality Inspection is required for Item {1}").format(
				row.idx, frappe.bold(row.item_code)
			)
			if self.docstatus == 1:
				frappe.throw(msg, title=_("Inspection Required"), exc=QualityInspectionRequiredError)
			else:
				frappe.msgprint(msg, title=_("Inspection Required"), indicator="blue")

	def validate_qi_submission(self, row):
		"""Check if QI is submitted on row level, during submission"""
		action = frappe.get_single_value("Stock Settings", "action_if_quality_inspection_is_not_submitted")
		qa_docstatus = frappe.db.get_value("Quality Inspection", row.quality_inspection, "docstatus")

		if qa_docstatus != 1:
			link = frappe.utils.get_link_to_form("Quality Inspection", row.quality_inspection)
			msg = _("Row #{0}: Quality Inspection {1} is not submitted for the item: {2}").format(
				row.idx, link, row.item_code
			)
			if action == "Stop":
				frappe.throw(msg, title=_("Inspection Submission"), exc=QualityInspectionNotSubmittedError)
			else:
				frappe.msgprint(msg, alert=True, indicator="orange")

	def validate_qi_rejection(self, row):
		"""Check if QI is rejected on row level, during submission"""
		action = frappe.get_single_value("Stock Settings", "action_if_quality_inspection_is_rejected")
		qa_status = frappe.db.get_value("Quality Inspection", row.quality_inspection, "status")

		if qa_status == "Rejected":
			link = frappe.utils.get_link_to_form("Quality Inspection", row.quality_inspection)
			msg = _("Row #{0}: Quality Inspection {1} was rejected for item {2}").format(
				row.idx, link, row.item_code
			)
			if action == "Stop":
				frappe.throw(msg, title=_("Inspection Rejected"), exc=QualityInspectionRejectedError)
			else:
				frappe.msgprint(msg, alert=True, indicator="orange")

	def update_blanket_order(self):
		blanket_orders = list(set([d.blanket_order for d in self.items if d.blanket_order]))
		for blanket_order in blanket_orders:
			frappe.get_doc("Blanket Order", blanket_order).update_ordered_qty()

	def validate_customer_provided_item(self):
		for d in self.get("items"):
			# Customer Provided parts will have zero valuation rate
			if frappe.get_cached_value("Item", d.item_code, "is_customer_provided_item"):
				d.allow_zero_valuation_rate = 1

	def set_rate_of_stock_uom(self):
		if self.doctype in [
			"Purchase Receipt",
			"Purchase Invoice",
			"Purchase Order",
			"Sales Invoice",
			"Sales Order",
			"Delivery Note",
			"Quotation",
		]:
			for d in self.get("items"):
				d.stock_uom_rate = d.rate / (d.conversion_factor or 1)

	def validate_internal_transfer(self):
		if self.doctype in ("Sales Invoice", "Delivery Note", "Purchase Invoice", "Purchase Receipt"):
			if self.is_internal_transfer():
				self.validate_in_transit_warehouses()
				self.validate_multi_currency()
				self.validate_packed_items()

				if self.get("is_internal_supplier") and self.docstatus == 1:
					self.validate_internal_transfer_qty()
			else:
				self.validate_internal_transfer_warehouse()

	def validate_internal_transfer_warehouse(self):
		for row in self.items:
			if row.get("target_warehouse"):
				row.target_warehouse = None

			if row.get("from_warehouse"):
				row.from_warehouse = None

	def validate_in_transit_warehouses(self):
		if (self.doctype == "Sales Invoice" and self.get("update_stock")) or self.doctype == "Delivery Note":
			for item in self.get("items"):
				if not item.target_warehouse:
					frappe.throw(
						_("Row {0}: Target Warehouse is mandatory for internal transfers").format(item.idx)
					)

		if (
			self.doctype == "Purchase Invoice" and self.get("update_stock")
		) or self.doctype == "Purchase Receipt":
			for item in self.get("items"):
				if not item.from_warehouse:
					frappe.throw(
						_("Row {0}: From Warehouse is mandatory for internal transfers").format(item.idx)
					)

	def validate_multi_currency(self):
		if self.currency != self.company_currency:
			frappe.throw(_("Internal transfers can only be done in company's default currency"))

	def validate_packed_items(self):
		if self.doctype in ("Sales Invoice", "Delivery Note Item") and self.get("packed_items"):
			frappe.throw(_("Packed Items cannot be transferred internally"))

	def validate_internal_transfer_qty(self):
		if self.doctype not in ["Purchase Invoice", "Purchase Receipt"]:
			return

		self.__inter_company_reference = (
			self.get("inter_company_reference")
			if self.doctype == "Purchase Invoice"
			else self.get("inter_company_invoice_reference")
		)

		item_wise_transfer_qty = self.get_item_wise_inter_transfer_qty()
		if not item_wise_transfer_qty:
			return

		item_wise_received_qty = self.get_item_wise_inter_received_qty()
		precision = frappe.get_precision(self.doctype + " Item", "qty")

		over_receipt_allowance = frappe.get_single_value("Stock Settings", "over_delivery_receipt_allowance")

		parent_doctype = {
			"Purchase Receipt": "Delivery Note",
			"Purchase Invoice": "Sales Invoice",
		}.get(self.doctype)

		for key, transferred_qty in item_wise_transfer_qty.items():
			recevied_qty = flt(item_wise_received_qty.get(key), precision)
			if over_receipt_allowance:
				transferred_qty = transferred_qty + flt(
					transferred_qty * over_receipt_allowance / 100, precision
				)

			if recevied_qty > flt(transferred_qty, precision):
				frappe.throw(
					_("For Item {0} cannot be received more than {1} qty against the {2} {3}").format(
						bold(key[1]),
						bold(flt(transferred_qty, precision)),
						bold(parent_doctype),
						get_link_to_form(parent_doctype, self.__inter_company_reference),
					)
				)

	def get_item_wise_inter_transfer_qty(self):
		parent_doctype = {
			"Purchase Receipt": "Delivery Note",
			"Purchase Invoice": "Sales Invoice",
		}.get(self.doctype)

		child_doctype = parent_doctype + " Item"

		parent_tab = frappe.qb.DocType(parent_doctype)
		child_tab = frappe.qb.DocType(child_doctype)

		query = (
			frappe.qb.from_(parent_doctype)
			.inner_join(child_tab)
			.on(child_tab.parent == parent_tab.name)
			.select(
				child_tab.name,
				child_tab.item_code,
				child_tab.qty,
			)
			.where((parent_tab.name == self.__inter_company_reference) & (parent_tab.docstatus == 1))
		)

		data = query.run(as_dict=True)
		item_wise_transfer_qty = defaultdict(float)
		for row in data:
			item_wise_transfer_qty[(row.name, row.item_code)] += flt(row.qty)

		return item_wise_transfer_qty

	def get_item_wise_inter_received_qty(self):
		child_doctype = self.doctype + " Item"

		parent_tab = frappe.qb.DocType(self.doctype)
		child_tab = frappe.qb.DocType(child_doctype)

		query = (
			frappe.qb.from_(self.doctype)
			.inner_join(child_tab)
			.on(child_tab.parent == parent_tab.name)
			.select(
				child_tab.item_code,
				child_tab.qty,
			)
			.where(parent_tab.docstatus == 1)
		)

		if self.doctype == "Purchase Invoice":
			query = query.select(
				child_tab.sales_invoice_item.as_("name"),
			)

			query = query.where(
				parent_tab.inter_company_invoice_reference == self.inter_company_invoice_reference
			)
		else:
			query = query.select(
				child_tab.delivery_note_item.as_("name"),
			)

			query = query.where(parent_tab.inter_company_reference == self.inter_company_reference)

		data = query.run(as_dict=True)
		item_wise_transfer_qty = defaultdict(float)
		for row in data:
			item_wise_transfer_qty[(row.name, row.item_code)] += flt(row.qty)

		return item_wise_transfer_qty

	def validate_putaway_capacity(self):
		# if over receipt is attempted while 'apply putaway rule' is disabled
		# and if rule was applied on the transaction, validate it.
		from erpnext.stock.doctype.putaway_rule.putaway_rule import get_available_putaway_capacity

		valid_doctype = self.doctype in (
			"Purchase Receipt",
			"Stock Entry",
			"Purchase Invoice",
			"Stock Reconciliation",
		)

		if not frappe.get_all("Putaway Rule", limit=1):
			return

		if self.doctype == "Purchase Invoice" and self.get("update_stock") == 0:
			valid_doctype = False

		if valid_doctype:
			rule_map = defaultdict(dict)
			for item in self.get("items"):
				warehouse_field = "t_warehouse" if self.doctype == "Stock Entry" else "warehouse"
				rule = frappe.db.get_value(
					"Putaway Rule",
					{"item_code": item.get("item_code"), "warehouse": item.get(warehouse_field)},
					["stock_capacity", "name", "disable"],
					as_dict=True,
				)
				if rule:
					if rule.get("disabled"):
						continue  # dont validate for disabled rule

					if self.doctype == "Stock Reconciliation":
						stock_qty = flt(item.qty)
					else:
						stock_qty = (
							flt(item.transfer_qty) if self.doctype == "Stock Entry" else flt(item.stock_qty)
						)

					rule_name = rule.get("name")
					if not rule_map[rule_name]:
						rule_map[rule_name]["warehouse"] = item.get(warehouse_field)
						rule_map[rule_name]["item"] = item.get("item_code")
						rule_map[rule_name]["qty_put"] = 0
						rule_map[rule_name]["capacity"] = (
							rule.stock_capacity
							if self.doctype == "Stock Reconciliation"
							else get_available_putaway_capacity(rule_name)
						)
					rule_map[rule_name]["qty_put"] += flt(stock_qty)

			for rule, values in rule_map.items():
				if flt(values["qty_put"]) > flt(values["capacity"]):
					message = self.prepare_over_receipt_message(rule, values)
					frappe.throw(msg=message, title=_("Over Receipt"))

	def prepare_over_receipt_message(self, rule, values):
		message = _("{0} qty of Item {1} is being received into Warehouse {2} with capacity {3}.").format(
			frappe.bold(values["qty_put"]),
			frappe.bold(values["item"]),
			frappe.bold(values["warehouse"]),
			frappe.bold(values["capacity"]),
		)
		message += "<br><br>"
		rule_link = frappe.utils.get_link_to_form("Putaway Rule", rule)
		message += _("Please adjust the qty or edit {0} to proceed.").format(rule_link)
		return message

	def repost_future_sle_and_gle(self, force=False, via_landed_cost_voucher=False):
		from erpnext.stock.services.stock_ledger import StockLedgerService

		return StockLedgerService(self).repost_future_sle_and_gle(force, via_landed_cost_voucher)

	def add_gl_entry(
		self,
		gl_entries,
		account,
		cost_center,
		debit,
		credit,
		remarks,
		against_account,
		debit_in_account_currency=None,
		credit_in_account_currency=None,
		account_currency=None,
		project=None,
		voucher_detail_no=None,
		item=None,
		posting_date=None,
	):
		from erpnext.accounts.services.base_gl_composer import add_gl_entry

		add_gl_entry(
			self,
			gl_entries,
			account,
			cost_center,
			debit,
			credit,
			remarks,
			against_account,
			debit_in_account_currency,
			credit_in_account_currency,
			account_currency,
			project,
			voucher_detail_no,
			item,
			posting_date,
		)

	def update_stock_reservation_entries(self):
		def get_sre_list():
			table = frappe.qb.DocType("Stock Reservation Entry")
			query = (
				frappe.qb.from_(table)
				.select(table.name)
				.where(
					(table.docstatus == 1)
					& (table.voucher_type == data_map[purpose or self.doctype]["voucher_type"])
					& (
						table.voucher_no
						== data_map[purpose or self.doctype].get(
							"voucher_no", item.get("subcontracting_order")
						)
					)
				)
				.orderby(table.creation)
			)
			if reference_field := data_map[purpose or self.doctype].get("voucher_detail_no_field"):
				query = query.where(table.voucher_detail_no == item.get(reference_field))
			else:
				query = query.where(
					(table.item_code == item.rm_item_code) & (table.warehouse == self.supplier_warehouse)
				)

			return query.run(pluck="name")

		def get_data_map():
			return {
				"Subcontracting Delivery": {
					"table_name": "items",
					"voucher_type": "Subcontracting Inward Order",
					"voucher_no": self.get("subcontracting_inward_order"),
					"voucher_detail_no_field": "scio_detail",
					"field": "delivered_qty",
				},
				"Send to Subcontractor": {
					"table_name": "items",
					"voucher_type": "Subcontracting Order",
					"voucher_no": self.get("subcontracting_order"),
					"voucher_detail_no_field": "sco_rm_detail",
					"field": "transferred_qty",
				},
				"Subcontracting Receipt": {
					"table_name": "supplied_items",
					"voucher_type": "Subcontracting Order",
					"field": "consumed_qty",
				},
			}

		purpose = self.get("purpose")
		if (
			purpose == "Subcontracting Delivery"
			or (
				purpose == "Send to Subcontractor"
				and frappe.get_value("Subcontracting Order", self.subcontracting_order, "reserve_stock")
			)
			or (self.doctype == "Subcontracting Receipt" and self.has_reserved_stock() and not self.is_return)
		):
			data_map = get_data_map()

			field = data_map[purpose or self.doctype]["field"]
			for item in self.get(data_map[purpose or self.doctype]["table_name"]):
				sre_list = get_sre_list()

				if not sre_list:
					continue

				qty = item.get("transfer_qty", item.get("consumed_qty"))
				for sre in sre_list:
					if qty <= 0:
						break

					sre_doc = frappe.get_doc("Stock Reservation Entry", sre)

					working_qty = 0
					if sre_doc.reservation_based_on == "Serial and Batch":
						sbb = frappe.get_doc("Serial and Batch Bundle", item.serial_and_batch_bundle)
						if sre_doc.has_serial_no:
							serial_nos = [d.serial_no for d in sbb.entries]
							for entry in sre_doc.sb_entries:
								if entry.serial_no in serial_nos:
									entry.delivered_qty = 1 if self._action == "submit" else 0
									entry.db_update()
									working_qty += 1
									serial_nos.remove(entry.serial_no)
						else:
							batch_qty = {d.batch_no: -1 * d.qty for d in sbb.entries}
							for entry in sre_doc.sb_entries:
								if entry.batch_no in batch_qty:
									delivered_qty = min(
										(entry.qty - entry.delivered_qty)
										if self._action == "submit"
										else entry.delivered_qty,
										batch_qty[entry.batch_no],
									)
									entry.delivered_qty += (
										delivered_qty if self._action == "submit" else (-1 * delivered_qty)
									)
									entry.db_update()
									working_qty += delivered_qty
									batch_qty[entry.batch_no] -= delivered_qty
					else:
						working_qty = min(
							(sre_doc.reserved_qty - sre_doc.get(field))
							if self._action == "submit"
							else sre_doc.get(field),
							qty,
						)

					sre_doc.set(
						field,
						sre_doc.get(field)
						+ (working_qty if self._action == "submit" else (-1 * working_qty)),
					)
					sre_doc.db_update()
					sre_doc.update_reserved_qty_in_voucher()
					sre_doc.update_status()
					sre_doc.update_reserved_stock_in_bin()

					qty -= working_qty

	def check_for_on_hold_or_closed_status(
		self, ref_doctype: str, ref_fieldname: str, exclude_if_field: str | None = None
	) -> None:
		def _include(d):
			return d.get(ref_fieldname) and not (exclude_if_field and d.get(exclude_if_field))

		included = [(d, d.get(ref_fieldname)) for d in self.get("items") if _include(d)]
		if not included:
			return

		status_map = {
			r.name: r.status
			for r in frappe.get_all(
				ref_doctype,
				filters={"name": ["in", {name for _, name in included}]},
				fields=["name", "status"],
			)
		}

		errors = []
		seen = set()
		for _d, ref_name in included:
			if ref_name in seen:
				continue
			seen.add(ref_name)
			if (status := status_map.get(ref_name)) in ("Closed", "On Hold"):
				errors.append(
					_("{ref_doctype} {ref_name} status is {status}.").format(
						ref_doctype=frappe.bold(_(ref_doctype)),
						ref_name=frappe.bold(ref_name),
						status=frappe.bold(_(status)),
					)
				)

		if errors:
			frappe.throw("<br>".join(errors), frappe.InvalidStatusError)


@frappe.whitelist()
def show_accounting_ledger_preview(company: str, doctype: str, docname: str):
	filters = frappe._dict(company=company, include_dimensions=1)
	doc = frappe.get_lazy_doc(doctype, docname)
	doc.run_method("before_gl_preview")

	gl_columns, gl_data = get_accounting_ledger_preview(doc, filters)

	frappe.db.rollback()

	return {"gl_columns": gl_columns, "gl_data": gl_data}


@frappe.whitelist()
def show_stock_ledger_preview(company: str, doctype: str, docname: str):
	filters = frappe._dict(company=company)
	doc = frappe.get_lazy_doc(doctype, docname)
	doc.run_method("before_sl_preview")

	sl_columns, sl_data = get_stock_ledger_preview(doc, filters)

	frappe.db.rollback()

	return {
		"sl_columns": sl_columns,
		"sl_data": sl_data,
	}


def get_accounting_ledger_preview(doc, filters):
	from erpnext.accounts.report.general_ledger.general_ledger import get_columns as get_gl_columns

	gl_columns, gl_data = [], []
	fields = [
		"posting_date",
		"account",
		"debit",
		"credit",
		"against",
		"party_type",
		"party",
		"cost_center",
		"against_voucher_type",
		"against_voucher",
	]

	doc.docstatus = 1

	if doc.get("update_stock") or doc.doctype in ("Purchase Receipt", "Delivery Note", "Stock Entry"):
		doc.update_stock_ledger()

	doc.make_gl_entries()
	columns = get_gl_columns(filters)
	gl_entries = get_gl_entries_for_preview(doc.doctype, doc.name, fields)

	gl_columns = get_columns(columns, fields)
	gl_data = get_data(fields, gl_entries)

	return gl_columns, gl_data


def get_stock_ledger_preview(doc, filters):
	from erpnext.stock.report.stock_ledger.stock_ledger import get_columns as get_sl_columns

	sl_columns, sl_data = [], []
	fields = [
		"item_code",
		"stock_uom",
		"actual_qty",
		"qty_after_transaction",
		"warehouse",
		"incoming_rate",
		"valuation_rate",
		"stock_value",
		"stock_value_difference",
	]
	columns_fields = [
		"item_code",
		"stock_uom",
		"in_qty",
		"out_qty",
		"qty_after_transaction",
		"warehouse",
		"incoming_rate",
		"in_out_rate",
		"stock_value",
		"stock_value_difference",
	]

	if doc.get("update_stock") or doc.doctype in ("Purchase Receipt", "Delivery Note", "Stock Entry"):
		doc.docstatus = 1
		doc.make_bundle_using_old_serial_batch_fields()
		doc.update_stock_ledger()

		columns = get_sl_columns(filters)
		sl_entries = get_sl_entries_for_preview(doc.doctype, doc.name, fields)

		sl_columns = get_columns(columns, columns_fields)
		sl_data = get_data(columns_fields, sl_entries)

	return sl_columns, sl_data


def get_sl_entries_for_preview(doctype, docname, fields):
	sl_entries = frappe.get_all(
		"Stock Ledger Entry", filters={"voucher_type": doctype, "voucher_no": docname}, fields=fields
	)

	for entry in sl_entries:
		if entry.actual_qty > 0:
			entry["in_qty"] = entry.actual_qty
			entry["out_qty"] = 0
		else:
			entry["out_qty"] = abs(entry.actual_qty)
			entry["in_qty"] = 0

		entry["in_out_rate"] = entry["valuation_rate"]

	return sl_entries


def get_gl_entries_for_preview(doctype, docname, fields):
	return frappe.get_all("GL Entry", filters={"voucher_type": doctype, "voucher_no": docname}, fields=fields)


def get_columns(raw_columns, fields):
	return [
		{"name": d.get("label"), "editable": False, "width": 110, "fieldtype": d.get("fieldtype")}
		for d in raw_columns
		if not d.get("hidden") and d.get("fieldname") in fields
	]


def get_data(raw_columns, raw_data):
	datatable_data = []
	for row in raw_data:
		data_row = []
		for column in raw_columns:
			data_row.append(row.get(column) or "")

		datatable_data.append(data_row)

	return datatable_data


def repost_required_for_queue(doc: StockController) -> bool:
	"""check if stock document contains repeated item-warehouse with queue based valuation.

	if queue exists for repeated items then SLEs need to reprocessed in background again.
	"""

	consuming_sles = frappe.db.get_all(
		"Stock Ledger Entry",
		filters={
			"voucher_type": doc.doctype,
			"voucher_no": doc.name,
			"actual_qty": ("<", 0),
			"is_cancelled": 0,
		},
		fields=["item_code", "warehouse", "stock_queue"],
	)
	item_warehouses = [(sle.item_code, sle.warehouse) for sle in consuming_sles]

	unique_item_warehouses = set(item_warehouses)

	if len(unique_item_warehouses) == len(item_warehouses):
		return False

	for sle in consuming_sles:
		if sle.stock_queue != "[]":  # using FIFO/LIFO valuation
			return True
	return False


@frappe.whitelist()
def check_item_quality_inspection(doctype: str, docstatus: str | int, items: str | list[dict]):
	if isinstance(items, str):
		items = json.loads(items)

	inspection_fieldname_map = {
		"Purchase Receipt": "inspection_required_before_purchase",
		"Purchase Invoice": "inspection_required_before_purchase",
		"Subcontracting Receipt": "inspection_required_before_purchase",
		"Sales Invoice": "inspection_required_before_delivery",
		"Delivery Note": "inspection_required_before_delivery",
	}

	inspection_fieldname = inspection_fieldname_map.get(doctype)
	if inspection_fieldname is None:
		return []

	allow_after_transaction = cint(docstatus) == 1 and frappe.get_single_value(
		"Stock Settings", "allow_to_make_quality_inspection_after_purchase_or_delivery"
	)

	if allow_after_transaction:
		return items

	item_codes = list({item.get("item_code") for item in items})

	Item = frappe.qb.DocType("Item")
	results = (
		frappe.qb.from_(Item)
		.select(Item.name)
		.where((Item.name.isin(item_codes)) & (Item[inspection_fieldname] == 1))
		.run(as_dict=True)
	)

	inspection_required_items = {row.name for row in results}

	return [item for item in items if item.get("item_code") in inspection_required_items]


@frappe.whitelist()
def make_quality_inspections(
	company: str, doctype: str, docname: str, items: str | list, inspection_type: str
):
	if isinstance(items, str):
		items = json.loads(items)

	inspections = []
	for item in items:
		if flt(item.get("sample_size")) > flt(item.get("qty")):
			frappe.throw(
				_(
					"{item_name}'s Sample Size ({sample_size}) cannot be greater than the Accepted Quantity ({accepted_quantity})"
				).format(
					item_name=item.get("item_name"),
					sample_size=item.get("sample_size"),
					accepted_quantity=item.get("qty"),
				)
			)

		quality_inspection = frappe.get_doc(
			{
				"company": company,
				"doctype": "Quality Inspection",
				"inspection_type": inspection_type,
				"inspected_by": frappe.session.user,
				"reference_type": doctype,
				"reference_name": docname,
				"item_code": item.get("item_code"),
				"description": item.get("description"),
				"sample_size": flt(item.get("sample_size")),
				"item_serial_no": item.get("serial_no").split("\n")[0] if item.get("serial_no") else None,
				"batch_no": item.get("batch_no"),
				"child_row_reference": item.get("child_row_reference"),
			}
		)
		quality_inspection.save()
		inspections.append(quality_inspection.name)

	return inspections


def is_reposting_pending():
	return frappe.db.exists(
		"Repost Item Valuation", {"docstatus": 1, "status": ["in", ["Queued", "In Progress"]]}
	)


def future_sle_exists(args, sl_entries=None):
	from erpnext.stock.utils import get_combine_datetime

	key = (args.voucher_type, args.voucher_no)
	if not hasattr(frappe.local, "future_sle"):
		frappe.local.future_sle = {}

	if validate_future_sle_not_exists(args, key, sl_entries):
		return False
	elif get_cached_data(args, key):
		return True

	if not sl_entries:
		sl_entries = get_sle_entries_against_voucher(args)
		if not sl_entries:
			return

	or_conditions = get_conditions_to_validate_future_sle(sl_entries)

	args["posting_datetime"] = get_combine_datetime(args["posting_date"], args["posting_time"])

	data = frappe.db.sql(
		"""
		select item_code, warehouse, count(name) as total_row
		from `tabStock Ledger Entry`
		where
			({})
			and posting_datetime >= %(posting_datetime)s
			and voucher_no != %(voucher_no)s
			and is_cancelled = 0
		GROUP BY
			item_code, warehouse
		""".format(" or ".join(or_conditions)),
		args,
		as_dict=1,
	)

	for d in data:
		frappe.local.future_sle[key][(d.item_code, d.warehouse)] = d.total_row

	return len(data)


def validate_future_sle_not_exists(args, key, sl_entries=None):
	item_key = ""
	if args.get("item_code"):
		item_key = (args.get("item_code"), args.get("warehouse"))

	if not sl_entries and hasattr(frappe.local, "future_sle"):
		if key not in frappe.local.future_sle:
			return False

		if not frappe.local.future_sle.get(key) or (
			item_key and item_key not in frappe.local.future_sle.get(key)
		):
			return True


def get_cached_data(args, key):
	if key not in frappe.local.future_sle:
		frappe.local.future_sle[key] = frappe._dict({})

	if args.get("item_code"):
		item_key = (args.get("item_code"), args.get("warehouse"))
		count = frappe.local.future_sle[key].get(item_key)

		return True if (count or count == 0) else False
	else:
		return frappe.local.future_sle[key]


def get_sle_entries_against_voucher(args):
	return frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_type": args.voucher_type, "voucher_no": args.voucher_no},
		fields=["item_code", "warehouse"],
		order_by="creation asc",
	)


def get_conditions_to_validate_future_sle(sl_entries):
	warehouse_items_map = {}
	for entry in sl_entries:
		if entry.warehouse not in warehouse_items_map:
			warehouse_items_map[entry.warehouse] = set()

		warehouse_items_map[entry.warehouse].add(entry.item_code)

	or_conditions = []
	for warehouse, items in warehouse_items_map.items():
		or_conditions.append(
			f"""warehouse = {frappe.db.escape(warehouse)}
				and item_code in ({", ".join(frappe.db.escape(item) for item in items)})"""
		)

	return or_conditions


def create_repost_item_valuation_entry(args):
	args = frappe._dict(args)
	repost_entry = frappe.new_doc("Repost Item Valuation")
	repost_entry.based_on = args.based_on
	if not args.based_on:
		repost_entry.based_on = "Transaction" if args.voucher_no else "Item and Warehouse"
	repost_entry.voucher_type = args.voucher_type
	repost_entry.voucher_no = args.voucher_no
	repost_entry.item_code = args.item_code
	repost_entry.warehouse = args.warehouse
	repost_entry.posting_date = args.posting_date
	repost_entry.posting_time = args.posting_time
	repost_entry.company = args.company
	repost_entry.allow_zero_rate = args.allow_zero_rate
	repost_entry.flags.ignore_links = True
	repost_entry.flags.ignore_permissions = True
	repost_entry.via_landed_cost_voucher = args.via_landed_cost_voucher
	repost_entry.save()
	repost_entry.submit()


def create_item_wise_repost_entries(
	voucher_type, voucher_no, allow_zero_rate=False, via_landed_cost_voucher=False
):
	"""Using a voucher create repost item valuation records for all item-warehouse pairs."""

	stock_ledger_entries = get_items_to_be_repost(voucher_type, voucher_no)

	distinct_item_warehouses = set()
	repost_entries = []

	for sle in stock_ledger_entries:
		item_wh = (sle.item_code, sle.warehouse)
		if item_wh in distinct_item_warehouses:
			continue
		distinct_item_warehouses.add(item_wh)

		repost_entry = frappe.new_doc("Repost Item Valuation")
		repost_entry.based_on = "Item and Warehouse"

		repost_entry.item_code = sle.item_code
		repost_entry.warehouse = sle.warehouse
		repost_entry.posting_date = sle.posting_date
		repost_entry.posting_time = sle.posting_time
		repost_entry.allow_zero_rate = allow_zero_rate
		repost_entry.flags.ignore_links = True
		repost_entry.flags.ignore_permissions = True
		repost_entry.via_landed_cost_voucher = via_landed_cost_voucher
		repost_entry.submit()
		repost_entries.append(repost_entry)

	return repost_entries


def make_bundle_for_material_transfer(**kwargs):
	if isinstance(kwargs, dict):
		kwargs = frappe._dict(kwargs)

	bundle_doc = frappe.get_doc("Serial and Batch Bundle", kwargs.serial_and_batch_bundle)

	if not kwargs.type_of_transaction:
		kwargs.type_of_transaction = "Inward"

	bundle_doc = frappe.copy_doc(bundle_doc)
	bundle_doc.docstatus = 0
	bundle_doc.warehouse = kwargs.warehouse
	bundle_doc.type_of_transaction = kwargs.type_of_transaction
	bundle_doc.voucher_type = kwargs.voucher_type
	bundle_doc.voucher_no = "" if kwargs.is_new or kwargs.docstatus == 2 else kwargs.voucher_no
	bundle_doc.is_cancelled = 0

	qty = 0
	if (
		len(bundle_doc.entries) == 1
		and flt(kwargs.qty) < flt(bundle_doc.total_qty)
		and not bundle_doc.has_serial_no
	):
		qty = kwargs.qty

	for row in bundle_doc.entries:
		row.is_outward = 0
		row.qty = abs(qty or row.qty)
		row.stock_value_difference = abs(row.stock_value_difference)
		if kwargs.type_of_transaction == "Outward":
			row.qty *= -1
			row.stock_value_difference *= row.stock_value_difference
			row.is_outward = 1

		row.warehouse = kwargs.warehouse
		row.posting_datetime = bundle_doc.posting_datetime
		row.voucher_type = bundle_doc.voucher_type
		row.voucher_no = bundle_doc.voucher_no
		row.voucher_detail_no = bundle_doc.voucher_detail_no
		row.type_of_transaction = bundle_doc.type_of_transaction
		row.item_code = bundle_doc.item_code

	bundle_doc.set_incoming_rate()
	bundle_doc.calculate_qty_and_amount()
	bundle_doc.flags.ignore_permissions = True
	bundle_doc.flags.ignore_validate = True
	if kwargs.do_not_submit:
		bundle_doc.save(ignore_permissions=True)
	else:
		bundle_doc.submit()

	return bundle_doc.name


def get_item_wise_inventory_account_map(rows, company):
	# returns dict of item_code and its inventory account details
	# Example: {"ITEM-001": {"account": "Stock - ABC", "account_currency": "INR"}, ...}

	inventory_map = frappe._dict()

	for row in rows:
		item_code = row.rm_item_code if hasattr(row, "rm_item_code") and row.rm_item_code else row.item_code
		if not item_code:
			continue

		if inventory_map.get(item_code):
			continue

		item_defaults = get_item_defaults(item_code, company)
		if item_defaults.default_inventory_account:
			inventory_map[item_code] = frappe._dict(
				{
					"account": item_defaults.default_inventory_account,
					"account_currency": item_defaults.inventory_account_currency,
				}
			)

		if not inventory_map.get(item_code):
			item_group_defaults = get_item_group_defaults(item_code, company)
			if item_group_defaults.default_inventory_account:
				inventory_map[item_code] = frappe._dict(
					{
						"account": item_group_defaults.default_inventory_account,
						"account_currency": item_group_defaults.inventory_account_currency,
					}
				)

		if not inventory_map.get(item_code):
			brand_defaults = get_brand_defaults(item_code, company)
			if brand_defaults.default_inventory_account:
				inventory_map[item_code] = frappe._dict(
					{
						"account": brand_defaults.default_inventory_account,
						"account_currency": brand_defaults.inventory_account_currency,
					}
				)

	return inventory_map

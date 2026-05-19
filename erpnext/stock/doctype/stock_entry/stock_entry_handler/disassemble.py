from collections import defaultdict

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import flt

from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos
from erpnext.stock.serial_batch_bundle import SerialBatchCreation
from erpnext.stock.utils import get_combine_datetime

from .manufacturing import ceil_qty_if_uom_has_whole_number, get_bom_items, get_secondary_items


class DisassembleStockEntry:
	def __init__(self, se_doc):
		self.doc = se_doc

	def validate(self):
		self.validate_warehouse()

	def validate_warehouse(self):
		for row in self.doc.items:
			if not row.s_warehouse and not row.t_warehouse:
				frappe.throw(_("Source or Target Warehouse is required for item {0}").format(row.item_code))

	def validate_fg_completed_qty(self):
		if not self.doc.source_stock_entry:
			return

		from erpnext.manufacturing.doctype.work_order.work_order import get_disassembly_available_qty

		available_qty = get_disassembly_available_qty(self.doc.source_stock_entry, self.doc.name)

		if flt(self.doc.fg_completed_qty) > available_qty:
			frappe.throw(
				_(
					"Cannot disassemble {0} qty against Stock Entry {1}. Only {2} qty available to disassemble."
				).format(
					self.doc.fg_completed_qty,
					self.doc.source_stock_entry,
					available_qty,
				),
				title=_("Excess Disassembly"),
			)

	def add_items(self):
		"""
		Priority:
		1. From a specific Manufacture Stock Entry (exact reversal)
		2. From Work Order Manufacture Stock Entries (averaged reversal)
		3. From BOM (standalone disassembly)
		"""

		# Auto-set source_stock_entry if WO has exactly one manufacture entry
		if not self.doc.get("source_stock_entry") and self.doc.work_order:
			manufacture_entries = frappe.get_all(
				"Stock Entry",
				filters={
					"work_order": self.doc.work_order,
					"purpose": "Manufacture",
					"docstatus": 1,
				},
				pluck="name",
			)
			if len(manufacture_entries) == 1:
				self.doc.source_stock_entry = manufacture_entries[0]

		if self.doc.get("source_stock_entry"):
			return self._add_items_for_disassembly_from_stock_entry()

		if self.doc.work_order:
			return self._add_items_for_disassembly_from_work_order()

		return self._add_items_for_disassembly_from_bom()

	def _add_items_for_disassembly_from_stock_entry(self):
		source_fg_qty = frappe.db.get_value("Stock Entry", self.doc.source_stock_entry, "fg_completed_qty")
		if not source_fg_qty:
			frappe.throw(
				_("Source Stock Entry {0} has no finished goods quantity").format(self.doc.source_stock_entry)
			)

		disassemble_qty = flt(self.doc.fg_completed_qty)
		scale_factor = disassemble_qty / flt(source_fg_qty)

		self._append_disassembly_row_from_source(
			disassemble_qty=disassemble_qty,
			scale_factor=scale_factor,
		)

	def _add_items_for_disassembly_from_work_order(self):
		wo_produced_qty = frappe.db.get_value("Work Order", self.doc.work_order, "produced_qty")

		wo_produced_qty = flt(wo_produced_qty)
		if wo_produced_qty <= 0:
			frappe.throw(_("Work Order {0} has no produced qty").format(self.doc.work_order))

		disassemble_qty = flt(self.doc.fg_completed_qty)
		if disassemble_qty <= 0:
			frappe.throw(_("Disassemble Qty cannot be less than or equal to 0."))

		scale_factor = disassemble_qty / wo_produced_qty

		self._append_disassembly_row_from_source(
			disassemble_qty=disassemble_qty,
			scale_factor=scale_factor,
		)

	def _append_disassembly_row_from_source(self, disassemble_qty, scale_factor):
		for source_row in self.get_items_from_manufacture_stock_entry():
			if source_row.is_finished_item:
				qty = disassemble_qty
				s_warehouse = self.doc.from_warehouse or source_row.t_warehouse
				t_warehouse = ""
			elif source_row.s_warehouse:
				# RM: was consumed FROM s_warehouse -> return TO s_warehouse
				qty = flt(source_row.qty * scale_factor)
				s_warehouse = ""
				t_warehouse = self.doc.to_warehouse or source_row.s_warehouse
			else:
				# Scrap/secondary: was produced TO t_warehouse -> take FROM t_warehouse
				qty = flt(source_row.qty * scale_factor)
				s_warehouse = source_row.t_warehouse
				t_warehouse = ""

			item = {
				"item_code": source_row.item_code,
				"item_name": source_row.item_name,
				"description": source_row.description,
				"stock_uom": source_row.stock_uom,
				"uom": source_row.uom,
				"conversion_factor": source_row.conversion_factor,
				"basic_rate": source_row.basic_rate,
				"qty": qty,
				"s_warehouse": s_warehouse,
				"t_warehouse": t_warehouse,
				"is_finished_item": source_row.is_finished_item,
				"type": source_row.type,
				"is_legacy_scrap_item": source_row.is_legacy_scrap_item,
				"bom_secondary_item": source_row.bom_secondary_item,
				"bom_no": source_row.bom_no,
				# batch and serial bundles built on submit
				"use_serial_batch_fields": 1 if (source_row.batch_no or source_row.serial_no) else 0,
			}

			if self.doc.source_stock_entry:
				item.update(
					{
						"against_stock_entry": self.doc.source_stock_entry,
						"ste_detail": source_row.name,
					}
				)

			self.doc.append("items", item)

	def _add_items_for_disassembly_from_bom(self):
		if not self.doc.bom_no or not self.doc.fg_completed_qty:
			frappe.throw(_("BOM and Finished Good Quantity is mandatory for Disassembly"))

		self.add_raw_materials()
		self.add_secondary_items()
		self.add_finished_goods()

	def add_raw_materials(self):
		# Raw materials will be available after disassembly in target warehouse
		items = get_bom_items(self.doc.bom_no, self.doc.use_multi_level_bom)

		for row in items:
			row["t_warehouse"] = self.doc.to_warehouse
			row["from_warehouse"] = ""
			row["is_finished_item"] = 0
			row["qty"] = flt(row["qty"]) * flt(self.doc.fg_completed_qty)
			row["uom"] = row.get("uom") or row.get("stock_uom")
			self.doc.append("items", row)

	def add_secondary_items(self):
		# Secondary items will be removed from source warehouse

		secondary_items = get_secondary_items(self.doc.bom_no, self.doc.work_order)
		for row in secondary_items:
			item_args = {}
			fields = [
				"item_code",
				"item_name",
				"uom",
				"stock_uom",
				"conversion_factor",
				"item_group",
				"description",
				"type",
			]
			for field in fields:
				item_args[field] = row.get(field)

			item_args["is_legacy_scrap_item"] = row.get("is_legacy")
			item_args["s_warehouse"] = self.doc.from_warehouse
			item_args["uom"] = item_args.get("uom") or item_args.get("stock_uom")
			item_args["bom_secondary_item"] = row.get("name")

			row.qty = row.qty * self.doc.fg_completed_qty
			if row.get("process_loss_per"):
				row.qty -= flt(row.qty * row.get("process_loss_per") / 100)
			item_args["qty"] = ceil_qty_if_uom_has_whole_number(row.qty, item_args["uom"])

			self.doc.append("items", item_args)

	def get_production_item_details(self):
		if self.doc.work_order:
			production_item = frappe.get_cached_value("Work Order", self.doc.work_order, "production_item")
		else:
			production_item = frappe.get_cached_value("BOM", self.doc.bom_no, "item")

		item_details = frappe.get_cached_value(
			"Item",
			production_item,
			["item_name", "item_group", "description", "stock_uom", "name"],
			as_dict=1,
		)

		return item_details

	def add_finished_goods(self):
		# Fininshed good will be removed from source warehouse

		item_details = self.get_production_item_details()

		item_details.update(
			{
				"conversion_factor": 1,
				"uom": item_details.stock_uom,
				"qty": self.doc.fg_completed_qty,
				"t_warehouse": None,
				"s_warehouse": self.doc.from_warehouse,
				"is_finished_item": 1,
			}
		)

		item_details["item_code"] = item_details["name"]
		del item_details["name"]

		self.doc.append("items", item_details)

	def get_items_from_manufacture_stock_entry(self):
		SE = frappe.qb.DocType("Stock Entry")
		SED = frappe.qb.DocType("Stock Entry Detail")
		query = frappe.qb.from_(SED).join(SE).on(SED.parent == SE.name).where(SE.docstatus == 1)

		common_fields = [
			SED.item_code,
			SED.item_name,
			SED.description,
			SED.stock_uom,
			SED.uom,
			SED.basic_rate,
			SED.conversion_factor,
			SED.is_finished_item,
			SED.type,
			SED.is_legacy_scrap_item,
			SED.bom_secondary_item,
			SED.batch_no,
			SED.serial_no,
			SED.use_serial_batch_fields,
			SED.s_warehouse,
			SED.t_warehouse,
			SED.bom_no,
		]

		if self.doc.source_stock_entry:
			return (
				query.select(SED.name, SED.qty, SED.transfer_qty, *common_fields)
				.where(SE.name == self.doc.source_stock_entry)
				.orderby(SED.idx)
				.run(as_dict=True)
			)

		return (
			query.select(Sum(SED.qty).as_("qty"), Sum(SED.transfer_qty).as_("transfer_qty"), *common_fields)
			.where(SE.purpose == "Manufacture")
			.where(SE.work_order == self.doc.work_order)
			.groupby(SED.item_code)
			.orderby(SED.idx)
			.run(as_dict=True)
		)

	def on_submit(self):
		self.set_serial_batch_for_disassembly()
		self.update_disassembled_order()

	def on_cancel(self):
		self.update_disassembled_order()

	def set_serial_batch_for_disassembly(self):
		if self.doc.get("source_stock_entry"):
			self._set_serial_batch_for_disassembly_from_stock_entry()
		else:
			self._set_serial_batch_for_disassembly_from_available_materials()

	def _set_serial_batch_for_disassembly_from_stock_entry(self):
		from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
			get_voucher_wise_serial_batch_from_bundle,
		)

		source_fg_qty = flt(
			frappe.db.get_value("Stock Entry", self.doc.source_stock_entry, "fg_completed_qty")
		)
		scale_factor = flt(self.doc.fg_completed_qty) / source_fg_qty if source_fg_qty else 0

		bundle_data = get_voucher_wise_serial_batch_from_bundle(voucher_no=[self.doc.source_stock_entry])
		source_rows_by_name = {r.name: r for r in self.get_items_from_manufacture_stock_entry()}

		for row in self.doc.items:
			if not row.ste_detail:
				continue

			source_row = source_rows_by_name.get(row.ste_detail)
			if not source_row:
				continue

			source_warehouse = source_row.s_warehouse or source_row.t_warehouse
			key = (source_row.item_code, source_warehouse, self.doc.source_stock_entry)
			source_bundle = bundle_data.get(key, {})

			batches = defaultdict(float)
			serial_nos = []

			if source_bundle.get("batch_nos"):
				qty_remaining = row.transfer_qty
				for batch_no, batch_qty in source_bundle["batch_nos"].items():
					if qty_remaining <= 0:
						break
					alloc = min(abs(flt(batch_qty)) * scale_factor, qty_remaining)
					batches[batch_no] = alloc
					qty_remaining -= alloc
			elif source_row.batch_no:
				batches[source_row.batch_no] = row.transfer_qty

			if source_bundle.get("serial_nos"):
				serial_nos = get_serial_nos(source_bundle["serial_nos"])[: int(row.transfer_qty)]
			elif source_row.serial_no:
				serial_nos = get_serial_nos(source_row.serial_no)[: int(row.transfer_qty)]

			self._set_serial_batch_bundle_for_disassembly_row(row, serial_nos, batches)

	def _set_serial_batch_for_disassembly_from_available_materials(self):
		available_materials = get_available_materials(self.doc.work_order, self.doc)
		for row in self.doc.items:
			warehouse = row.s_warehouse or row.t_warehouse
			materials = available_materials.get((row.item_code, warehouse))
			if not materials:
				continue

			batches = defaultdict(float)
			serial_nos = []
			qty = row.transfer_qty
			for batch_no, batch_qty in materials.batch_details.items():
				if qty <= 0:
					break

				batch_qty = abs(batch_qty)
				if batch_qty <= qty:
					batches[batch_no] = batch_qty
					qty -= batch_qty
				else:
					batches[batch_no] = qty
					qty = 0

			if materials.serial_nos:
				serial_nos = materials.serial_nos[: int(row.transfer_qty)]

			self._set_serial_batch_bundle_for_disassembly_row(row, serial_nos, batches)

	def _set_serial_batch_bundle_for_disassembly_row(self, row, serial_nos, batches):
		if not serial_nos and not batches:
			return

		warehouse = row.s_warehouse or row.t_warehouse
		bundle_doc = SerialBatchCreation(
			{
				"item_code": row.item_code,
				"warehouse": warehouse,
				"posting_datetime": get_combine_datetime(self.doc.posting_date, self.doc.posting_time),
				"voucher_type": self.doc.doctype,
				"voucher_no": self.doc.name,
				"voucher_detail_no": row.name,
				"qty": row.transfer_qty,
				"type_of_transaction": "Inward" if row.t_warehouse else "Outward",
				"company": self.doc.company,
				"do_not_submit": True,
			}
		).make_serial_and_batch_bundle(serial_nos=serial_nos, batch_nos=batches)

		row.serial_and_batch_bundle = bundle_doc.name
		row.use_serial_batch_fields = 0

	def update_disassembled_order(self):
		if not self.doc.work_order:
			return

		if self.doc.fg_completed_qty:
			pro_doc = frappe.get_doc("Work Order", self.doc.work_order)
			pro_doc.run_method(
				"update_disassembled_qty", self.doc.fg_completed_qty, self.doc._action == "cancel"
			)


def get_available_materials(work_order, stock_entry_doc=None) -> dict:
	data = get_stock_entry_data(work_order, stock_entry_doc=stock_entry_doc)

	available_materials = {}
	for row in data:
		key = (row.item_code, row.warehouse)
		if row.purpose != "Material Transfer for Manufacture":
			key = (row.item_code, row.s_warehouse)

		if stock_entry_doc and stock_entry_doc.purpose == "Disassemble":
			key = (row.item_code, row.s_warehouse or row.warehouse)

		if key not in available_materials:
			available_materials.setdefault(
				key,
				frappe._dict(
					{"item_details": row, "batch_details": defaultdict(float), "qty": 0, "serial_nos": []}
				),
			)

		item_data = available_materials[key]

		if row.purpose == "Material Transfer for Manufacture" or (
			stock_entry_doc and stock_entry_doc.purpose == "Disassemble" and row.purpose == "Manufacture"
		):
			item_data.qty += row.qty
			if row.batch_no:
				item_data.batch_details[row.batch_no] += row.qty

			elif row.batch_nos:
				for batch_no, qty in row.batch_nos.items():
					item_data.batch_details[batch_no] += qty

			if row.serial_no:
				item_data.serial_nos.extend(get_serial_nos(row.serial_no))
				item_data.serial_nos.sort()

			elif row.serial_nos:
				item_data.serial_nos.extend(get_serial_nos(row.serial_nos))
				item_data.serial_nos.sort()
		else:
			# Consume raw material qty in case of 'Manufacture' or 'Material Consumption for Manufacture'

			item_data.qty -= row.qty
			if row.batch_no:
				item_data.batch_details[row.batch_no] -= row.qty

			elif row.batch_nos:
				for batch_no, qty in row.batch_nos.items():
					item_data.batch_details[batch_no] += qty

			if row.serial_no:
				for serial_no in get_serial_nos(row.serial_no):
					if serial_no in item_data.serial_nos:
						item_data.serial_nos.remove(serial_no)

			elif row.serial_nos:
				for serial_no in get_serial_nos(row.serial_nos):
					if serial_no in item_data.serial_nos:
						item_data.serial_nos.remove(serial_no)

	return available_materials


def get_stock_entry_data(work_order, stock_entry_doc=None):
	from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
		get_voucher_wise_serial_batch_from_bundle,
	)

	stock_entry = frappe.qb.DocType("Stock Entry")
	stock_entry_detail = frappe.qb.DocType("Stock Entry Detail")

	data = (
		frappe.qb.from_(stock_entry)
		.from_(stock_entry_detail)
		.select(
			stock_entry_detail.item_name,
			stock_entry_detail.original_item,
			stock_entry_detail.item_code,
			stock_entry_detail.qty,
			(stock_entry_detail.t_warehouse).as_("warehouse"),
			(stock_entry_detail.s_warehouse).as_("s_warehouse"),
			stock_entry_detail.description,
			stock_entry_detail.stock_uom,
			stock_entry_detail.expense_account,
			stock_entry_detail.cost_center,
			stock_entry_detail.serial_and_batch_bundle,
			stock_entry_detail.batch_no,
			stock_entry_detail.serial_no,
			stock_entry.purpose,
			stock_entry.name,
		)
		.where(
			(stock_entry.name == stock_entry_detail.parent)
			& (stock_entry.work_order == work_order)
			& (stock_entry.docstatus == 1)
		)
		.orderby(stock_entry.creation, stock_entry_detail.item_code, stock_entry_detail.idx)
	)

	if stock_entry_doc and stock_entry_doc.purpose == "Disassemble":
		data = data.where(
			stock_entry.purpose.isin(
				[
					"Disassemble",
					"Manufacture",
				]
			)
		)

		data = data.where(stock_entry.name != stock_entry_doc.name)
	else:
		data = data.where(
			stock_entry.purpose.isin(
				[
					"Manufacture",
					"Material Consumption for Manufacture",
					"Material Transfer for Manufacture",
				]
			)
		)

		data = data.where(stock_entry_detail.s_warehouse.isnotnull())

	data = data.run(as_dict=1)

	if not data:
		return []

	voucher_nos = [row.get("name") for row in data if row.get("name")]
	if voucher_nos:
		bundle_data = get_voucher_wise_serial_batch_from_bundle(voucher_no=voucher_nos)
		for row in data:
			key = (row.item_code, row.warehouse, row.name)
			if row.purpose != "Material Transfer for Manufacture":
				key = (row.item_code, row.s_warehouse, row.name)

			if stock_entry_doc and stock_entry_doc.purpose == "Disassemble":
				key = (row.item_code, row.s_warehouse or row.warehouse, row.name)

			if bundle_data.get(key):
				row.update(bundle_data.get(key))

	return data

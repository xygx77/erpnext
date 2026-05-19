import json
from collections import defaultdict

import frappe
from frappe import _, bold
from frappe.query_builder.functions import Sum
from frappe.utils import ceil, cint, flt, get_link_to_form

from erpnext.manufacturing.doctype.bom.bom import add_additional_cost, get_backflush_based_on
from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos
from erpnext.stock.serial_batch_bundle import (
	SerialBatchCreation,
	get_batch_nos,
	get_empty_batches_based_work_order,
	get_serial_or_batch_items,
)

from .serial_batch import create_serial_and_batch_bundle


class BaseManufactureStockEntry:
	def __init__(self, se_doc):
		self.doc = se_doc

	def set_default_warehouse(self):
		for row in self.doc.items:
			if (
				not row.s_warehouse
				and self.doc.from_warehouse
				and not row.is_finished_item
				and not row.is_legacy_scrap_item
				and not row.type
			):
				row.s_warehouse = self.doc.from_warehouse
				row.t_warehouse = None

			elif (
				not row.t_warehouse
				and self.doc.to_warehouse
				and (row.is_finished_item or row.is_legacy_scrap_item or row.type)
			):
				row.t_warehouse = self.doc.to_warehouse
				row.s_warehouse = None

	def validate_warehouse(self):
		for row in self.doc.items:
			if not row.s_warehouse and not row.t_warehouse:
				frappe.throw(_("Source or Target Warehouse is required for item {0}").format(row.item_code))

	def validate_raw_materials_exists(self):
		if frappe.db.get_single_value("Manufacturing Settings", "material_consumption"):
			return

		raw_materials = []
		for row in self.doc.items:
			if row.s_warehouse:
				raw_materials.append(row.item_code)

		if not raw_materials:
			frappe.throw(
				_(
					"At least one raw material item must be present in the stock entry for the type {0}"
				).format(bold(self.doc.purpose)),
				title=_("Raw Materials Missing"),
			)

	@property
	def wo_doc(self):
		if not getattr(self, "_wo_doc", None):
			if self.doc.work_order:
				self._wo_doc = frappe.get_doc("Work Order", self.doc.work_order)
		return getattr(self, "_wo_doc", None)

	@property
	def backflush_based_on(self):
		return get_backflush_based_on(self.doc.bom_no)

	def get_item_dict(self, row):
		item_args = {}
		fields = [
			"item_code",
			"item_name",
			"item_group",
			"description",
			"uom",
			"stock_uom",
			"conversion_factor",
			"allow_alternative_item",
		]
		for field in fields:
			if row.get(field):
				item_args[field] = row.get(field)

		return item_args

	def add_secondary_items(self):
		secondary_items = get_secondary_items(self.doc.bom_no, self.doc.work_order)
		for row in secondary_items:
			item_args = self.get_item_dict(row)
			item_args["is_legacy_scrap_item"] = bool(row.get("is_legacy"))
			item_args["type"] = row.type
			item_args["bom_secondary_item"] = row.name

			if row.type == "Scrap" and self.wo_doc and self.wo_doc.get("scrap_warehouse"):
				item_args["t_warehouse"] = self.wo_doc.scrap_warehouse
			else:
				item_args["t_warehouse"] = self.doc.to_warehouse

			row.qty = row.qty * self.doc.fg_completed_qty
			if row.get("process_loss_per"):
				row.qty -= flt(
					row.qty * row.get("process_loss_per") / 100, self.doc.precision("fg_completed_qty")
				)

			item_args["qty"] = ceil_qty_if_uom_has_whole_number(row.qty, row.uom)
			item_args["transfer_qty"] = item_args["qty"]
			self.doc.append("items", item_args)

	def set_process_loss_qty(self):
		precision = self.doc.precision("process_loss_qty")
		if self.doc.work_order:
			data = frappe.get_all(
				"Work Order Operation",
				filters={"parent": self.doc.work_order},
				fields=[{"MAX": "process_loss_qty", "as": "process_loss_qty"}],
			)

			if data and data[0].process_loss_qty:
				process_loss_qty = data[0].process_loss_qty
				if flt(self.doc.process_loss_qty, precision) != flt(process_loss_qty, precision):
					self.doc.process_loss_qty = flt(process_loss_qty, precision)

					frappe.msgprint(
						_("The Process Loss Qty has reset as per job cards Process Loss Qty"), alert=True
					)

		if not self.doc.process_loss_percentage and not self.doc.process_loss_qty:
			self.doc.process_loss_percentage = frappe.get_cached_value(
				"BOM", self.doc.bom_no, "process_loss_percentage"
			)

		if self.doc.process_loss_percentage and not self.doc.process_loss_qty:
			self.doc.process_loss_qty = flt(
				(flt(self.doc.fg_completed_qty) * flt(self.doc.process_loss_percentage)) / 100
			)
		elif self.doc.process_loss_qty and not self.doc.process_loss_percentage:
			self.doc.process_loss_percentage = flt(
				(flt(self.doc.process_loss_qty) / flt(self.doc.fg_completed_qty)) * 100
			)

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
		item_details = self.get_production_item_details()
		fg_item_qty = flt(self.doc.fg_completed_qty) - flt(self.doc.process_loss_qty)

		item_details.update(
			{
				"conversion_factor": 1,
				"uom": item_details.stock_uom,
				"qty": ceil_qty_if_uom_has_whole_number(fg_item_qty, item_details.stock_uom),
				"t_warehouse": self.doc.to_warehouse,
				"s_warehouse": None,
				"is_finished_item": 1,
			}
		)

		item_details["item_code"] = item_details["name"]
		del item_details["name"]

		item_details["transfer_qty"] = item_details["qty"]

		if self.wo_doc and cint(
			frappe.db.get_single_value(
				"Manufacturing Settings", "make_serial_no_batch_from_work_order", cache=True
			)
		):
			if self.wo_doc.has_serial_no:
				self.set_serial_nos_for_finished_good(item_details)
			elif self.wo_doc.has_batch_no:
				self.set_batchwise_finished_goods(item_details)
		else:
			self.doc.append("items", item_details)

	def set_serial_nos_for_finished_good(self, item_details):
		serial_nos = self.get_available_serial_nos_for_fg(item_details.item_code)
		if serial_nos:
			row = frappe._dict({"serial_nos": serial_nos[0 : cint(item_details.qty)]})

			_id = create_serial_and_batch_bundle(
				self.doc,
				row,
				frappe._dict(
					{
						"item_code": item_details.item_code,
						"warehouse": item_details.t_warehouse,
					}
				),
			)

			item_details.serial_and_batch_bundle = _id
			item_details.use_serial_batch_fields = 0

			self.doc.append("items", item_details)

	def get_available_serial_nos_for_fg(self, item_code) -> list[str]:
		return frappe.get_all(
			"Serial No",
			filters={
				"item_code": item_code,
				"warehouse": ("is", "not set"),
				"status": "Inactive",
				"work_order": self.wo_doc.name,
			},
			pluck="name",
			order_by="creation asc",
		)

	def set_batchwise_finished_goods(self, item_details):
		batches = get_empty_batches_based_work_order(self.doc.work_order, self.doc.pro_doc.production_item)

		if not batches:
			self.doc.append("items", item_details)
		else:
			self.add_batchwise_finished_good(batches, item_details)

	def add_batchwise_finished_good(self, batches, item_details):
		qty = flt(self.doc.fg_completed_qty)
		row = frappe._dict({"batches_to_be_consume": defaultdict(float)})

		self.update_batches_to_be_consume(batches, row, qty)

		if not row.batches_to_be_consume:
			return

		_id = create_serial_and_batch_bundle(
			self.doc,
			row,
			frappe._dict(
				{
					"item_code": self.wo_doc.production_item,
					"warehouse": item_details.get("t_warehouse"),
				}
			),
		)

		item_details["serial_and_batch_bundle"] = _id
		self.doc.append("items", item_details)

	def update_batches_to_be_consume(self, batches, row, qty):
		qty_to_be_consumed = qty
		batches = sorted(batches.items(), key=lambda x: x[0])

		for batch_no, batch_qty in batches:
			if qty_to_be_consumed <= 0 or batch_qty <= 0:
				continue

			if batch_qty > qty_to_be_consumed:
				batch_qty = qty_to_be_consumed

			row.batches_to_be_consume[batch_no] += batch_qty

			if batch_no and row.serial_nos:
				serial_nos = self.get_serial_nos_based_on_transferred_batch(batch_no, row.serial_nos)
				serial_nos = serial_nos[0 : cint(batch_qty)]

				# remove consumed serial nos from list
				for sn in serial_nos:
					row.serial_nos.remove(sn)

			if "batch_details" in row:
				row.batch_details[batch_no] -= batch_qty

			qty_to_be_consumed -= batch_qty


class ManufactureStockEntry(BaseManufactureStockEntry):
	def before_validate(self):
		self.set_default_warehouse()
		self.set_job_card_data()

	def validate(self):
		self.validate_warehouse()
		self.validate_raw_materials_exists()
		self.validate_component_and_quantities()

	def set_job_card_data(self):
		if self.doc.job_card and not self.doc.work_order:
			data = frappe.db.get_value(
				"Job Card",
				self.doc.job_card,
				["for_quantity", "work_order", "bom_no", "semi_fg_bom"],
				as_dict=1,
			)
			self.doc.fg_completed_qty = data.for_quantity
			self.doc.work_order = data.work_order
			self.doc.from_bom = 1
			self.doc.bom_no = data.semi_fg_bom or data.bom_no

	def validate_component_and_quantities(self):
		if not frappe.db.get_single_value("Manufacturing Settings", "validate_components_quantities_per_bom"):
			return

		if not self.doc.fg_completed_qty:
			return

		rm_items = [item for item in self.doc.items if item.s_warehouse]
		if not rm_items:
			return

		precision = frappe.get_precision("Stock Entry Detail", "qty")
		bom_items = get_bom_items(self.doc.bom_no, self.doc.use_multi_level_bom)

		for row in bom_items:
			row.qty = row.qty * self.doc.fg_completed_qty
			if matched_item := self.get_matched_items(row.item_code):
				if flt(row.qty, precision) != flt(matched_item.qty, precision):
					frappe.throw(
						_(
							"For the item {0}, the consumed quantity should be {1} according to the BOM {2}."
						).format(
							bold(row.item_code),
							flt(row.qty),
							get_link_to_form("BOM", self.doc.bom_no),
						),
						title=_("Incorrect Component Quantity"),
					)
			else:
				frappe.throw(
					_("According to the BOM {0}, the Item '{1}' is missing in the stock entry.").format(
						get_link_to_form("BOM", self.doc.bom_no), bold(row.item_code)
					),
					title=_("Missing Item"),
				)

	def get_matched_items(self, item_code):
		items = [item for item in self.doc.items if item.s_warehouse]
		for row in items:
			if row.item_code == item_code or row.original_item == item_code:
				return row

		return {}

	def validate_work_order(self):
		if not self.doc.work_order:
			frappe.throw(_("Work Order is mandatory"))

	def add_items(self):
		self.add_raw_materials()
		self.set_process_loss_qty()
		self.add_finished_goods()
		self.add_secondary_items()
		self.add_additional_cost()
		self.add_secondary_items_from_job_card()

	def add_raw_materials(self):
		if not frappe.db.get_single_value("Manufacturing Settings", "material_consumption"):
			if self.backflush_based_on == "BOM" or self.wo_doc.skip_transfer:
				self.add_raw_materials_based_on_work_order()
			else:
				self.add_raw_materials_based_on_transfer()
		elif self.backflush_based_on == "BOM":
			self.add_unconsumed_raw_materials()
		else:
			self.add_raw_materials_based_on_transfer()

	def add_unconsumed_raw_materials(self):
		wo = self.wo_doc
		if not wo:
			return

		work_order_qty = flt(wo.material_transferred_for_manufacturing) or flt(wo.qty)
		wo_qty_to_produce = work_order_qty - flt(wo.produced_qty)

		for item in wo.get("required_items"):
			wo_item_qty = flt(item.transferred_qty) or flt(item.required_qty)
			wo_qty_unconsumed = wo_item_qty - flt(item.consumed_qty)
			bom_qty_per_unit = flt(item.required_qty) / flt(wo.qty)

			req_qty_each = wo_qty_unconsumed / (wo_qty_to_produce or 1)
			req_qty_each = min(req_qty_each, bom_qty_per_unit)

			qty = req_qty_each * flt(self.doc.fg_completed_qty)
			if qty <= 0:
				continue

			item_args = self.get_item_dict(item)
			item_args.update(
				{
					"conversion_factor": 1,
					"s_warehouse": wo.wip_warehouse or item.source_warehouse,
					"uom": item.stock_uom,
					"qty": ceil_qty_if_uom_has_whole_number(qty, item.stock_uom),
				}
			)
			item_args["transfer_qty"] = item_args["qty"]
			self.doc.append("items", item_args)

	def add_raw_materials_based_on_work_order(self):
		bom_items = (
			self.wo_doc.get("required_items")
			if self.wo_doc
			else get_bom_items(self.doc.bom_no, self.doc.use_multi_level_bom)
		)
		alternative_items = self.get_alternative_items(bom_items)

		for row in bom_items:
			item_args = self.get_item_dict(row)
			warehouse = self.doc.from_warehouse
			if not warehouse:
				if self.wo_doc.from_wip_warehouse:
					warehouse = self.wo_doc.wip_warehouse
				else:
					warehouse = row.get("source_warehouse")

			item_args.update(
				{
					"conversion_factor": 1,
					"item_group": row.get("item_group"),
					"s_warehouse": warehouse,
					"uom": row.stock_uom,
				}
			)

			if self.wo_doc:
				qty = (row.required_qty / self.wo_doc.qty) * self.doc.fg_completed_qty
			else:
				qty = flt(row.qty) * self.doc.fg_completed_qty

			item_args["qty"] = ceil_qty_if_uom_has_whole_number(qty, row.stock_uom)
			item_args["transfer_qty"] = item_args["qty"]

			if alternative_item_details := alternative_items.get(row.item_code):
				self.set_alternative_item_details(item_args, alternative_item_details)

			self.doc.append("items", item_args)

	def get_alternative_items(self, bom_items):
		doctype = frappe.qb.DocType("Stock Entry")
		child_doc = frappe.qb.DocType("Stock Entry Detail")

		query = (
			frappe.qb.from_(child_doc)
			.inner_join(doctype)
			.on(child_doc.parent == doctype.name)
			.select(
				child_doc.item_code,
				child_doc.uom,
				child_doc.stock_uom,
				child_doc.conversion_factor,
				child_doc.item_name,
				child_doc.item_group,
				child_doc.description,
				child_doc.original_item,
			)
			.where(
				(doctype.work_order == self.doc.work_order)
				& (doctype.purpose == "Material Transfer for Manufacture")
				& (doctype.docstatus == 1)
			)
		)

		item_codes_in_bom = [row.item_code for row in bom_items]
		if item_codes_in_bom:
			query = query.where(child_doc.original_item.isin(item_codes_in_bom))

		data = query.run(as_dict=1)
		if not data:
			return frappe._dict()

		alternative_items = frappe._dict()
		for row in data:
			alternative_items[row.original_item] = row
			alternative_items[row.original_item].original_item = None

		return alternative_items

	def set_alternative_item_details(self, row, alternative_item_details):
		if self.doc.work_order and row.get("allow_alternative_item") is None:
			row["allow_alternative_item"] = self.wo_doc.allow_alternative_item

		if row["allow_alternative_item"]:
			original_item = row["item_code"]
			row.update(alternative_item_details)
			row["original_item"] = original_item

	def add_raw_materials_based_on_transfer(self):
		self.prepare_available_materials_based_on_transfer()

		pending_qty_to_mfg = flt(self.wo_doc.material_transferred_for_manufacturing) - flt(
			self.wo_doc.produced_qty
		)

		if pending_qty_to_mfg <= 0 and not self.doc.get("is_return"):
			return

		for row in self.available_materials:
			row = self.available_materials[row]
			item_args = self.get_item_dict(row)
			if not self.doc.get("is_return"):
				qty = (flt(row.qty) * flt(self.doc.fg_completed_qty)) / pending_qty_to_mfg
			else:
				qty = row.qty

			item_args["qty"] = ceil_qty_if_uom_has_whole_number(qty, row.uom)
			item_args["transfer_qty"] = item_args["qty"]

			if not self.doc.get("is_return"):
				item_args["t_warehouse"] = None
				item_args["s_warehouse"] = row.warehouse
			else:
				# In case of return, source and target warehouse will be swapped
				item_args["s_warehouse"] = row.s_warehouse
				item_args["t_warehouse"] = row.t_warehouse

			if row.serial_nos or row.batches:
				self.assign_serial_batches_to_materials(item_args, row, qty)
			else:
				self.doc.append("items", item_args)

	def assign_serial_batches_to_materials(self, item_args, row, qty):
		if row.serial_nos:
			if serial_nos := row.serial_nos[0 : cint(qty)]:
				item_args["serial_no"] = "\n".join(serial_nos)

			if not item_args["uom"]:
				item_args["uom"] = row.stock_uom

			item_args["use_serial_batch_fields"] = 1
			self.doc.append("items", item_args)
		elif row.batches and len(row.batches) == 1:
			item_args["batch_no"] = next(iter(row.batches.keys()))
			if not item_args["uom"]:
				item_args["uom"] = row.stock_uom

			item_args["use_serial_batch_fields"] = 1
			self.doc.append("items", item_args)
		elif row.batches:
			self.split_items_based_on_batches(qty, item_args, row)

	def split_items_based_on_batches(self, qty, item_args, row):
		for batch_no, batch_qty in row.batches.items():
			if qty <= 0:
				return

			if batch_qty >= qty:
				item_args["qty"] = qty
				qty = 0
			else:
				item_args["qty"] = batch_qty
				qty -= batch_qty

			row.batches[batch_no] -= batch_qty
			if not item_args["uom"]:
				item_args["uom"] = row.stock_uom

			item_args["batch_no"] = batch_no
			item_args["transfer_qty"] = item_args["qty"]
			item_args["use_serial_batch_fields"] = 1

			self.doc.append("items", item_args)

	def prepare_available_materials_based_on_transfer(self):
		self.available_materials = frappe._dict()
		self._transfer_entries = self.get_transfer_entries()
		if not self._transfer_entries:
			return

		self.add_materials_from_transfer()
		self._consumption_entries = self.get_consumption_entries()
		if not self._consumption_entries:
			return

		self.remove_consumed_materials_from_available()

	def return_available_materials_in_source_wh(self):
		for row in self.doc.items:
			row.s_warehouse, row.t_warehouse = row.t_warehouse, row.s_warehouse

	def get_transfer_entries(self):
		stock_entry = frappe.qb.DocType("Stock Entry")
		stock_entry_detail = frappe.qb.DocType("Stock Entry Detail")

		return (
			frappe.qb.from_(stock_entry)
			.inner_join(stock_entry_detail)
			.on(stock_entry.name == stock_entry_detail.parent)
			.select(stock_entry_detail.star)
			.where(
				(stock_entry.work_order == self.doc.work_order)
				& (stock_entry.purpose == "Material Transfer for Manufacture")
				& (stock_entry.docstatus == 1)
			)
			.orderby(stock_entry_detail.idx)
		).run(as_dict=1)

	def add_materials_from_transfer(self):
		for row in self._transfer_entries:
			row.warehouse = row.t_warehouse
			key = (row.item_code, row.warehouse)
			if key not in self.available_materials:
				self.available_materials[key] = frappe._dict(row)
			else:
				self.available_materials[key].qty += row.qty

			if row.serial_and_batch_bundle:
				self.available_materials[key].update(self.get_sabb_details(row.serial_and_batch_bundle))

	def get_consumption_entries(self):
		stock_entry = frappe.qb.DocType("Stock Entry")
		stock_entry_detail = frappe.qb.DocType("Stock Entry Detail")

		return (
			frappe.qb.from_(stock_entry)
			.inner_join(stock_entry_detail)
			.on(stock_entry.name == stock_entry_detail.parent)
			.select(stock_entry_detail.star)
			.where(
				(stock_entry.work_order == self.doc.work_order)
				& (stock_entry_detail.s_warehouse.isnotnull())
				& (stock_entry.purpose == "Manufacture")
				& (stock_entry.docstatus == 1)
			)
			.orderby(stock_entry_detail.idx)
		).run(as_dict=1)

	def remove_consumed_materials_from_available(self):
		for row in self._consumption_entries:
			row.warehouse = row.s_warehouse
			key = (row.item_code, row.warehouse)
			self.available_materials[key].qty -= row.qty
			if row.serial_and_batch_bundle:
				_details = self.get_sabb_details(row.serial_and_batch_bundle)
				if _details.serial_nos:
					for sn in _details.serial_nos:
						self.available_materials[key].serial_nos.remove(sn)
				elif _details.batches:
					# Qty is in negative therefore added insted of subtraction
					for batch_no, qty in _details.batches.items():
						self.available_materials[key].batches[batch_no] += qty

	def add_additional_cost(self):
		if not self.wo_doc:
			return

		add_additional_cost(self.doc, self.wo_doc)

	def add_secondary_items_from_job_card(self):
		if not self.wo_doc:
			return

		secondary_items = self.get_secondary_items_from_job_card()
		for row in secondary_items:
			row.uom = row.uom or row.stock_uom
			row.qty = ceil_qty_if_uom_has_whole_number(row.stock_qty, row.stock_uom)
			row.transfer_qty = row.qty
			row.s_warehouse = None
			row.t_warehouse = row.warehouse or self.doc.to_warehouse
			row.is_legacy_scrap_item = row.is_legacy
			row.type = row.get("type")

			self.doc.append("items", row)

	def get_secondary_items_from_job_card(self):
		if not self.wo_doc.operations:
			return []

		secondary_items = get_secondary_items_from_job_card(self.doc.work_order, self.doc.job_card)
		if self.doc.job_card:
			pending_qty = flt(self.doc.fg_completed_qty)
		else:
			pending_qty = flt(self.get_completed_job_card_qty()) - flt(self.wo_doc.produced_qty)

		used_secondary_items = self.get_used_secondary_items()
		for row in secondary_items:
			row.stock_qty -= flt(used_secondary_items.get(row.item_code))
			row.stock_qty = (row.stock_qty) * flt(self.doc.fg_completed_qty) / flt(pending_qty)

			if used_secondary_items.get(row.item_code):
				used_secondary_items[row.item_code] -= row.stock_qty

		return secondary_items

	def get_used_secondary_items(self):
		used_secondary_items = defaultdict(float)

		StockEntry = frappe.qb.DocType("Stock Entry")
		StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")
		data = (
			frappe.qb.from_(StockEntry)
			.inner_join(StockEntryDetail)
			.on(StockEntryDetail.parent == StockEntry.name)
			.select(StockEntryDetail.item_code, StockEntryDetail.qty)
			.where(
				(StockEntry.work_order == self.doc.work_order)
				& ((StockEntryDetail.type.isnotnull()) | (StockEntryDetail.is_legacy_scrap_item == 1))
				& (StockEntry.docstatus == 1)
				& (StockEntry.purpose.isin(["Repack", "Manufacture"]))
			)
		).run(as_dict=1)

		for row in data:
			used_secondary_items[row.item_code] += row.qty

		return used_secondary_items

	def get_completed_job_card_qty(self):
		return flt(min([d.completed_qty for d in self.wo_doc.operations]))

	def get_sabb_details(self, sabb):
		sabb_entries = frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": sabb, "docstatus": 1, "is_cancelled": 0},
			fields=["serial_no", "batch_no", "qty"],
			order_by="idx",
		)

		serial_nos = []
		batches = defaultdict(float)

		for row in sabb_entries:
			if row.serial_no:
				serial_nos.append(row.serial_no)
			else:
				batches[row.batch_no] += row.qty

		return frappe._dict({"serial_nos": serial_nos, "batches": batches})

	def on_submit(self):
		self.update_job_card_and_work_order()

	def on_cancel(self):
		self.update_job_card_and_work_order()

	def update_job_card_and_work_order(self):
		def _validate_work_order(pro_doc):
			msg, title = "", ""
			if flt(pro_doc.docstatus) != 1:
				msg = _("Work Order {0} must be submitted").format(self.doc.work_order)

			if pro_doc.status == "Stopped":
				msg = _("Transaction not allowed against stopped Work Order {0}").format(self.doc.work_order)

			if msg:
				frappe.throw(_(msg), title=title)

		if self.doc.job_card:
			job_doc = frappe.get_doc("Job Card", self.doc.job_card)
			job_doc.set_consumed_qty_in_job_card_item(self.doc)
			job_doc.set_manufactured_qty()
			job_doc.update_work_order()

		if self.doc.work_order:
			_validate_work_order(self.wo_doc)

			if self.doc.fg_completed_qty:
				self.wo_doc.run_method("update_work_order_qty")
				self.wo_doc.run_method("update_planned_qty")

			self.wo_doc.run_method("update_status")
			if not self.wo_doc.operations:
				self.wo_doc.set_actual_dates()


class RepackStockEntry(BaseManufactureStockEntry):
	def before_validate(self):
		self.set_default_warehouse()

	def validate(self):
		self.validate_raw_materials_exists()
		self.validate_repack_entry()

	def validate_repack_entry(self):
		fg_items = {row.item_code: row for row in self.doc.items if row.is_finished_item}

		if len(fg_items) > 1 and not all(row.set_basic_rate_manually for row in fg_items.values()):
			frappe.throw(
				_(
					"When there are multiple finished goods ({0}) in a Repack stock entry, the basic rate for all finished goods must be set manually. To set rate manually, enable the checkbox 'Set Basic Rate Manually' in the respective finished good row."
				).format(", ".join(fg_items)),
				title=_("Set Basic Rate Manually"),
			)

	def add_items(self):
		self.add_raw_materials_based_on_bom()
		self.set_process_loss_qty()
		self.add_finished_goods()
		self.add_secondary_items()

	def add_raw_materials_based_on_bom(self):
		bom_items = get_bom_items(self.doc.bom_no, self.doc.use_multi_level_bom)

		for row in bom_items:
			row.s_warehouse = self.doc.from_warehouse
			row.qty = row.qty * self.doc.fg_completed_qty
			row.transfer_qty = row.qty
			if not row.uom:
				row.uom = row.stock_uom

			self.doc.append("items", row)


class MaterialConsumptionForManufactureStockEntry(ManufactureStockEntry):
	def before_validate(self):
		self.set_default_warehouse()

	def validate(self):
		self.validate_work_order()

	def add_items(self):
		if self.backflush_based_on == "BOM" or self.wo_doc.skip_transfer:
			self.add_raw_materials_based_on_work_order()
		else:
			self.add_raw_materials_based_on_transfer()


def get_bom_items(bom_no, use_multi_level_bom=None, qty=None, fetch_secondary_items=False):
	if use_multi_level_bom is None:
		use_multi_level_bom = frappe.get_cached_value("BOM", bom_no, "use_multi_level_bom")

	if qty is None:
		qty = 1

	table_name = "BOM Item"
	if use_multi_level_bom:
		table_name = "BOM Explosion Item"

	if fetch_secondary_items:
		table_name = "BOM Secondary Item"

	bom_doc = frappe.qb.DocType("BOM")
	doctype = frappe.qb.DocType(table_name)

	query = (
		frappe.qb.from_(doctype)
		.inner_join(bom_doc)
		.on(doctype.parent == bom_doc.name)
		.select(
			doctype.item_code,
			doctype.item_name,
			doctype.stock_uom,
			doctype.description,
			(doctype.stock_qty / bom_doc.quantity.as_("qty") * qty).as_("qty"),
			doctype.rate.as_("basic_rate"),
		)
		.where((bom_doc.name == bom_no) & (bom_doc.docstatus == 1))
		.orderby(doctype.idx)
	)

	if table_name == "BOM Secondary Item":
		query = query.select(
			doctype.name,
			doctype.cost_allocation_per,
			doctype.uom,
			doctype.process_loss_per,
			doctype.type,
			doctype.is_legacy,
			doctype.conversion_factor,
		)
	elif table_name == "BOM Item":
		query = query.select(
			doctype.allow_alternative_item, doctype.uom, doctype.conversion_factor, doctype.bom_no
		)

	items = query.run(as_dict=1)
	item_dict = {}
	for item in items:
		if item.item_code in item_dict:
			item_dict[item.item_code].qty += item.qty
		else:
			item_dict[item.item_code] = item

	return list(item_dict.values())


def get_secondary_items(bom_no, work_order=None):
	if (
		frappe.db.get_single_value(
			"Manufacturing Settings", "set_op_cost_and_secondary_items_from_sub_assemblies"
		)
		and work_order
		and frappe.get_cached_value("Work Order", work_order, "use_multi_level_bom")
	):
		return get_secondary_items_from_sub_assemblies(bom_no)
	else:
		return get_bom_items(bom_no, fetch_secondary_items=True)


def get_secondary_items_from_sub_assemblies(bom_no):
	items = []
	bom_items = get_bom_items(bom_no)
	for row in bom_items:
		if not row.bom_no:
			continue

		items.extend(get_bom_items(row.bom_no, qty=row.qty, fetch_secondary_items=True))
		items.extend(get_secondary_items_from_sub_assemblies(row.bom_no))

	return items


def get_secondary_items_from_job_card(work_order, jc_name=None):
	job_card = frappe.qb.DocType("Job Card")
	job_card_secondary_item = frappe.qb.DocType("Job Card Secondary Item")

	secondary_items = (
		frappe.qb.from_(job_card)
		.select(
			Sum(job_card_secondary_item.stock_qty).as_("stock_qty"),
			job_card_secondary_item.item_code,
			job_card_secondary_item.item_name,
			job_card_secondary_item.description,
			job_card_secondary_item.stock_uom,
			job_card_secondary_item.type,
			job_card_secondary_item.bom_secondary_item,
		)
		.join(job_card_secondary_item)
		.on(job_card_secondary_item.parent == job_card.name)
		.where(
			(job_card_secondary_item.item_code.isnotnull())
			& (job_card.work_order == work_order)
			& (job_card.docstatus == 1)
		)
		.groupby(job_card_secondary_item.item_code, job_card_secondary_item.type)
		.orderby(job_card_secondary_item.idx)
	)

	if jc_name:
		secondary_items = secondary_items.where(job_card.name == jc_name)

	return secondary_items.run(as_dict=1)


def ceil_qty_if_uom_has_whole_number(qty, stock_uom):
	if cint(frappe.get_cached_value("UOM", stock_uom, "must_be_whole_number")):
		qty = ceil(qty)

	return qty


@frappe.whitelist()
def move_sample_to_retention_warehouse(company: str, items: str | list):
	if isinstance(items, str):
		items = json.loads(items)

	retention_warehouse = frappe.get_single_value("Stock Settings", "sample_retention_warehouse")
	stock_entry = frappe.new_doc("Stock Entry")
	stock_entry.company = company
	stock_entry.purpose = "Material Transfer"
	stock_entry.set_stock_entry_type()
	for item in items:
		if item.get("sample_quantity") and item.get("serial_and_batch_bundle"):
			warehouse = item.get("t_warehouse") or item.get("warehouse")
			total_qty = 0
			cls_obj = SerialBatchCreation(
				{
					"type_of_transaction": "Outward",
					"serial_and_batch_bundle": item.get("serial_and_batch_bundle"),
					"item_code": item.get("item_code"),
					"warehouse": warehouse,
					"do_not_save": True,
				}
			)
			sabb = cls_obj.duplicate_package()
			batches = get_batch_nos(item.get("serial_and_batch_bundle"))
			sabe_list = []
			for batch_no in batches.keys():
				sample_quantity = validate_sample_quantity(
					item.get("item_code"),
					item.get("sample_quantity"),
					item.get("transfer_qty") or item.get("qty"),
					batch_no,
				)

				sabe = next(item for item in sabb.entries if item.batch_no == batch_no)
				if sample_quantity:
					if sabb.has_serial_no:
						new_sabe = [
							entry
							for entry in sabb.entries
							if entry.batch_no == batch_no
							and frappe.db.exists(
								"Serial No", {"name": entry.serial_no, "warehouse": warehouse}
							)
						][: int(sample_quantity)]
						sabe_list.extend(new_sabe)
						total_qty += len(new_sabe)
					else:
						total_qty += sample_quantity
						sabe.qty = sample_quantity
				else:
					sabb.entries.remove(sabe)

			if total_qty:
				if sabe_list:
					sabb.entries = sabe_list
				sabb.save()

				stock_entry.append(
					"items",
					{
						"item_code": item.get("item_code"),
						"s_warehouse": warehouse,
						"t_warehouse": retention_warehouse,
						"qty": total_qty,
						"basic_rate": item.get("valuation_rate"),
						"uom": item.get("uom"),
						"stock_uom": item.get("stock_uom"),
						"conversion_factor": item.get("conversion_factor") or 1.0,
						"serial_and_batch_bundle": sabb.name,
					},
				)
	if stock_entry.get("items"):
		return stock_entry.as_dict()


@frappe.whitelist()
def validate_sample_quantity(item_code: str, sample_quantity: int, qty: float, batch_no: str | None = None):
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	if cint(qty) < cint(sample_quantity):
		frappe.throw(
			_("Sample quantity {0} cannot be more than received quantity {1}").format(sample_quantity, qty)
		)
	retention_warehouse = frappe.get_single_value("Stock Settings", "sample_retention_warehouse")
	retainted_qty = 0
	if batch_no:
		retainted_qty = get_batch_qty(batch_no, retention_warehouse, item_code)
	max_retain_qty = frappe.get_value("Item", item_code, "sample_quantity")
	if retainted_qty >= max_retain_qty:
		frappe.msgprint(
			_(
				"Maximum Samples - {0} have already been retained for Batch {1} and Item {2} in Batch {3}."
			).format(retainted_qty, batch_no, item_code, batch_no),
			alert=True,
		)
		sample_quantity = 0
	qty_diff = max_retain_qty - retainted_qty
	if cint(sample_quantity) > cint(qty_diff):
		frappe.msgprint(
			_("Maximum Samples - {0} can be retained for Batch {1} and Item {2}.").format(
				max_retain_qty, batch_no, item_code
			),
			alert=True,
		)
		sample_quantity = qty_diff

	return sample_quantity

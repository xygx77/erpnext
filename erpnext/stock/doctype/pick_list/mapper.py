# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import json
from itertools import groupby

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import map_child_doc
from frappe.utils import flt, get_link_to_form

from erpnext.selling.doctype.sales_order.mapper import (
	make_delivery_note as create_delivery_note_from_sales_order,
)


def validate_item_locations(pick_list):
	if not pick_list.locations:
		frappe.throw(_("Add items in the Item Locations table"))


@frappe.whitelist()
def create_delivery_note(source_name: str, target_doc: str | Document | None = None):
	pick_list = frappe.get_doc("Pick List", source_name)
	validate_item_locations(pick_list)
	sales_dict = dict()
	sales_orders = []
	delivery_notes = []
	for location in pick_list.locations:
		if location.sales_order:
			sales_orders.append(
				frappe.db.get_value(
					"Sales Order",
					location.sales_order,
					[
						"customer",
						"name as sales_order",
						"company_address",
						"dispatch_address_name",
						"shipping_address_name",
						"customer_address",
					],
					as_dict=True,
				)
			)

	group_key = lambda so: (  # noqa
		so["customer"],
		so["company_address"] or "",
		so["dispatch_address_name"] or "",
		so["shipping_address_name"] or "",
		so["customer_address"] or "",
	)
	for key, rows in groupby(sorted(sales_orders, key=group_key), key=group_key):
		sales_dict[key] = {row.sales_order for row in rows}

	if sales_dict:
		delivery_notes.extend(create_dn_with_so(sales_dict, pick_list))

	if not all(item.sales_order for item in pick_list.locations):
		delivery_notes.append(create_dn_wo_so(pick_list))

	if len(delivery_notes) == 1:
		return delivery_notes[0]
	else:
		from frappe.utils import comma_and

		doc_list = [get_link_to_form("Delivery Note", p.name) for p in delivery_notes]
		frappe.msgprint(_("{0} created").format(comma_and(doc_list)))


def create_dn_wo_so(pick_list, delivery_note=None):
	if not delivery_note:
		delivery_note = frappe.new_doc("Delivery Note")

	delivery_note.company = pick_list.company

	item_table_mapper_without_so = {
		"doctype": "Delivery Note Item",
		"field_map": {
			"rate": "rate",
			"name": "name",
			"parent": "",
		},
	}
	map_pl_locations(pick_list, item_table_mapper_without_so, delivery_note)
	delivery_note.flags.ignore_mandatory = True
	delivery_note.save()

	return delivery_note


@frappe.whitelist()
def create_dn_for_pick_lists(
	source_name: str, target_doc: str | Document | None = None, kwargs: dict | str | None = None
):
	"""Get Items from Multiple Pick Lists and create a Delivery Note for filtered customer"""
	if kwargs is None:
		kwargs = {}
	if isinstance(kwargs, str):
		kwargs = json.loads(kwargs)

	pick_list = frappe.get_doc("Pick List", source_name)
	validate_item_locations(pick_list)

	sales_order_arg = kwargs.get("sales_order")
	customer_arg = kwargs.get("customer")

	if sales_order_arg:
		sales_orders = {sales_order_arg}
	else:
		sales_orders = {row.sales_order for row in pick_list.locations if row.sales_order}

		if customer_arg:
			sales_orders = frappe.get_all(
				"Sales Order",
				filters={"customer": customer_arg, "name": ["in", list(sales_orders)]},
				pluck="name",
			)

	delivery_note = create_dn_from_so(pick_list, sales_orders, delivery_note=target_doc, kwargs=kwargs)

	if not sales_order_arg and not all(item.sales_order for item in pick_list.locations):
		if isinstance(delivery_note, str):
			delivery_note = frappe.get_doc(frappe.parse_json(delivery_note))

		delivery_note = create_dn_wo_so(pick_list, delivery_note)

	return delivery_note


def create_dn_with_so(sales_dict, pick_list):
	"""Create Delivery Note for each customer (based on SO) in a Pick List."""
	delivery_notes = []

	for key in sales_dict:
		delivery_note = create_dn_from_so(pick_list, sales_dict[key], None)
		if delivery_note:
			delivery_note.flags.ignore_mandatory = True
			# updates packed_items on save
			# save as multiple customers are possible
			delivery_note.save()
			delivery_notes.append(delivery_note)

	return delivery_notes


def create_dn_from_so(pick_list, sales_order_list, delivery_note=None, kwargs=None):
	if not sales_order_list:
		return delivery_note

	def select_item(d):
		filtered_items = kwargs.get("filtered_children", [])
		child_filter = d.name in filtered_items if filtered_items else True
		return child_filter

	item_table_mapper = {
		"doctype": "Delivery Note Item",
		"field_map": {
			"rate": "rate",
			"name": "so_detail",
			"parent": "against_sales_order",
		},
		"condition": lambda doc: abs(doc.delivered_qty) < abs(doc.qty)
		and doc.delivered_by_supplier != 1
		and select_item(doc),
	}

	kwargs = {"skip_item_mapping": True, "ignore_pricing_rule": pick_list.ignore_pricing_rule}

	delivery_note = create_delivery_note_from_sales_order(
		next(iter(sales_order_list)), delivery_note, kwargs=kwargs
	)

	if not delivery_note:
		return

	for so in sales_order_list:
		map_pl_locations(pick_list, item_table_mapper, delivery_note, so)

	return delivery_note


def map_pl_locations(pick_list, item_mapper, delivery_note, sales_order=None):
	for location in pick_list.locations:
		if location.sales_order != sales_order or location.product_bundle_item:
			continue

		if location.sales_order_item:
			sales_order_item = frappe.get_doc("Sales Order Item", location.sales_order_item)
		else:
			sales_order_item = None

		source_doc = sales_order_item or location

		dn_item = map_child_doc(source_doc, delivery_note, item_mapper)

		if dn_item:
			dn_item.against_pick_list = pick_list.name
			dn_item.pick_list_item = location.name
			dn_item.warehouse = location.warehouse
			dn_item.qty = flt(location.picked_qty - location.delivered_qty) / (
				flt(dn_item.conversion_factor) or 1
			)
			dn_item.batch_no = location.batch_no
			dn_item.serial_no = location.serial_no
			dn_item.use_serial_batch_fields = location.use_serial_batch_fields

			update_delivery_note_item(source_doc, dn_item, delivery_note)

	add_product_bundles_to_delivery_note(pick_list, delivery_note, item_mapper, sales_order)
	set_delivery_note_missing_values(delivery_note)

	delivery_note.company = pick_list.company
	if sales_order:
		delivery_note.customer = frappe.get_value("Sales Order", sales_order, "customer")


def add_product_bundles_to_delivery_note(pick_list, delivery_note, item_mapper, sales_order=None) -> None:
	"""Add product bundles found in pick list to delivery note.

	When mapping pick list items, the bundle item itself isn't part of the
	locations. Dynamically fetch and add parent bundle item into DN."""
	product_bundles = pick_list._get_product_bundles()
	product_bundle_qty_map = pick_list._get_product_bundle_qty_map(product_bundles.values())

	for so_row, value in product_bundles.items():
		sales_order_item = frappe.get_doc("Sales Order Item", so_row)
		if sales_order and sales_order_item.parent != sales_order:
			continue

		dn_bundle_item = map_child_doc(sales_order_item, delivery_note, item_mapper)
		dn_bundle_item.qty = pick_list._compute_picked_qty_for_bundle(
			so_row, product_bundle_qty_map[value.item_code]
		)
		dn_bundle_item.pick_list_item = value.pick_list_item
		dn_bundle_item.against_pick_list = pick_list.name
		update_delivery_note_item(sales_order_item, dn_bundle_item, delivery_note)


@frappe.whitelist()
def create_stock_entry(pick_list: str):
	pick_list = frappe.get_doc(json.loads(pick_list))
	validate_item_locations(pick_list)

	if stock_entry_exists(pick_list.get("name")):
		return frappe.msgprint(_("Stock Entry has been already created against this Pick List"))

	stock_entry = frappe.new_doc("Stock Entry")
	stock_entry.pick_list = pick_list.get("name")
	stock_entry.purpose = pick_list.get("purpose")
	stock_entry.company = pick_list.get("company")
	stock_entry.set_stock_entry_type()

	if pick_list.get("work_order"):
		stock_entry = update_stock_entry_based_on_work_order(pick_list, stock_entry)
	elif pick_list.get("material_request"):
		stock_entry = update_stock_entry_based_on_material_request(pick_list, stock_entry)
	else:
		stock_entry = update_stock_entry_items_with_no_reference(pick_list, stock_entry)

	stock_entry.set_missing_values()

	return stock_entry.as_dict()


def update_delivery_note_item(source, target, delivery_note):
	cost_center = frappe.db.get_value("Project", delivery_note.project, "cost_center")
	if not cost_center:
		cost_center = get_cost_center(source.item_code, "Item", delivery_note.company)

	if not cost_center:
		cost_center = get_cost_center(source.item_group, "Item Group", delivery_note.company)

	target.cost_center = cost_center


def get_cost_center(for_item, from_doctype, company):
	"""Returns Cost Center for Item or Item Group"""
	return frappe.db.get_value(
		"Item Default",
		fieldname=["buying_cost_center"],
		filters={"parent": for_item, "parenttype": from_doctype, "company": company},
	)


def set_delivery_note_missing_values(target):
	target.run_method("set_missing_values")
	target.run_method("set_po_nos")
	target.run_method("calculate_taxes_and_totals")


def stock_entry_exists(pick_list_name):
	return frappe.db.exists("Stock Entry", {"pick_list": pick_list_name})


def update_stock_entry_based_on_work_order(pick_list, stock_entry):
	work_order = frappe.get_doc("Work Order", pick_list.get("work_order"))

	stock_entry.work_order = work_order.name
	stock_entry.company = work_order.company
	stock_entry.from_bom = 1
	stock_entry.bom_no = work_order.bom_no
	stock_entry.use_multi_level_bom = work_order.use_multi_level_bom
	stock_entry.fg_completed_qty = pick_list.for_qty
	if work_order.bom_no:
		stock_entry.inspection_required = frappe.db.get_value("BOM", work_order.bom_no, "inspection_required")

	is_wip_warehouse_group = frappe.db.get_value("Warehouse", work_order.wip_warehouse, "is_group")
	if not (is_wip_warehouse_group and work_order.skip_transfer):
		wip_warehouse = work_order.wip_warehouse
	else:
		wip_warehouse = None
	stock_entry.to_warehouse = wip_warehouse

	stock_entry.project = work_order.project

	for location in pick_list.locations:
		item = frappe._dict()
		update_common_item_properties(item, location)
		item.t_warehouse = wip_warehouse

		stock_entry.append("items", item)

	return stock_entry


def update_stock_entry_based_on_material_request(pick_list, stock_entry):
	for location in pick_list.locations:
		target_warehouse = None
		if location.material_request_item:
			target_warehouse = frappe.get_value(
				"Material Request Item", location.material_request_item, "warehouse"
			)
		item = frappe._dict()
		update_common_item_properties(item, location)
		item.t_warehouse = target_warehouse
		stock_entry.append("items", item)

	return stock_entry


def update_stock_entry_items_with_no_reference(pick_list, stock_entry):
	for location in pick_list.locations:
		item = frappe._dict()
		update_common_item_properties(item, location)

		stock_entry.append("items", item)

	return stock_entry


def update_common_item_properties(item, location):
	item.item_code = location.item_code
	item.s_warehouse = location.warehouse
	item.transfer_qty = location.picked_qty
	item.qty = flt(location.picked_qty / (location.conversion_factor or 1), location.precision("qty"))
	item.uom = location.uom
	item.conversion_factor = location.conversion_factor
	item.stock_uom = location.stock_uom
	item.material_request = location.material_request
	item.serial_no = location.serial_no
	item.batch_no = location.batch_no
	item.material_request_item = location.material_request_item

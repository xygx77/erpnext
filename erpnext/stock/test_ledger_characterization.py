"""Phase 0 characterization tests for the stock_controller refactor.

These are golden-master snapshot tests: each scenario builds a representative
stock voucher, submits it, and compares its GL *and* Stock Ledger entries against
a stored snapshot (see ``erpnext/stock/ledger_snapshots``). They assert nothing
about *correct* accounting or valuation — only that ledger output stays
byte-identical as ``stock_controller`` is split into services.

Regenerate goldens after an intentional change::

    REGEN_LEDGER_SNAPSHOTS=1 bench run-tests --site test-erpnext-v17 \\
        --module erpnext.stock.test_ledger_characterization
"""

import frappe
from frappe.tests import IntegrationTestCase

from erpnext.stock.doctype.purchase_receipt.test_purchase_receipt import make_purchase_receipt
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.ledger_snapshot import assert_ledger_snapshot

POSTING_DATE = "2024-01-15"
CUSTOMER = "_Test Customer"
COMPANY = "_Test Company with perpetual inventory"
WAREHOUSE = "Stores - TCP1"


class TestLedgerCharacterization(IntegrationTestCase):
	def test_dn_basic(self):
		make_stock_entry(item_code="_Test Item", target=WAREHOUSE, qty=10, basic_rate=100)
		dn = _make_dated_delivery_note(qty=5, rate=150)
		dn.insert()
		dn.submit()
		assert_ledger_snapshot(self, "dn_basic", "Delivery Note", dn.name)

	def test_dn_return(self):
		make_stock_entry(item_code="_Test Item", target=WAREHOUSE, qty=10, basic_rate=100)
		original = _make_dated_delivery_note(qty=5, rate=150)
		original.insert()
		original.submit()

		ret = frappe.copy_doc(original)
		ret.is_return = 1
		ret.return_against = original.name
		for item in ret.items:
			item.qty = -item.qty
		ret.set_posting_time = 1
		ret.posting_date = POSTING_DATE
		ret.insert()
		ret.submit()
		assert_ledger_snapshot(self, "dn_return", "Delivery Note", ret.name)

	def test_se_material_receipt(self):
		se = make_stock_entry(
			item_code="_Test Item",
			target=WAREHOUSE,
			qty=5,
			basic_rate=100,
			company=COMPANY,
			posting_date=POSTING_DATE,
			do_not_submit=True,
		)
		se.submit()
		assert_ledger_snapshot(self, "se_material_receipt", "Stock Entry", se.name)

	def test_se_material_issue(self):
		make_stock_entry(item_code="_Test Item", target=WAREHOUSE, qty=10, basic_rate=100, company=COMPANY)
		se = make_stock_entry(
			item_code="_Test Item",
			source=WAREHOUSE,
			qty=5,
			company=COMPANY,
			posting_date=POSTING_DATE,
			do_not_submit=True,
		)
		se.submit()
		assert_ledger_snapshot(self, "se_material_issue", "Stock Entry", se.name)

	def test_se_material_transfer(self):
		make_stock_entry(item_code="_Test Item", target=WAREHOUSE, qty=10, basic_rate=100, company=COMPANY)
		se = make_stock_entry(
			item_code="_Test Item",
			source=WAREHOUSE,
			target="Finished Goods - TCP1",
			qty=5,
			company=COMPANY,
			posting_date=POSTING_DATE,
			do_not_submit=True,
		)
		se.submit()
		assert_ledger_snapshot(self, "se_material_transfer", "Stock Entry", se.name)

	def test_sr_basic(self):
		sr = _make_dated_stock_reconciliation(qty=10, rate=150)
		sr.insert()
		sr.submit()
		assert_ledger_snapshot(self, "sr_basic", "Stock Reconciliation", sr.name)

	def test_pr_basic(self):
		pr = make_purchase_receipt(
			company=COMPANY, warehouse=WAREHOUSE, posting_date=POSTING_DATE, qty=5, rate=100
		)
		assert_ledger_snapshot(self, "pr_basic", "Purchase Receipt", pr.name)

	def test_pr_with_taxes(self):
		pr = make_purchase_receipt(
			company=COMPANY,
			warehouse=WAREHOUSE,
			posting_date=POSTING_DATE,
			qty=5,
			rate=100,
			get_taxes_and_charges=True,
		)
		assert_ledger_snapshot(self, "pr_with_taxes", "Purchase Receipt", pr.name)

	def test_pr_return(self):
		from erpnext.stock.doctype.purchase_receipt.mapper import make_purchase_return

		original = make_purchase_receipt(
			company=COMPANY, warehouse=WAREHOUSE, posting_date=POSTING_DATE, qty=5, rate=100
		)
		ret = make_purchase_return(original.name)
		ret.posting_date = POSTING_DATE
		ret.set_posting_time = 1
		ret.insert()
		ret.submit()
		assert_ledger_snapshot(self, "pr_return", "Purchase Receipt", ret.name)


def _make_dated_delivery_note(**args) -> frappe.Document:
	"""Minimal Delivery Note on a fixed posting date using the perpetual-inventory
	test company.

	Inlined to avoid importing test_delivery_note which drags in conflicting
	test-record dependencies at discovery time."""
	dn = frappe.new_doc("Delivery Note")
	dn.company = COMPANY
	dn.customer = CUSTOMER
	dn.posting_date = POSTING_DATE
	dn.set_posting_time = 1
	dn.append(
		"items",
		{
			"item_code": args.get("item_code", "_Test Item"),
			"warehouse": args.get("warehouse", WAREHOUSE),
			"qty": args.get("qty", 1),
			"rate": args.get("rate", 100),
			"expense_account": "Cost of Goods Sold - TCP1",
			"cost_center": "Main - TCP1",
		},
	)
	return dn


def _make_dated_stock_reconciliation(**args) -> frappe.Document:
	"""Minimal Stock Reconciliation on a fixed posting date using the perpetual-inventory
	test company.

	Inlined to avoid importing test_stock_reconciliation which drags in conflicting
	test-record dependencies at discovery time."""
	sr = frappe.new_doc("Stock Reconciliation")
	sr.company = COMPANY
	sr.purpose = args.get("purpose", "Stock Reconciliation")
	sr.posting_date = POSTING_DATE
	sr.posting_time = "00:00:00"
	sr.set_posting_time = 1
	sr.expense_account = frappe.get_cached_value("Company", COMPANY, "stock_adjustment_account")
	sr.cost_center = frappe.get_cached_value("Company", COMPANY, "cost_center")
	sr.append(
		"items",
		{
			"item_code": args.get("item_code", "_Test Item"),
			"warehouse": args.get("warehouse", WAREHOUSE),
			"qty": args.get("qty", 10),
			"valuation_rate": args.get("rate", 100),
		},
	)
	return sr

"""Phase 0 characterization tests for the accounts/controller refactor.

These are golden-master snapshot tests: each scenario builds a representative
voucher, submits it, and compares its GL entries against a stored snapshot
(see ``erpnext/accounts/gl_snapshots``). They assert nothing about *correct*
accounting — only that GL output stays byte-identical as the GL pipeline is
refactored into composer / validator / sink services.

Regenerate goldens after an intentional change::

    REGEN_GL_SNAPSHOTS=1 bench run-tests --site test-erpnext-v17 \\
        --module erpnext.accounts.test_gl_characterization
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.classes.context_managers import change_settings

from erpnext.accounts.doctype.account.test_account import create_account
from erpnext.accounts.doctype.mode_of_payment.test_mode_of_payment import (
	set_default_account_for_mode_of_payment,
)
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.accounts.doctype.purchase_invoice.mapper import make_debit_note
from erpnext.accounts.doctype.purchase_invoice.test_purchase_invoice import make_purchase_invoice
from erpnext.accounts.doctype.sales_invoice.mapper import make_sales_return
from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
from erpnext.accounts.gl_snapshot import assert_gl_snapshot
from erpnext.stock.doctype.purchase_receipt.test_purchase_receipt import make_purchase_receipt
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

POSTING_DATE = "2024-01-15"
COMPANY = "_Test Company"
CUSTOMER = "_Test Customer"
WAREHOUSE = "_Test Warehouse - _TC"
DN_COMPANY = "_Test Company with perpetual inventory"
DN_WAREHOUSE = "Stores - TCP1"


def make_dated_purchase_invoice(**args):
	"""make_purchase_invoice ignores posting_date unless set_posting_time is on,
	which would make snapshots depend on the run date. Force the backdated time."""
	pi = make_purchase_invoice(do_not_save=True, **args)
	pi.set_posting_time = 1
	pi.posting_date = POSTING_DATE
	return pi


def make_dated_payment_entry(**args):
	"""Standalone Payment Entry (no invoice reference) on a fixed posting date.

	Mirrors test_payment_entry.create_payment_entry without importing that test
	module, whose import drags in test-record dependencies that conflict during
	discovery."""
	pe = frappe.new_doc("Payment Entry")
	pe.company = COMPANY
	pe.payment_type = args.get("payment_type") or "Pay"
	pe.party_type = args.get("party_type") or "Supplier"
	pe.party = args.get("party") or "_Test Supplier"
	pe.paid_from = args.get("paid_from") or "_Test Bank - _TC"
	pe.paid_to = args.get("paid_to") or "Creditors - _TC"
	pe.paid_amount = args.get("paid_amount") or 1000
	pe.setup_party_account_field()
	pe.set_missing_values()
	pe.set_exchange_rate()
	pe.received_amount = pe.paid_amount / pe.target_exchange_rate
	pe.reference_no = "Test001"
	pe.posting_date = POSTING_DATE
	pe.reference_date = POSTING_DATE
	return pe


def make_dated_journal_entry(accounts, multi_currency=0):
	"""Journal Entry on a fixed posting date built from explicit account rows.

	Inlined rather than importing test_journal_entry.make_journal_entry, whose
	import drags in test-record dependencies that conflict during discovery."""
	jv = frappe.new_doc("Journal Entry")
	jv.posting_date = POSTING_DATE
	jv.company = COMPANY
	jv.remark = "test"
	jv.multi_currency = multi_currency
	jv.set("accounts", accounts)
	return jv


class TestGLCharacterization(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for mode, account in (("Cash", "_Test Cash - _TC"), ("Bank Draft", "_Test Bank - _TC")):
			set_default_account_for_mode_of_payment(frappe.get_doc("Mode of Payment", mode), COMPANY, account)

	def test_si_basic(self):
		si = create_sales_invoice(posting_date=POSTING_DATE, qty=10, rate=100)
		assert_gl_snapshot(self, "si_basic", "Sales Invoice", si.name)

	def test_si_with_taxes(self):
		si = create_sales_invoice(posting_date=POSTING_DATE, qty=10, rate=100, do_not_save=True)
		si.append(
			"taxes",
			{
				"charge_type": "On Net Total",
				"account_head": "_Test Account Service Tax - _TC",
				"cost_center": "_Test Cost Center - _TC",
				"description": "Service Tax",
				"rate": 14,
			},
		)
		si.insert()
		si.submit()
		assert_gl_snapshot(self, "si_with_taxes", "Sales Invoice", si.name)

	def test_si_multi_currency(self):
		si = create_sales_invoice(
			posting_date=POSTING_DATE, qty=10, rate=100, currency="USD", conversion_rate=75
		)
		assert_gl_snapshot(self, "si_multi_currency", "Sales Invoice", si.name)

	def test_si_return(self):
		original = create_sales_invoice(posting_date=POSTING_DATE, qty=10, rate=100)
		credit_note = make_sales_return(original.name)
		credit_note.set_posting_time = 1
		credit_note.posting_date = POSTING_DATE
		credit_note.insert()
		credit_note.submit()
		assert_gl_snapshot(self, "si_return", "Sales Invoice", credit_note.name)

	def test_si_round_off(self):
		si = create_sales_invoice(posting_date=POSTING_DATE, qty=1, rate=100, do_not_save=True)
		si.append(
			"taxes",
			{
				"charge_type": "On Net Total",
				"account_head": "_Test Account Service Tax - _TC",
				"cost_center": "_Test Cost Center - _TC",
				"description": "Service Tax",
				"rate": 6.5,
			},
		)
		si.insert()
		si.submit()
		assert_gl_snapshot(self, "si_round_off", "Sales Invoice", si.name)

	def test_si_with_discount_accounting(self):
		with change_settings("Selling Settings", {"enable_discount_accounting": 1}):
			discount_account = create_account(
				account_name="Discount Account",
				parent_account="Indirect Expenses - _TC",
				company=COMPANY,
			)
			si = create_sales_invoice(
				posting_date=POSTING_DATE, qty=1, rate=90, discount_account=discount_account
			)
			assert_gl_snapshot(self, "si_with_discount", "Sales Invoice", si.name)

	def test_si_with_advance(self):
		advance = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"party_type": "Customer",
				"party": CUSTOMER,
				"company": COMPANY,
				"posting_date": POSTING_DATE,
				"paid_from": "Debtors - _TC",
				"paid_to": "_Test Cash - _TC",
				"paid_from_account_currency": "INR",
				"paid_to_account_currency": "INR",
				"source_exchange_rate": 1,
				"target_exchange_rate": 1,
				"reference_no": "ADV-1",
				"reference_date": POSTING_DATE,
				"paid_amount": 500,
				"received_amount": 500,
			}
		)
		advance.insert()
		advance.submit()

		si = create_sales_invoice(posting_date=POSTING_DATE, qty=10, rate=100, do_not_save=True)
		si.allocate_advances_automatically = 1
		si.insert()
		si.submit()
		assert_gl_snapshot(self, "si_with_advance", "Sales Invoice", si.name)

	def test_si_pos(self):
		si = create_sales_invoice(posting_date=POSTING_DATE, qty=10, rate=100, do_not_save=True)
		si.is_pos = 1
		si.append("payments", {"mode_of_payment": "Cash", "amount": 500})
		si.append("payments", {"mode_of_payment": "Bank Draft", "amount": 500})
		si.insert()
		si.submit()
		assert_gl_snapshot(self, "si_pos", "Sales Invoice", si.name)

	def test_pi_basic(self):
		pi = make_dated_purchase_invoice(qty=5, rate=50)
		pi.insert()
		pi.submit()
		assert_gl_snapshot(self, "pi_basic", "Purchase Invoice", pi.name)

	def test_pi_with_taxes(self):
		pi = make_dated_purchase_invoice(qty=5, rate=50)
		pi.append(
			"taxes",
			{
				"charge_type": "On Net Total",
				"account_head": "_Test Account VAT - _TC",
				"cost_center": "_Test Cost Center - _TC",
				"description": "VAT",
				"rate": 15,
			},
		)
		pi.insert()
		pi.submit()
		assert_gl_snapshot(self, "pi_with_taxes", "Purchase Invoice", pi.name)

	def test_pi_multi_currency(self):
		pi = make_dated_purchase_invoice(qty=5, rate=50, currency="USD", conversion_rate=75)
		pi.insert()
		pi.submit()
		assert_gl_snapshot(self, "pi_multi_currency", "Purchase Invoice", pi.name)

	def test_pi_return(self):
		original = make_dated_purchase_invoice(qty=5, rate=50)
		original.insert()
		original.submit()
		debit_note = make_debit_note(original.name)
		debit_note.set_posting_time = 1
		debit_note.posting_date = POSTING_DATE
		debit_note.insert()
		debit_note.submit()
		assert_gl_snapshot(self, "pi_return", "Purchase Invoice", debit_note.name)

	def test_pe_receive_against_si(self):
		si = create_sales_invoice(posting_date=POSTING_DATE, qty=10, rate=100)
		pe = get_payment_entry("Sales Invoice", si.name, bank_account="_Test Cash - _TC")
		pe.posting_date = POSTING_DATE
		pe.reference_no = "PE-REC-1"
		pe.reference_date = POSTING_DATE
		pe.insert()
		pe.submit()
		assert_gl_snapshot(self, "pe_receive_against_si", "Payment Entry", pe.name)

	def test_pe_pay_against_pi(self):
		pi = make_dated_purchase_invoice(qty=5, rate=50)
		pi.insert()
		pi.submit()
		pe = get_payment_entry("Purchase Invoice", pi.name, bank_account="_Test Bank - _TC")
		pe.posting_date = POSTING_DATE
		pe.reference_no = "PE-PAY-1"
		pe.reference_date = POSTING_DATE
		pe.insert()
		pe.submit()
		assert_gl_snapshot(self, "pe_pay_against_pi", "Payment Entry", pe.name)

	def test_pe_with_deductions(self):
		si = create_sales_invoice(posting_date=POSTING_DATE, qty=10, rate=100)
		pe = get_payment_entry("Sales Invoice", si.name, bank_account="_Test Cash - _TC")
		pe.posting_date = POSTING_DATE
		pe.reference_no = "PE-DED-1"
		pe.reference_date = POSTING_DATE
		pe.received_amount = pe.received_amount - 50
		pe.append(
			"deductions",
			{
				"account": "Write Off - _TC",
				"cost_center": "_Test Cost Center - _TC",
				"amount": 50,
			},
		)
		pe.insert()
		pe.submit()
		assert_gl_snapshot(self, "pe_with_deductions", "Payment Entry", pe.name)

	def test_pe_with_taxes(self):
		frappe.db.set_single_value("Accounts Settings", "merge_similar_account_heads", 1)
		pe = make_dated_payment_entry(party="_Test Supplier", paid_to="Creditors - _TC")
		pe.append(
			"taxes",
			{
				"account_head": "_Test Account Service Tax - _TC",
				"charge_type": "Actual",
				"tax_amount": 100,
				"add_deduct_tax": "Add",
				"description": "Service Tax",
				"cost_center": "_Test Cost Center - _TC",
			},
		)
		pe.save()
		pe.submit()
		assert_gl_snapshot(self, "pe_with_taxes", "Payment Entry", pe.name)

	def test_pe_multi_currency(self):
		pe = make_dated_payment_entry(party="_Test Supplier USD", paid_to="_Test Payable USD - _TC")
		pe.target_exchange_rate = 80
		pe.received_amount = pe.paid_amount / pe.target_exchange_rate
		pe.save()
		pe.submit()
		assert_gl_snapshot(self, "pe_multi_currency", "Payment Entry", pe.name)

	def test_je_basic(self):
		jv = make_dated_journal_entry(
			[
				{
					"account": "_Test Cash - _TC",
					"cost_center": "_Test Cost Center - _TC",
					"debit_in_account_currency": 1000,
					"exchange_rate": 1,
				},
				{
					"account": "_Test Bank - _TC",
					"cost_center": "_Test Cost Center - _TC",
					"credit_in_account_currency": 1000,
					"exchange_rate": 1,
				},
			]
		)
		jv.insert()
		jv.submit()
		assert_gl_snapshot(self, "je_basic", "Journal Entry", jv.name)

	def test_je_multi_currency(self):
		jv = make_dated_journal_entry(
			[
				{
					"account": "_Test Bank USD - _TC",
					"cost_center": "_Test Cost Center - _TC",
					"debit_in_account_currency": 100,
					"exchange_rate": 75,
				},
				{
					"account": "_Test Bank - _TC",
					"cost_center": "_Test Cost Center - _TC",
					"credit_in_account_currency": 7500,
					"exchange_rate": 1,
				},
			],
			multi_currency=1,
		)
		jv.insert()
		jv.submit()
		assert_gl_snapshot(self, "je_multi_currency", "Journal Entry", jv.name)

	def test_je_against_si(self):
		si = create_sales_invoice(posting_date=POSTING_DATE, qty=10, rate=100)
		jv = make_dated_journal_entry(
			[
				{
					"account": "Write Off - _TC",
					"cost_center": "_Test Cost Center - _TC",
					"debit_in_account_currency": 1000,
					"exchange_rate": 1,
				},
				{
					"account": "Debtors - _TC",
					"party_type": "Customer",
					"party": CUSTOMER,
					"cost_center": "_Test Cost Center - _TC",
					"credit_in_account_currency": 1000,
					"exchange_rate": 1,
					"reference_type": "Sales Invoice",
					"reference_name": si.name,
				},
			]
		)
		jv.insert()
		jv.submit()
		assert_gl_snapshot(self, "je_against_si", "Journal Entry", jv.name)

	def test_dn_basic(self):
		make_stock_entry(item_code="_Test Item", target=DN_WAREHOUSE, qty=10, basic_rate=100)
		dn = _make_dated_delivery_note(qty=5, rate=150)
		dn.insert()
		dn.submit()
		assert_gl_snapshot(self, "dn_basic", "Delivery Note", dn.name)

	def test_dn_return(self):
		make_stock_entry(item_code="_Test Item", target=DN_WAREHOUSE, qty=10, basic_rate=100)
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
		assert_gl_snapshot(self, "dn_return", "Delivery Note", ret.name)

	def test_se_material_receipt(self):
		se = make_stock_entry(
			item_code="_Test Item",
			target=DN_WAREHOUSE,
			qty=5,
			basic_rate=100,
			company=DN_COMPANY,
			posting_date=POSTING_DATE,
			do_not_submit=True,
		)
		se.submit()
		assert_gl_snapshot(self, "se_material_receipt", "Stock Entry", se.name)

	def test_se_material_issue(self):
		make_stock_entry(
			item_code="_Test Item", target=DN_WAREHOUSE, qty=10, basic_rate=100, company=DN_COMPANY
		)
		se = make_stock_entry(
			item_code="_Test Item",
			source=DN_WAREHOUSE,
			qty=5,
			company=DN_COMPANY,
			posting_date=POSTING_DATE,
			do_not_submit=True,
		)
		se.submit()
		assert_gl_snapshot(self, "se_material_issue", "Stock Entry", se.name)

	def test_se_material_transfer(self):
		make_stock_entry(
			item_code="_Test Item", target=DN_WAREHOUSE, qty=10, basic_rate=100, company=DN_COMPANY
		)
		se = make_stock_entry(
			item_code="_Test Item",
			source=DN_WAREHOUSE,
			target="Finished Goods - TCP1",
			qty=5,
			company=DN_COMPANY,
			posting_date=POSTING_DATE,
			do_not_submit=True,
		)
		se.submit()
		assert_gl_snapshot(self, "se_material_transfer", "Stock Entry", se.name)

	def test_sr_basic(self):
		sr = _make_dated_stock_reconciliation(qty=10, rate=150)
		sr.insert()
		sr.submit()
		assert_gl_snapshot(self, "sr_basic", "Stock Reconciliation", sr.name)

	def test_pr_basic(self):
		pr = make_purchase_receipt(
			company=DN_COMPANY,
			warehouse=DN_WAREHOUSE,
			posting_date=POSTING_DATE,
			qty=5,
			rate=100,
		)
		assert_gl_snapshot(self, "pr_basic", "Purchase Receipt", pr.name)

	def test_pr_with_taxes(self):
		pr = make_purchase_receipt(
			company=DN_COMPANY,
			warehouse=DN_WAREHOUSE,
			posting_date=POSTING_DATE,
			qty=5,
			rate=100,
			get_taxes_and_charges=True,
		)
		assert_gl_snapshot(self, "pr_with_taxes", "Purchase Receipt", pr.name)

	def test_pr_return(self):
		original = make_purchase_receipt(
			company=DN_COMPANY,
			warehouse=DN_WAREHOUSE,
			posting_date=POSTING_DATE,
			qty=5,
			rate=100,
		)
		from erpnext.stock.doctype.purchase_receipt.mapper import make_purchase_return

		ret = make_purchase_return(original.name)
		ret.posting_date = POSTING_DATE
		ret.set_posting_time = 1
		ret.insert()
		ret.submit()
		assert_gl_snapshot(self, "pr_return", "Purchase Receipt", ret.name)


def _make_dated_delivery_note(**args) -> frappe.Document:
	"""Minimal Delivery Note on a fixed posting date using the perpetual-inventory
	test company.

	Inlined to avoid importing test_delivery_note which drags in conflicting
	test-record dependencies at discovery time."""
	dn = frappe.new_doc("Delivery Note")
	dn.company = DN_COMPANY
	dn.customer = CUSTOMER
	dn.posting_date = POSTING_DATE
	dn.set_posting_time = 1
	dn.append(
		"items",
		{
			"item_code": args.get("item_code", "_Test Item"),
			"warehouse": args.get("warehouse", DN_WAREHOUSE),
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
	sr.company = DN_COMPANY
	sr.purpose = args.get("purpose", "Stock Reconciliation")
	sr.posting_date = POSTING_DATE
	sr.posting_time = "00:00:00"
	sr.set_posting_time = 1
	sr.expense_account = frappe.get_cached_value("Company", DN_COMPANY, "stock_adjustment_account")
	sr.cost_center = frappe.get_cached_value("Company", DN_COMPANY, "cost_center")
	sr.append(
		"items",
		{
			"item_code": args.get("item_code", "_Test Item"),
			"warehouse": args.get("warehouse", DN_WAREHOUSE),
			"qty": args.get("qty", 10),
			"valuation_rate": args.get("rate", 100),
		},
	)
	return sr

# Accounts / Controller Refactor — Spec

## Motivation
Move ERPNext away from the deep `AccountsController → SellingController/BuyingController → SalesInvoice`
inheritance chain and the monolithic `sales_invoice.py` / god-object `accounts_controller.py`
toward **composition**: per-doctype `services/` plus shared module-level `accounts/services/`.
Goal is testability, readability, and factoring shared domain logic out so Sales/Purchase
voucher logic is not duplicated.

## Target structure
```
erpnext
├── controllers
│   └── transaction_controller.py        # thin lifecycle base, delegates to services
├── accounts
│   ├── general_ledger.py                # the SINK (unchanged): post / merge / round-off / reverse
│   ├── services
│   │   ├── base_gl_composer.py          # BaseGLComposer — shared GL helpers
│   │   ├── gl_validator.py              # list-level validation (functions, stateless)
│   │   ├── advances.py
│   │   ├── taxes.py
│   │   └── budget.py
│   └── doctype
│       └── sales_invoice
│           ├── sales_invoice.py         # thin: delegates to services
│           ├── services
│           │   ├── gl_composer.py       # SalesInvoiceGLComposer(BaseGLComposer)
│           │   ├── pos.py
│           │   ├── loyalty.py
│           │   ├── status.py
│           │   ├── inter_company.py
│           │   ├── fixed_assets.py
│           │   └── timesheet_billing.py
│           ├── mapper.py
│           └── api.py
```

## GL layer — frozen design
Pipeline:
```
SalesInvoiceGLComposer.compose()  →  gl_entries  →  gl_validator.validate(gl_entries)  →  general_ledger.make_gl_entries()
```

| Role | Location | Form | Responsibility |
|---|---|---|---|
| **Composer (base)** | `accounts/services/base_gl_composer.py` → `BaseGLComposer` | class (stateful, holds `self.doc`) | shared row factory + common entries |
| **Composer (doctype)** | `sales_invoice/services/gl_composer.py` → `SalesInvoiceGLComposer(BaseGLComposer)` | class | voucher-specific rows via `.compose()` |
| **Validator** | `accounts/services/gl_validator.py` | module functions (stateless) | assert the finished `gl_entries` list is legal to post |
| **Sink** | `accounts/general_ledger.py` (unchanged) | module functions | merge / round-off / post / reverse |

### Naming decisions (frozen)
- Chose **`compose`** over `make`/`build` — the sink already owns the verb `make` (`make_gl_entries`); `compose` avoids a two-makers collision.
- `base_` prefix on the shared/abstract file; the concrete subclass carries the specific name, no prefix.
- Rejected: `gl_map` (it's a list, not a map — but it's an entrenched public param; rename to `gl_entries` later as its own deprecation pass), `gl_processor` (redundant with `general_ledger.py`), `gl_entries.py` (collides with the `gl_entry` doctype + the ubiquitous local var), `ledger_builder` (clashes with stock/payment ledger), `builder`/`maker` (generic; "maker" collides with `make_gl_entries`).

## Bucketing `accounts_controller.py`
- **Base composer (`BaseGLComposer`):** `get_gl_dict`, `get_value_in_transaction_currency`, `make_discount_gl_entries` (+ `get_amount_and_base_amount`, `get_tax_amounts`), `make_precision_loss_gl_entry`, `make_exchange_gain_loss_journal` (+ `gain_loss_journal_already_booked`), `set_transaction_currency_and_rate_in_gl_map`. Regional hooks `update_gl_dict_with_regional_fields` / `..._app_based_fields` stay free functions called inside `get_gl_dict`.
- **Advances service:** `set_advances`, `get_advance_entries`, `clear_unallocated_advances`, `validate_advance_entries`, `set_advance_gain_or_loss`, `calculate_total_advance_from_ledger`, `set_total_advance_paid`, `set_advance_payment_status`, `delink_advance_entries`, `create_advance_and_reconcile`, `get_advance_payment_doctypes`, `_remove_advance_payment_ledger_entries`, module funcs `get_advance_journal_entries` / `get_advance_payment_entries`.
- **Validator (from `general_ledger.py`):** `validate_disabled_accounts`, `validate_accounting_period`, `validate_cwip_accounts`, `check_freezing_date`, `validate_against_pcv`, `validate_allowed_dimensions`. (Moved in Phase 1.)
  - **Balance trio stays in `general_ledger.py` for now** (revised during Phase 1): `get_debit_credit_difference` / `get_debit_credit_allowance` / `raise_debit_credit_not_equal_error`. `get_debit_credit_difference` *mutates* entries (rounds debit/credit in place) and the trio is interleaved with `process_debit_credit_difference` → `make_round_off_gle` (the round-off *repair* run before and after balancing). It is not a standalone pre-post gate, so it can't move into a pure `validate(gl_entries)` without changing behavior. It travels with round-off when that moves compose-side (see below).
  - **Stays in compose (do NOT move to validator):** `process_debit_credit_difference` / `make_round_off_gle` — these *repair* balance by appending a round-off entry (mutation), not validation.
  - **Stays in composer (not validator):** row-level checks (right account for a row, dimension applicability) — validator only validates the finished list.
- **Leave in controller:** `validate_company_in_accounting_dimension`, `validate_company` (dimension validation, not GL).

## Phases
Each phase is behavior-preserving, one draft PR, gated by the Phase-0 snapshot suite + `bench run-tests --site test-site-ai`.

### Phase 0 — Safety net (first, mandatory)
Characterization tests snapshotting `gl_entries` output for representative transactions (SI/PI with taxes, multi-currency, advances, discounts, round-off, POS). Every later phase passes iff snapshots are byte-identical.

### Phase 1 — Extract `gl_validator.py` (lowest risk) — DONE
Moved the 6 pure list-level validators to `erpnext/accounts/services/gl_validator.py`; `general_ledger.py` imports and calls them at the existing call sites (no behavior change). A consolidated `gl_validator.validate(gl_entries)` facade is deferred — the current checks run at different points (make_gl_entries / save_entries per-entry / make_reverse_gl_entries), so collapsing them into one call would alter ordering. Verified: all 12 Phase-0 snapshots byte-identical.

### Phase 2 — Pilot composer on Sales Invoice only — DONE
Added `BaseGLComposer` (minimal: holds `self.doc`) and `SalesInvoiceGLComposer`. SI's `get_gl_entries` is a thin shim delegating to `SalesInvoiceGLComposer(self).compose()`. All 11 SI-specific row builders (make_customer/tax/item/internal_transfer/pos/loyalty/write_off/rounding GL entries, stock_delivered_but_not_billed, get_gl_entries_for_fixed_asset, get_gle_for_change_amount) moved onto the composer and operate on `self.doc`. The `super().get_gl_entries()` stock-expense call became `super(SalesInvoice, doc).get_gl_entries()` (MRO-faithful). Bucket-A shared helpers (`get_gl_dict`, `make_discount_gl_entries`, `make_precision_loss_gl_entry`, `set_transaction_currency_and_rate_in_gl_map`, `get_tax_amounts`, `get_amount_and_base_amount`) **stay on the controller** — they're still called via `self.doc` and only lift to `BaseGLComposer` once all doctypes use composers (can't move while other doctypes inherit them). Verified: 12 snapshots + 10 existing SI tests (perpetual `super()`, POS change, write-off, returns, fixed-asset disposal/regain, internal transfer, loyalty) all green.

### Phase 3 — Second doctype: Purchase Invoice (base earns its shape) — DONE
Added `PurchaseInvoiceGLComposer` with all 13 PI GL builders migrated (make_supplier_gl_entry, add_supplier_gl_entry, make_item_gl_entries, make_stock_adjustment_entry, get_provisional_accounts, make_provisional_gl_entry, update_net_purchase_amount_for_linked_assets, make_tax_gl_entries, make_internal_transfer_gl_entries, make_gl_entries_for_tax_withholding, make_payment_gl_entries, make_write_off_gl_entry, make_gle_for_rounding_adjustment). PI.get_gl_entries is a thin shim. **Decision after comparing SI and PI: keep `BaseGLComposer` minimal** (`self.doc` + abstract `compose`). The two flows differ too much to share a template — different step order, different builders, per-doctype `make_regional_gl_entries`. Revisit base-lifting only when a 3rd+ doctype reveals a real common shape. Remaining on doc: Bucket-A helpers (`make_precision_loss_gl_entry`, `set_transaction_currency_and_rate_in_gl_map`, `get_gl_dict`, `get_tax_amounts`, `get_amount_and_base_amount`) and inherited `set_gl_entry_for_purchase_expense`. Verified: 12 snapshots + 80/81 existing PI tests green (1 pre-existing failure in `test_purchase_invoice_with_exchange_rate_difference_for_non_stock_item`, unrelated to refactoring).

### Phase 4 — Roll out composer to remaining GL-posting doctypes
Payment Entry, Journal Entry, Delivery Note, Stock Entry, etc. Mechanical now; one PR per doctype (or small batches), each snapshot-gated.

- **Payment Entry — DONE.** Added `payment_entry/services/gl_composer.py` → `PaymentEntryGLComposer(BaseGLComposer)`. `compose()` mirrors the old `build_gl_map` (setup party account field, set txn currency/rate, then party/bank/deductions/tax builders, then `add_regional_gl_entries`). The four row builders (`add_party_gl_entries`, `add_bank_gl_entries`, `add_tax_gl_entries`, `add_deductions_gl_entries`) moved onto the composer and operate on `self.doc`; `build_gl_map` is now a thin shim delegating to the composer. **Advance builders stay on the doc** (`make_advance_gl_entries`, `add_advance_gl_entries`, `get_dr_and_account_for_advances`, `add_advance_gl_for_reference`) — they post in a separate pass inside `make_gl_entries`, not part of `compose()`, and belong to the Phase 5 advances service. Shared helpers (`get_gl_dict`, `calculate_base_allocated_amount_for_reference`, `get_exchange_rate`, `get_party_account_for_taxes`) stay on the doc, called via `self.doc`. Extended the Phase-0 snapshot net with 5 PE scenarios (receive-vs-SI, pay-vs-PI, deductions, taxes, multi-currency). Verified: 17 snapshots byte-identical + 53 existing PE tests green.

### Phase 5 — Extract `advances.py`
Move the advances cluster. After composers, because advances cross-calls the exchange-gain/loss helper now on `BaseGLComposer`.

### Phase 6 — Extract remaining domain services from `accounts_controller`
`taxes.py`, `budget.py`, etc. Shrink `accounts_controller` to a thin lifecycle base that delegates.

### Phase 7 — Split the rest of the `sales_invoice.py` monolith
Non-GL doctype services: `pos.py`, `loyalty.py`, `status.py`, `inter_company.py`, `fixed_assets.py`, `timesheet_billing.py`. Independent of GL work; can run parallel to 5–6.

### Phase 8 — Collapse the inheritance chain
Flatten `SellingController` / `BuyingController` layers that are now pass-through. Last, because only safe once the delegated-to services exist.

**Dependencies:** 1→2→3 sequential; 4 and 7 can parallelize once 3 lands; 8 always last.

## Cross-cutting rules
- Public signatures stay stable — keep the `gl_map=` param and `make_gl_entries` intact. The `gl_map → gl_entries` rename is its own deprecation pass, deferred to the end (or excluded).
- Composers are classes (stateful, per-document); sink and validator are stateless module functions.
- Every phase: behavior-preserving, snapshot + `bench run-tests --site test-site-ai` green before merge, draft PR.

# VEYQON patches on URY

Branch `veyqon/shared-tenant-safety`, based on ury-erp/ury `version-16@100c43f`. Purpose: install URY on a Frappe 16 / ERPNext site that also runs non-restaurant accounting, stock and manufacturing, without URY changing those records. Every change is a no-op for restaurant records. Each is marked `VEYQON shared-tenant` in the code and is meant to go upstream as a PR.

| # | File | Change |
|---|---|---|
| 1 | `ury/ury/utils/stock_reconciliation_guards.py` | One-draft-per-branch rule skipped when the reconciliation has no branch (before: one branchless draft per site). |
| 2 | `ury/ury/api/ury_production_plan_cancel_hooks.py` | Work Order link-check waiver only for plans with `custom_ury_sales_plan` (before: any plan cancelled with orphaned Work Orders). |
| 3 | `ury/ury/hooks/ury_sales_invoice.py` | `update_stock = 1` and the round-off Journal Entry only when the POS Profile has a `restaurant`. |
| 4 | `ury/public/js/remove_duplicates.js` | Duplicate-item-line block only for users with a URY role (before: every user, on PO / PR / PI / Material Request). The copy in `ury/ury/public/js/` is not served and is untouched. |
| 5 | `ury/ury/hooks/ury_item.py` | Yield-standard guard: a new Item carrying the field defaults, or an empty value, is not a manager change (before: non-managers could not create Items). |
| 6 | `ury/setup/demo.py` | "Clear Demo Data" keeps URY's "(Demo)"-company guard but no longer deletes `%Demo%` Price Lists site-wide or value-matched masters (Item, Customer, User, Employee, Branch, Gender, Item Group…). |

Tests: `ury/ury/utils/test_stock_reconciliation_guards.py`, `ury/ury/hooks/test_veyqon_shared_tenant.py`, and one new case in `ury/ury/api/test_ury_production_plan_cancel_hooks.py`. Verified so far only against a minimal frappe stub (logic), not on a bench: `bench --site <test-site> run-tests --app ury --module ury.ury.hooks.test_veyqon_shared_tenant` etc. still to run.

Licence: URY's `LICENSE` is AGPL-3.0 (`hooks.py` says MIT; the LICENSE file governs). This fork must stay AGPL and its source must be offered to users of the deployed service.

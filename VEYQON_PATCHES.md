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
| 7 | `ury/ury/api/dashboard.py`, `ury/api/dashboard.py` | `get_module_records` uses permission-checked `frappe.get_list` (before: `get_all` / `db.get_all` returned every row of any DocType — or of User / POS Invoice — to any logged-in user). |
| 8 | `ury/ury/doctype/ury_print_job/ury_print_job.json` | Guest permission removed (before: Guest read/export/share on print jobs: invoice names, users, printer IP/port, PDF path). |
| 9 | `ury/ury/printing/api.py` | `retry_print_job` limited to System Manager / URY Manager / URY Admin and to jobs whose referenced document the caller can read (before: any logged-in user, any job, any printer). |
| 10 | `ury/ury/doctype/bulk_production/bulk_production.py` | Cancel reverses each Stock Entry through `doc.cancel()` (before: `db.set_value(docstatus=2)` left stock ledger and GL entries in place). Retry only for a submitted run with status Failed, by users who may submit Stock Entries (before: retry on a posted run duplicated every Stock Entry). Note: cancel still logs and continues on a per-entry failure, as upstream does. |

Tests: `ury/ury/utils/test_stock_reconciliation_guards.py`, `ury/ury/hooks/test_veyqon_shared_tenant.py`, one new case in `ury/ury/api/test_ury_production_plan_cancel_hooks.py`, and `TestGetModuleRecords` in `ury/ury/api/test_dashboard.py` (now patches `get_list`). Verified so far only against a minimal frappe stub (logic), not on a bench: `bench --site <test-site> run-tests --app ury --module ury.ury.hooks.test_veyqon_shared_tenant` etc. still to run.

Licence: URY's `LICENSE` is AGPL-3.0 (`hooks.py` says MIT; the LICENSE file governs). This fork must stay AGPL and its source must be offered to users of the deployed service.

## POS / Captain (P-, C-, SC-)

IDs from Q-Restaurant `docs/specs/12-pos-cashier.md` (P-), `13-captain.md` (C-) and `00-phase6-integration.md` (SC-). Code marked `# VEYQON:`.

| ID | File | Change |
|---|---|---|
| helpers | `ury/ury_pos/api.py` | `is_pos_superuser`, `assert_user_branch` (branch = session user's `URY User` branch, fail closed on no branch), `has_pos_profile_role` / `assert_billing_user` (role in the POS Profile's `role_allowed_for_billing`, or other "Role Permitted" tables). Administrator / System Manager pass. Meant for the P-1 / P-2 / C-1 fixes in `ury_order.py` too. |
| P-9 | `ury/ury_pos/api.py` `create_pos_opening_entry` | User must be in the profile's `applicable_for_users` (or the list is empty), and the profile's branch must be the user's branch (before: any cashier could open a session on another branch's profile). |
| P-5 | `ury/ury/api/ury_print.py` `qz_print_update`, `print_pos_page`, `select_network_printer`, `network_printing` (POS Invoice) | Marking a bill printed needs POS Invoice write + a billing role on the invoice's profile + the invoice's branch; a comment records who marked it. `print_pos_page` gates the POS Invoice `name` whatever `doctype` is passed; `select_network_printer` uses the invoice's own profile for printers. The gate in `qz_print_update` now runs outside the `try`, so a refusal is a PermissionError. |
| P-15 (part) | `ury/ury/api/ury_print.py` `print_pos_page` | `print_<branch>` is published per `URY User` of that branch instead of site-wide. Other site-wide events (`invoice_print_completed`, `kot_print_completed`, `printer_health_alert`, `kot_error_*`) live in files this patch does not touch. |
| SC-1 | `ury/ury/doctype/sub_pos_closing/sub_pos_closing.py` | `validate` requires `user == session user` (URY Manager / System Manager / Administrator may close for a cashier) and a submitted, Open POS Opening Entry of that user and profile; `period_start_date` comes from it. Transactions, totals, opening / expected / difference per mode are recomputed server-side from the same invoice set `get_pos_invoices` returns (before: a status-Paid list for transactions, client values for everything else, so a cashier could hide a shortage). permlevel of `expected_amount` / `difference` unchanged. |

Tests: `ury/ury/hooks/test_veyqon_pos_safety.py` (22 cases); `test_ury_print.py` runs as superuser for the gate (setUpModule) and patches the branch-user lookup; `test_create_pos_opening_entry_permissions.py` patches `getBranch`. Logic-verified against the frappe stub only; bench run still to do.

Not in this patch (other files): P-1 / P-2 `make_invoice` / `submit_order_invoice` and C-1 / P-4 merge / unmerge (`ury/ury/doctype/ury_order/ury_order.py`), P-3 / C-2 `kot_execute` whitelist (`ury/ury/api/ury_kot_generate.py`), C-3 `reduce_order_item_qty` (`ury/ury/api/ury_pos_invoice_qty_reduction.py`), C-4 POS Invoice REST writes (`ury/ury/hooks/ury_pos_invoice.py` / `hooks.py`).

## Kitchen / stock / realtime (S10-, K-)

Marked `# VEYQON:` in the code. Tests: `ury/ury/hooks/test_veyqon_kitchen_safety.py` (20 cases, minimal frappe stub only; bench run still to do).

| ID | File | Change |
|---|---|---|
| S10-16 / K-3 / K-4 | `ury/ury/api/veyqon_actor.py` (new) and the whitelisted services in `ury_wastage`, `ury_stock_service`, `ury_issue_authorization`, `ury_batch_manufacture_service`, `ury_kot_cancellation_service`, `ury_mto_fulfilment_service`, `ury_preproduced_fulfilment_service`, `ury_reservation_service`, `ury_kot_chef_filter` | The acting user is always `frappe.session.user`. `actor` (and `user` on `get_filtered_kot_list`) is still accepted, but must be empty or equal to the session user, otherwise PermissionError (before: `actor="Administrator"` skipped every role check, and stock and wastage posted as Administrator). Server callers (`sync_order`, self-order, seeds run as Administrator) already pass the session user. |
| K-3 | `ury_batch_manufacture_service.py` | `start_batch` / `receive_batch` also require the config's `branch` to be the caller's active branch (`_require_kot_branch_scope`; System Manager exempt). |
| K-4 | `ury_kot_cancellation_service.py` | Every `cancel_*` and `resolve_cancellation_disposition` requires a kitchen or manager role and the KOT's branch (`_require_execution_actor`, the item-execution gate). `cancel_before_start` releases a `reservation_name` only if that reservation is in the KOT's branch (part of P-8). |
| S10-17 | `ury_wastage.py`, `ury_issue_authorization.py` | Wastage qty is summed from the `Wastage Item` child table (`held_quantity`, `prior_quantities`) instead of the removed `wasted_qty` / `component_item` columns, so capture, approval and `create_issue_authorization` no longer fail on a v16 schema. |
| K-14 | `ury_kot_execution_service.py`, `ury_kot_item_execution_service.py` | KOT and item start / ready / serve refuse a KOT whose docstatus is not 1, whose type is Cancelled / Partially cancelled, or whose execution state is `CANCELLED_*` (before: READY on cancelled food created a Manufacture posting intent and overwrote the cancelled state). |
| K-1 / C-13 | `doctype/ury_kot/ury_kot.py`, `api/ury_kot_validation.py`, `printing/finalizer.py`, `printing/notifications.py`, `printing/print_job_poller.py` | `kot_update_*`, `kot_error_*`, `kot_print_completed` go to room `doctype:URY KOT`; `invoice_print_completed` to `doctype:POS Invoice`; `printer_health_alert`, `*print_long_running`, `print_job_status_updated` to `doctype:URY Print Job` (before: the site room, every System User socket on the tenant). Clients must `doctype_subscribe` (read-checked). Users who can read the DocType still see every branch until Branch User Permissions exist (backlog item 9). |
| C-3 | `ury_pos_invoice_qty_reduction.py` | `reduce_order_item_qty` also requires `_enforce_order_access(require_modify=True)`, the session user's branch (`assert_user_branch`) and the remove-sent-items right (`_can_remove_sent_items`) (before: POS Invoice write permission only). |

Existing tests changed to match: `test_ury_wastage` (held qty via the child-table SQL), `test_ury_kot_chef_filter`, `test_ury_kot_cancellation_disposition`, `test_ury_kot_cancellation_service_permissions`, `test_ury_kot_execution_service_permissions`, `test_ury_pos_invoice_qty_reduction` (patch the new gates; the gates have their own cases in the VEYQON test file).

Upstream URY UI note: the mosaic KDS and POS listened to these events on the site room; with the room change they must `doctype_subscribe`. Q-Restaurant polls and is unaffected.

## Self-order / integrations (S9-, G-, W-, GF-1)

| ID | File | Change |
|---|---|---|
| S9-16 | `ury/ury/doctype/ury_self_ordering_profile/ury_self_ordering_profile.json`, `ury/ury/api/self_ordering.py`, `ury/patches/v3_45/encrypt_self_ordering_profile_secrets.py` | `qr_signing_secret` is a Password field (before: Data, readable by every role with profile read, so anyone could mint table QR tokens). Read with `get_decrypted_password`. The patch moves existing values into `__Auth` unchanged, so printed QR codes keep working. New `rotate_qr_signing_secret(profile)` (POST, profile write permission) issues a new secret and invalidates every printed code. |
| S9-17 | same JSON + patch, `self_ordering.assign_device_table` | `staff_pin` is a Password field (before: Data, readable by cashiers over REST). 5 attempts per device per 15 min. |
| S9-18 | `ury/ury/api/payment_terminal.py` | `register_simulated_terminal_provider` no longer whitelisted (before: any signed-in user switched kiosk payments to auto-approve). |
| S9-20 | `self_ordering._resolve_device`, `ury/ury/doctype/ury_ordering_device/ury_ordering_device.py` | Device `validate`: branch follows the profile; a Fixed device needs a fixed table in that branch. `_resolve_device` refuses a device whose branch or fixed table is in another branch. |
| S9-23 / G-1 | `self_ordering.py` | All eleven guest functions (the ten the guest app calls + `share_payment_link`) are `methods=["POST"]`; `enroll_device` too. Bearer tokens no longer travel in GET query strings. URY's own React self-order client (`self-order/`) still sends GET reads and stops working; Q-Restaurant's guest client already POSTs. |
| S9-24 | `self_ordering.py` | Redis fixed-window limits per IP / device / session (hashed identities; HTTP requests only), numbers from spec 15 §6.2; over the limit gives `TooManyRequestsError` (HTTP 429). `create_payment_request` reuses a submitted Requested/Initiated Payment Request created since the invoice last changed. `share_payment_link` accepts only a phone number or e-mail address. |
| G-3 | `self_ordering.add_customer_items` | qty must be a whole number 1-20 (before: any float > 0, `0.001` and `1e9` passed); at most 40 lines per session per hour. Constants, not profile settings. |
| W-1 | `ury/ury/api/ury_ai_settings.py` | `set_provider_api_key` and `update_agent_config` need write permission on the AI Provider / Agent and save without `ignore_permissions` (before: any URY Manager / URY Admin / Manager overwrote tenant-wide keys). |
| W-6 | `ury/ury/api/wa_signature.py` | The webhook signature check runs only when `ury_whatsapp_app_secret` or `ury_chairman_whatsapp_account` is set (before: with no secret, every app's frappe_whatsapp webhook got 403). With the chairman channel on and no secret it still fails closed. |
| W-9 | `ury/fixtures/custom_field.json`, `ury/ury/hooks/checklist.py`, `hooks.py` | Quality Review `branch` / `employee` no longer `reqd`; a review with a branch still needs an employee (`validate`). The visibility filter applies only to reviews with a `branch` (URY checklists); other apps' reviews are visible as before URY. |
| GF-1 | `ury/ury/api/ury_department_profitability.py` | Full tier = System Manager (+ Administrator) only; `Finance`, `Finance Manager`, `URY Finance`, `Inventory Manager` dropped. `NO_ACCESS_ROLES` is checked before every other tier, so URY Cashier + Chef is denied. GF-2 / GF-3 not changed. |

Tests: `ury/ury/hooks/test_veyqon_selforder_safety.py` (29 cases); `test_wa_signature.py` (W-6 cases); `test_ury_department_profitability.py` uses System Manager for the full tier; `test_self_ordering.py`, `test_self_ordering_session_endpoints.py` and `test_ury_self_ordering_profile.py` read the secret / PIN through the Password API. Logic-verified against the frappe stub only; bench run (including the patch on a copy of a site with profiles) still to do.

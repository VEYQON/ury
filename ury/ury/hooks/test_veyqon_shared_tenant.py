# VEYQON shared-tenant patch tests for Sales Invoice, Item and hooks.

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.ury.hooks import ury_sales_invoice
from ury.ury.hooks.ury_item import validate_yield_standard_permission

SI = "ury.ury.hooks.ury_sales_invoice"
ITEM = "ury.ury.hooks.ury_item"


class TestRestaurantPosGate(FrappeTestCase):
	def _validate(self, doc, restaurant):
		with patch(f"{SI}.frappe.db.get_value", return_value=restaurant):
			ury_sales_invoice.validate(doc, "validate")
		return doc

	def test_non_restaurant_pos_invoice_keeps_update_stock(self):
		doc = self._validate(frappe._dict(is_pos=1, pos_profile="Shop POS", update_stock=0), None)
		self.assertEqual(doc.update_stock, 0)

	def test_restaurant_pos_invoice_updates_stock(self):
		doc = self._validate(frappe._dict(is_pos=1, pos_profile="Cafe POS", update_stock=0), "Cafe")
		self.assertEqual(doc.update_stock, 1)

	def test_round_off_still_posts_for_restaurant_pos(self):
		doc = frappe._dict(is_pos=1, pos_profile="Cafe POS", rounded_total=100, paid_amount=99, grand_total=100,
			debit_to="Debtors", customer="Walk-in", cost_center="Main", name="SINV-1", company="Cafe", posting_date="2026-11-13")
		lookup = {"restaurant": "Cafe", "cash_discount_account": "Cash Discount"}
		with patch(f"{SI}.frappe.db.get_value", side_effect=lambda dt, name, field: lookup[field]), 			 patch(f"{SI}.frappe.get_doc") as get_doc:
			ury_sales_invoice.round_off_journal_entry(doc)
		get_doc.assert_called_once()
		get_doc.return_value.submit.assert_called_once()

	def test_non_pos_invoice_untouched_without_lookup(self):
		doc = frappe._dict(is_pos=0, pos_profile=None, update_stock=0)
		with patch(f"{SI}.frappe.db.get_value") as get_value:
			ury_sales_invoice.validate(doc, "validate")
		get_value.assert_not_called()
		self.assertEqual(doc.update_stock, 0)

	def test_round_off_skipped_for_non_restaurant_pos(self):
		doc = frappe._dict(is_pos=1, pos_profile="Shop POS", rounded_total=100, paid_amount=99, grand_total=100, debit_to="Debtors")
		# a profile WITH a cash discount account but no restaurant: only the gate stops the JE
		lookup = {"restaurant": None, "cash_discount_account": "Cash Discount"}
		with patch(f"{SI}.frappe.db.get_value", side_effect=lambda dt, name, field: lookup[field]), \
			 patch(f"{SI}.frappe.get_doc") as get_doc:
			ury_sales_invoice.round_off_journal_entry(doc)
		get_doc.assert_not_called()


class TestYieldGuardOnNewItem(FrappeTestCase):
	def _doc(self, values, defaults):
		doc = MagicMock()
		doc.get.side_effect = lambda f, d=None: values.get(f, d)
		doc.get_doc_before_save.return_value = None
		doc.meta.get_field.side_effect = lambda f: frappe._dict(default=defaults.get(f))
		doc.meta.get_label.side_effect = lambda f: f
		return doc

	def _run(self, doc):
		with patch(f"{ITEM}._is_yield_standard_manager", return_value=False):
			validate_yield_standard_permission(doc, "validate")

	def test_new_item_with_defaults_is_allowed_for_non_manager(self):
		self._run(self._doc(
			{"custom_yield_check_cadence": "None", "custom_yield_tracked": 0},
			{"custom_yield_check_cadence": "None", "custom_yield_tracked": "0"},
		))

	def test_new_item_with_explicit_empty_value_is_allowed(self):
		self._run(self._doc({"custom_yield_check_cadence": ""}, {"custom_yield_check_cadence": "None"}))

	def test_existing_item_change_is_still_refused_for_non_manager(self):
		doc = self._doc({"custom_yield_check_cadence": "Sampled"}, {"custom_yield_check_cadence": "None"})
		doc.get_doc_before_save.return_value = frappe._dict(custom_yield_check_cadence="None")
		with self.assertRaises(frappe.PermissionError):
			self._run(doc)

	def test_new_item_setting_a_yield_standard_is_refused_for_non_manager(self):
		doc = self._doc(
			{"custom_yield_check_cadence": "Every Issue"},
			{"custom_yield_check_cadence": "None"},
		)
		with self.assertRaises(frappe.PermissionError):
			self._run(doc)


class TestDemoClearIsScoped(FrappeTestCase):
	def test_clear_masters_only_removes_the_demo_company_pos_profiles(self):
		from ury.setup import demo
		with patch.object(demo.frappe, "get_all", return_value=["Demo POS"]) as get_all, 			 patch.object(demo.frappe, "delete_doc") as delete_doc, 			 patch.object(demo.frappe, "get_hooks", return_value=["Item", "Customer"]) as get_hooks:
			demo.clear_masters("Cafe (Demo)")
		get_all.assert_called_once_with("POS Profile", filters={"company": "Cafe (Demo)"}, pluck="name")
		delete_doc.assert_called_once_with("POS Profile", "Demo POS", force=1, ignore_permissions=True)
		get_hooks.assert_not_called()

	def test_clear_demo_data_override_is_kept(self):
		# URY's override only deletes "(Demo)" companies; ERPNext's own deletes Global Defaults.demo_company.
		from ury import hooks
		self.assertEqual(hooks.override_whitelisted_methods["erpnext.setup.demo.clear_demo_data"], "ury.setup.demo.clear_demo_data")


class TestServedDuplicateScriptIsGated(FrappeTestCase):
	def test_app_include_js_points_at_the_gated_file(self):
		import os
		from ury import hooks
		self.assertIn("/assets/ury/js/remove_duplicates.js", hooks.app_include_js)
		# /assets/ury/... is served from the app package's public/ (ury/public), not ury/ury/public
		path = os.path.join(os.path.dirname(hooks.__file__), "public", "js", "remove_duplicates.js")
		with open(path, encoding="utf-8") as f:
			self.assertIn("if (!frappe.user.has_role(URY_ROLES)) return;", f.read())


class TestRetryPrintJobIsRestricted(FrappeTestCase):
	def test_non_manager_is_refused_before_the_job_is_read(self):
		from ury.ury.printing import api
		with patch.object(api.frappe, "only_for", side_effect=frappe.PermissionError) as only_for, \
			 patch.object(api, "get_job") as get_job:
			with self.assertRaises(frappe.PermissionError):
				api.retry_print_job("JOB-1", target_printer="Kitchen")
		only_for.assert_called_once()
		get_job.assert_not_called()

	def test_reference_document_read_permission_is_checked(self):
		from ury.ury.printing import api
		job = {"reference_doctype": "URY KOT", "reference_name": "KOT-1", "printer": "Kitchen"}
		with patch.object(api.frappe, "only_for"), \
			 patch.object(api, "get_job", return_value=job), \
			 patch.object(api.frappe, "has_permission", side_effect=frappe.PermissionError) as has_perm:
			with self.assertRaises(frappe.PermissionError):
				api.retry_print_job("JOB-1")
		has_perm.assert_called_once_with("URY KOT", "read", "KOT-1", throw=True)


class TestPrintJobHasNoGuestAccess(FrappeTestCase):
	def test_guest_is_not_granted(self):
		import json, os
		from ury.ury.doctype import ury_print_job
		path = os.path.join(os.path.dirname(ury_print_job.__file__), "ury_print_job.json")
		with open(path, encoding="utf-8") as f:
			roles = {p["role"] for p in json.load(f)["permissions"]}
		self.assertNotIn("Guest", roles)


class TestBulkProductionStockEntries(FrappeTestCase):
	def _bp(self, **fields):
		from ury.ury.doctype.bulk_production import bulk_production as bp
		doc = bp.BulkProduction.__new__(bp.BulkProduction)
		doc.__dict__.update({"name": "BP-1", "docstatus": 1, "status": "Failed", **fields})
		return bp, doc

	def test_cancel_reverses_through_the_stock_entry_document(self):
		bp, doc = self._bp(production_items=[frappe._dict(stock_entry="STE-1")])
		with patch.object(bp.frappe.db, "get_value", return_value=1), \
			 patch.object(bp.frappe.db, "set_value") as set_value, \
			 patch.object(bp.frappe, "get_doc") as get_doc:
			doc.cancel_stock_entry()
		get_doc.assert_called_once_with("Stock Entry", "STE-1")
		get_doc.return_value.cancel.assert_called_once()
		set_value.assert_not_called()

	def test_retry_refused_unless_submitted_and_failed(self):
		bp, doc = self._bp(status="Completed")
		with patch.object(bp.BulkProduction, "enqueue_save_stockentry") as enqueue:
			with self.assertRaises(frappe.ValidationError):
				doc.retry()
		enqueue.assert_not_called()

	def test_retry_requires_stock_entry_submit_permission(self):
		bp, doc = self._bp()
		with patch.object(bp.frappe, "has_permission", side_effect=frappe.PermissionError), \
			 patch.object(bp.BulkProduction, "enqueue_save_stockentry") as enqueue:
			with self.assertRaises(frappe.PermissionError):
				doc.retry()
		enqueue.assert_not_called()

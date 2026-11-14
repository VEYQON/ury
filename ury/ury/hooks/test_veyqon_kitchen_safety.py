# VEYQON kitchen / stock / realtime patch tests (S10-16, S10-17, K-1, K-3, K-4, K-14).

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.ury.api import ury_issue_authorization, ury_kot_execution_service, ury_wastage
from ury.ury.api.veyqon_actor import session_actor

WASTE = "ury.ury.api.ury_wastage"
EXEC = "ury.ury.api.ury_kot_execution_service"
ACTOR = "ury.ury.api.veyqon_actor"


class TestSessionActor(FrappeTestCase):
	def test_omitted_or_own_actor_is_session_user(self):
		with patch(f"{ACTOR}.frappe.session", frappe._dict(user="chef@x")):
			self.assertEqual(session_actor(None), "chef@x")
			self.assertEqual(session_actor("chef@x"), "chef@x")

	def test_other_actor_refused(self):
		with patch(f"{ACTOR}.frappe.session", frappe._dict(user="chef@x")):
			with self.assertRaises(frappe.PermissionError):
				session_actor("Administrator")

	def test_approve_wastage_with_administrator_actor_refused_before_any_read(self):
		with patch(f"{ACTOR}.frappe.session", frappe._dict(user="clerk@x")), \
			 patch(f"{WASTE}.frappe.get_doc") as get_doc:
			with self.assertRaises(frappe.PermissionError):
				ury_wastage.approve_wastage("W-1", actor="Administrator")
		get_doc.assert_not_called()

	def test_approve_wastage_checks_session_user_roles(self):
		with patch(f"{ACTOR}.frappe.session", frappe._dict(user="clerk@x")), \
			 patch(f"{WASTE}.frappe.get_roles", return_value=["Chef"]) as get_roles:
			with self.assertRaises(frappe.PermissionError):
				ury_wastage.reject_wastage("W-1")
		get_roles.assert_called_with("clerk@x")


class TestWastageQtyFromChildTable(FrappeTestCase):
	def test_held_quantity_sums_items_and_excludes_current(self):
		auth = frappe._dict(name="IA-1", authorized_qty=10, component_item="FLOUR")
		with patch(f"{WASTE}.frappe.db.exists", return_value=True), \
			 patch(f"{WASTE}.frappe.db.sql", return_value=[[4]], create=True) as sql, \
			 patch(f"{WASTE}.frappe.get_all", return_value=[]):
			self.assertEqual(ury_wastage.held_quantity(auth, exclude_wastage="W-1"), 6)
		query, params = sql.call_args[0]
		self.assertIn("tabWastage Item", query)
		self.assertIn("w.`name` != %(exclude_name)s", query)
		self.assertEqual(params["exclude_name"], "W-1")
		# review fix #9: only this component's rows, only Wastage Items of URY Wastage
		self.assertIn("i.`item_code` = %(component_item)s", query)
		self.assertIn("i.parenttype = %(parenttype)s", query)
		self.assertEqual((params["component_item"], params["parenttype"]), ("FLOUR", "URY Wastage"))

	def test_prior_quantities_joins_wastage_items_by_component(self):
		def get_all(doctype, **kw):
			self.assertNotEqual(doctype, "URY Wastage")  # removed columns live in the child table
			return []
		with patch(f"{WASTE}.frappe.db.exists", return_value=True), \
			 patch("ury.ury.api.ury_issue_authorization.frappe.db.exists", return_value=True), \
			 patch(f"{WASTE}.frappe.db.sql", return_value=[[2]], create=True) as sql, \
			 patch(f"{WASTE}.frappe.get_all", side_effect=get_all), \
			 patch("ury.ury.api.ury_issue_authorization.frappe.get_all", side_effect=get_all):
			prior = ury_issue_authorization.prior_quantities("P-1", "Hot", "B1", "C1", "FLOUR")
		self.assertEqual(prior["wasted_qty"], 2)
		query, params = sql.call_args[0]
		self.assertIn("i.`item_code` = %(component_item)s", query)
		self.assertEqual(params["component_item"], "FLOUR")


class TestCancelledKotTransitions(FrappeTestCase):
	def _check(self, kot_row, state):
		values = {"URY KOT": kot_row, "URY KOT Execution": state}
		with patch(f"{EXEC}.frappe.db.get_value", side_effect=lambda dt, *a, **k: values[dt]):
			ury_kot_execution_service._require_kot_open("KOT-1")

	def test_open_kot_allowed(self):
		self._check(frappe._dict(docstatus=1, type="New Order"), "QUEUED")

	def test_cancelled_doc_refused(self):
		with self.assertRaises(ury_kot_execution_service.ExecutionError):
			self._check(frappe._dict(docstatus=2, type="New Order"), None)

	def test_cancel_kot_type_refused(self):
		with self.assertRaises(ury_kot_execution_service.ExecutionError):
			self._check(frappe._dict(docstatus=1, type="Cancelled"), None)

	def test_cancelled_execution_state_refused(self):
		with self.assertRaises(ury_kot_execution_service.ExecutionError):
			self._check(frappe._dict(docstatus=1, type="New Order"), "CANCELLED_AFTER_START")


class TestKotRealtimeRoom(FrappeTestCase):
	def test_kot_update_not_published_to_site_room(self):
		from ury.ury.doctype.ury_kot import ury_kot

		doc = MagicMock(branch="B1", production=None, pos_profile="P1", time="10:00")
		cache = MagicMock()
		cache.get_value.return_value = None
		with patch.object(ury_kot.frappe, "publish_realtime", create=True) as pub, \
			 patch.object(ury_kot.frappe, "as_json", return_value="{}", create=True), \
			 patch.object(ury_kot.frappe, "cache", return_value=cache, create=True), \
			 patch(f"{EXEC}.frappe.db.get_value", return_value=None):
			ury_kot.URYKOT.kotDisplayRealtime(doc)
		self.assertEqual(pub.call_args.kwargs.get("room"), "doctype:URY KOT")


BATCH = "ury.ury.api.ury_batch_manufacture_service"
CANCEL = "ury.ury.api.ury_kot_cancellation_service"


class TestBatchActor(FrappeTestCase):
	def test_administrator_actor_refused(self):
		from ury.ury.api import ury_batch_manufacture_service as batch

		with patch(f"{ACTOR}.frappe.session", frappe._dict(user="chef@x")), \
			 patch(f"{BATCH}.flt", side_effect=float, create=True):
			with self.assertRaises(frappe.PermissionError):
				batch.start_batch("CFG-1", 1, actor="Administrator")
			with self.assertRaises(frappe.PermissionError):
				batch.receive_batch("CFG-1", 1, actor="Administrator")

	def test_config_branch_checked_against_session_user(self):
		from ury.ury.api import ury_batch_manufacture_service as batch

		row = {"name": "CFG-1", "branch": "B2", "active": 0}
		with patch(f"{ACTOR}.frappe.session", frappe._dict(user="chef@x")), \
			 patch(f"{BATCH}.frappe.get_roles", return_value=["Chef"]), \
			 patch(f"{BATCH}.frappe.has_permission", return_value=True), \
			 patch(f"{BATCH}.frappe.db.sql", return_value=[row], create=True), \
			 patch(f"{EXEC}._require_kot_branch_scope", side_effect=frappe.PermissionError) as scope:
			with self.assertRaises(frappe.PermissionError):
				batch._load_and_validate_config("CFG-1", batch.IN_HOUSE, None)
		scope.assert_called_once_with("B2", "chef@x")


class TestCancellationScope(FrappeTestCase):
	def _values(self, reservation_branch="B1"):
		def get_value(dt, *a, **k):
			return {"URY KOT": {"branch": "B1", "production": "Hot"}, "Branch": "C1",
				"URY Stock Reservation": reservation_branch, "URY KOT Execution": None}[dt]
		return get_value

	def test_waiter_cannot_cancel_before_start(self):
		from ury.ury.api import ury_kot_cancellation_service as cancel

		with patch(f"{ACTOR}.frappe.session", frappe._dict(user="waiter@x")), \
			 patch(f"{CANCEL}.frappe.db.exists", return_value=True), \
			 patch(f"{CANCEL}.frappe.db.get_value", side_effect=self._values()), \
			 patch(f"{CANCEL}.frappe.get_roles", return_value=["URY Captain"]):
			with self.assertRaises(frappe.ValidationError) as ctx:
				cancel.cancel_before_start("KOT-1")
		self.assertEqual(ctx.exception.reason_code, "NOT_PERMITTED")

	def test_reservation_of_other_branch_refused(self):
		from ury.ury.api import ury_kot_cancellation_service as cancel

		with patch(f"{ACTOR}.frappe.session", frappe._dict(user="chef@x")), \
			 patch(f"{CANCEL}.frappe.db.exists", return_value=True), \
			 patch(f"{CANCEL}.frappe.db.get_value", side_effect=self._values("B2")), \
			 patch(f"{CANCEL}.frappe.db.sql", return_value=[], create=True), \
			 patch(f"{CANCEL}.frappe.get_roles", return_value=["Chef"]), \
			 patch(f"{EXEC}._require_kot_branch_scope"), \
			 patch(f"{CANCEL}.ury_reservation_service.release_reservation") as release:
			with self.assertRaises(cancel.CancellationError) as ctx:
				cancel.cancel_before_start("KOT-1", reservation_name="RES-9")
		self.assertEqual(ctx.exception.reason_code, "BRANCH_SCOPE_MISMATCH")
		release.assert_not_called()


class TestChefFilterUser(FrappeTestCase):
	def test_other_user_refused(self):
		from ury.ury.api import ury_kot_chef_filter

		with patch(f"{ACTOR}.frappe.session", frappe._dict(user="chef@x")):
			with self.assertRaises(frappe.PermissionError):
				ury_kot_chef_filter.get_filtered_kot_list(user="Administrator")


class TestQtyReductionAccess(FrappeTestCase):
	def _gate(self, can_remove=True, branch_error=None, billing=False):
		import sys
		from ury.ury.api import ury_pos_invoice_qty_reduction as qr

		order = MagicMock(_can_remove_sent_items=MagicMock(return_value=can_remove))
		pos_api = MagicMock(assert_user_branch=MagicMock(side_effect=branch_error),
			has_pos_profile_role=MagicMock(return_value=billing))
		inv = frappe._dict(name="INV-1", branch="B1")
		with patch.dict(sys.modules, {"ury.ury.doctype.ury_order.ury_order": order, "ury.ury_pos.api": pos_api}):
			qr._require_order_reduction_access(inv, frappe._dict(name="P1"))
		# review fix #3: non-billing callers only while the order is modifiable; billing users on a printed bill
		order._enforce_order_access.assert_called_once_with(inv, require_modify=not billing)
		pos_api.has_pos_profile_role.assert_called_once_with("P1")
		pos_api.assert_user_branch.assert_called_once_with("B1")
		return order

	def test_owner_in_branch_with_remove_right_passes(self):
		self._gate()

	def test_billing_user_passes_without_remove_right(self):
		order = self._gate(can_remove=False, billing=True)
		order._can_remove_sent_items.assert_not_called()

	def test_no_remove_sent_items_right_refused(self):
		from ury.ury.api.ury_pos_invoice_qty_reduction import QtyReductionError

		with self.assertRaises(QtyReductionError) as ctx:
			self._gate(can_remove=False)
		self.assertEqual(ctx.exception.reason_code, "NOT_PERMITTED")

	def test_other_branch_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._gate(branch_error=frappe.PermissionError)


class TestPrintRealtimeRooms(FrappeTestCase):
	def test_printer_health_alert_not_site_wide(self):
		from ury.ury.printing import notifications

		with patch.object(notifications.frappe, "publish_realtime", create=True) as pub, \
			 patch.object(notifications.frappe, "logger", create=True):
			notifications.notify_printer_status("P1", False)
		self.assertEqual(pub.call_args.kwargs.get("room"), "doctype:URY Print Job")

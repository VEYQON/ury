# VEYQON gap-fill safety tests (Phase 9: GF-2..GF-8, reservation realtime).
# Mock style, like test_veyqon_selforder_safety.py.

import inspect
import json
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.ury.api import ury_availability as avail
from ury.ury.api import ury_department_profitability as dept
from ury.ury.api import ury_requirements_stock as req
from ury.ury.api import ury_sales_plan as plan_api
from ury.ury.api import ury_yield_variance as yv
from ury.ury.doctype.ury_table_reservation import ury_table_reservation as res
from ury.ury.report_api import day_close
from ury.ury.services import yield_check_reminders as reminders

POS = "ury.ury_pos.api"


@contextmanager
def _as(roles, branches=("B1",), user="u@x.test"):
	"""Session user with these roles and URY User rows on these branches."""
	with ExitStack() as stack:
		stack.enter_context(patch("frappe.get_roles", return_value=list(roles)))
		stack.enter_context(patch("frappe.session", SimpleNamespace(user=user), create=True))
		stack.enter_context(patch(f"{POS}.user_branches", return_value=list(branches)))
		get_branch = stack.enter_context(patch(f"{POS}.getBranch"))
		if branches:
			get_branch.return_value = branches[0]
		else:
			get_branch.side_effect = frappe.ValidationError("User is not Associated with any Branch")
		yield


class TestDeptProfitBranchTiers(FrappeTestCase):
	"""GF-2: URY management roles are branch managers scoped by URY User, never Employee.branch."""

	def test_management_roles_get_branch_tier_on_own_branch(self):
		for role in ("URY Manager", "URY Admin", "URY Owner", "Production Manager", "Manager"):
			with _as([role]), patch.object(dept.frappe.db, "get_value", side_effect=AssertionError("no Employee read"), create=True):
				self.assertEqual(dept._resolve_access("C1", "B1", None)[0], "branch_manager", role)

	def test_other_branch_denied(self):
		for role in ("URY Manager", "URY Admin", "Manager", "URY Chef"):
			with _as([role], branches=("B1",)), self.assertRaises(frappe.PermissionError, msg=role):
				dept._resolve_access("C1", "B2", None)

	def test_no_ury_user_row_denied(self):
		with _as(["URY Manager"], branches=()), self.assertRaises(frappe.PermissionError) as ctx:
			dept._resolve_access("C1", "B1", None)
		self.assertIn("You are not assigned to branch B1", str(ctx.exception))  # review M3

	def test_multi_branch_user_reaches_second_branch(self):
		with _as(["URY Manager"], branches=("B1", "B2")):
			self.assertEqual(dept._resolve_access("C1", "B2", None)[0], "branch_manager")

	def test_system_manager_full_without_branch_row(self):
		with _as(["System Manager"], branches=()):
			self.assertEqual(dept._resolve_access("C1", "B9", None)[0], "full")


class TestDeptProfitDepartmentTier(FrappeTestCase):
	"""GF-3: departments are URY Production Department rows the user manages, not HR departments."""

	def _run(self, department, managed):
		get_all = MagicMock(return_value=list(managed))
		with _as(["URY Department Manager"]), patch.object(dept.frappe, "get_all", get_all):
			out = dept._resolve_access("C1", "B1", department)
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(get_all.call_args.args[0], "URY Production Department")
		self.assertEqual(filters, {"branch": "B1", "department_manager": "u@x.test", "enabled": 1})
		return out

	def test_single_managed_department_is_forced(self):
		self.assertEqual(self._run(None, ["Kitchen"]), ("department_manager", False, "Kitchen"))

	def test_picked_managed_department(self):
		self.assertEqual(self._run("Bar", ["Kitchen", "Bar"])[2], "Bar")

	def test_unmanaged_department_fails_closed(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run("Bar", ["Kitchen"])
		self.assertIn(dept.DEPARTMENT_SCOPE_MISMATCH, str(ctx.exception))

	def test_none_managed_or_ambiguous(self):
		for department, managed in ((None, []), ("Kitchen", []), (None, ["Kitchen", "Bar"])):
			with self.assertRaises(frappe.ValidationError, msg=(department, managed)):
				self._run(department, managed)


class TestCloseDayScope(FrappeTestCase):
	"""GF-4: branch access, and Work Orders of the branch's company only."""

	def _call(self, company="C1", roles=("URY Manager",), branches=("B1",), branch="B1"):
		count = MagicMock(return_value=0)
		with _as(roles, branches), patch.object(day_close, "require_manager"), \
			patch.object(day_close.frappe.db, "get_value", return_value=company, create=True), \
			patch.object(day_close.frappe.db, "count", count, create=True), \
			patch.object(day_close.frappe, "get_all", return_value=[]):
			result = day_close.get_close_day_checklist(branch, "2026-11-14")
		return result, count

	def test_work_orders_filtered_by_branch_company(self):
		result, count = self._call()
		wo = [c for c in count.call_args_list if c.args[0] == "Work Order"]
		self.assertEqual(wo[0].args[1]["company"], "C1")
		note = next(i for i in result["items"] if i["key"] == "unposted_production")["scope_note"]
		self.assertIn("this company", note)

	def test_other_branch_denied(self):
		with self.assertRaises(frappe.PermissionError):
			self._call(branch="B2")

	def test_branch_without_company_fails_closed(self):
		with self.assertRaises(frappe.ValidationError):
			self._call(company=None)


class TestYieldScope(FrappeTestCase):
	"""GF-5: one branch of the caller, in the given company; company filter on every count."""

	def test_non_superuser_needs_branch(self):
		with _as(["URY Manager"]), self.assertRaises(frappe.PermissionError):
			yv._require_report_scope("C1", None)

	def test_superuser_may_aggregate(self):
		with _as(["System Manager"], branches=()):
			yv._require_report_scope("C1", None)

	def test_branch_outside_company_or_unassigned_denied(self):
		for company, branch in (("C2", "B1"), (None, "B1")):
			with _as(["URY Manager"]), patch.object(yv.frappe.db, "get_value", return_value=company, create=True):
				with self.assertRaises(frappe.PermissionError, msg=(company, branch)):
					yv._require_report_scope("C1", branch)
		with _as(["URY Manager"]), patch.object(yv.frappe.db, "get_value", return_value="C1", create=True):
			with self.assertRaises(frappe.PermissionError):
				yv._require_report_scope("C1", "B2")
			yv._require_report_scope("C1", "B1")

	def test_admin_still_checks_company(self):
		with _as(["System Manager"], branches=()), patch.object(yv.frappe.db, "get_value", return_value="C2", create=True):
			with self.assertRaises(frappe.PermissionError):
				yv._require_report_scope("C1", "B1")

	def test_endpoints_call_the_scope_check(self):
		for fn in (yv.get_yield_variance, yv.get_yield_check_compliance):
			self.assertIn("_require_report_scope(company, branch)", inspect.getsource(fn))
		src = inspect.getsource(yv.get_yield_check_compliance)
		self.assertEqual(src.count('"company": company,  # VEYQON: GF-5'), 2)

	def test_sampled_aggregate_counts_only_company_branches(self):
		get_all = MagicMock(return_value=[])
		with patch.object(yv.frappe, "get_all", get_all):
			yv._count_sampled_days(None, None, None, "I1", "C1")
		self.assertEqual(get_all.call_args.kwargs["filters"], {"company": "C1"})

	def test_due_checks_api_branch_scoped(self):
		with _as(["URY Manager"]), patch.object(reminders, "require_manager"), \
			patch.object(reminders, "get_due_yield_checks", return_value=[]) as due:
			with self.assertRaises(frappe.PermissionError):
				reminders.get_due_yield_checks_api("B2")
			due.assert_not_called()
			self.assertEqual(reminders.get_due_yield_checks_api("B1"), [])


class TestRequirementsStock(FrappeTestCase):
	"""GF-6: pairs come from the plan's frozen snapshot; plan read + branch access."""

	SNAP = '{"demand_vector": [{"component_item": "A", "department": "D1", "production_unit": "U1"},' \
		' {"component_item": "A", "department": "D1", "production_unit": "U2"}, {"component_item": "B", "department": "D2"}]}'

	def _call(self, plan="PLAN-1", branch=None, readable=True, roles=("URY Manager",), branches=("B1",)):
		doc = frappe._dict(branch="B1", company="C1", approval_snapshot=self.SNAP)
		with _as(roles, branches), patch.object(req, "require_manager"), \
			patch.object(req.frappe.db, "get_value", return_value=doc, create=True), \
			patch.object(req.frappe, "has_permission", return_value=readable, create=True), \
			patch.object(req, "parse_json", json.loads), \
			patch.object(req, "_stock_for_items", return_value=["ok"]) as stock:
			return req.get_plan_stock_on_hand(plan=plan, branch=branch), stock

	def test_client_items_not_accepted(self):
		self.assertNotIn("items", inspect.signature(req.get_plan_stock_on_hand).parameters)

	def test_pairs_derived_from_snapshot(self):
		out, stock = self._call()
		self.assertEqual(out, ["ok"])
		stock.assert_called_once_with(
			"B1", [{"item_code": "A", "department": "D1"}, {"item_code": "B", "department": "D2"}], "C1"
		)

	def test_guards(self):
		with self.assertRaises(frappe.ValidationError):
			self._call(plan=None)
		with self.assertRaises(frappe.PermissionError):
			self._call(readable=False)
		with self.assertRaises(frappe.ValidationError):
			self._call(branch="B2")
		with self.assertRaises(frappe.PermissionError):
			self._call(branches=("B2",))

	def test_profile_is_deterministic_and_company_scoped(self):
		get_value = MagicMock(return_value=None)
		with patch.object(req.frappe.db, "get_value", get_value, create=True), \
			patch.object(req.frappe, "cache", return_value=MagicMock(get_value=MagicMock(return_value=None))), \
			patch.object(req.frappe, "get_all", return_value=[]):
			req._stock_for_items("B1", [], "C1")
		call = get_value.call_args
		self.assertEqual(call.args[1], {"branch": "B1", "disabled": 0, "company": "C1"})
		self.assertEqual(call.kwargs["order_by"], "name asc")


class TestAvailabilityScope(FrappeTestCase):
	"""GF-7: company check for everyone; URY Admin needs a URY User row like any role."""

	def _check(self, roles, company_of_branch, assigned=False, user="u@x.test"):
		with _as(roles, user=user), patch.object(avail.frappe.db, "get_value", return_value=company_of_branch, create=True), \
			patch.object(avail.frappe.db, "exists", return_value=assigned, create=True):
			avail._verify_branch_scope(user, "B1", "C1")

	def test_admins_still_need_matching_company(self):
		for roles, user in ((["System Manager"], "u@x.test"), ([], "Administrator")):
			for company in ("C2", None):
				with self.assertRaises(frappe.PermissionError, msg=(roles, company)):
					self._check(roles, company, user=user)
			self._check(roles, "C1", user=user)

	def test_ury_admin_without_assignment_denied(self):
		with self.assertRaises(frappe.PermissionError):
			self._check(["URY Admin"], "C1", assigned=False)
		self._check(["URY Admin"], "C1", assigned=True)

	def test_branch_without_company_fails_closed(self):
		with self.assertRaises(frappe.PermissionError):
			self._check(["URY Manager"], None, assigned=True)


class TestPlanStatusScope(FrappeTestCase):
	"""GF-8: branch access and read on the plan found."""

	def _call(self, branch="B1", doc_readable=True):
		def has_permission(doctype, ptype, doc=None, **kw):
			return doc_readable if doc else True

		with _as(["URY Manager"]), patch.object(plan_api.frappe, "has_permission", side_effect=has_permission, create=True), \
			patch.object(plan_api.frappe, "get_all", return_value=[{"name": "P1", "status": "Draft", "enforcement_mode": "Block"}]) as get_all:
			return plan_api.get_plan_status(branch, "2026-11-14"), get_all

	def test_other_branch_denied_before_any_read(self):
		with self.assertRaises(frappe.PermissionError):
			self._call(branch="B2")

	def test_unreadable_plan_denied(self):
		with self.assertRaises(frappe.PermissionError):
			self._call(doc_readable=False)

	def test_own_branch_ok(self):
		self.assertEqual(self._call()[0]["name"], "P1")


class TestReservationRealtime(FrappeTestCase):
	def _doc(self):
		doc = res.URYTableReservation.__new__(res.URYTableReservation)
		doc.__dict__.update(name="R1", branch="B1", status="Confirmed", reserved_table="T1", customer_phone="+94770000000")
		return doc

	def test_publish_to_doctype_room_without_pii(self):
		publish = MagicMock()
		with patch.object(res.frappe, "publish_realtime", publish, create=True):
			self._doc().publish_reservation_update()
			self._doc().on_trash()
		(event, payload), kwargs = publish.call_args_list[0]
		self.assertEqual(event, "table_reservation_updated")
		self.assertEqual(kwargs, {"room": "doctype:URY Table Reservation", "after_commit": True})
		self.assertNotIn("customer_phone", payload)
		self.assertTrue(publish.call_args_list[1].args[1]["deleted"])

	def test_on_update_publishes(self):
		self.assertIn("self.publish_reservation_update()", inspect.getsource(res.URYTableReservation.on_update))

# VEYQON reports / dashboard scoping tests (Phase 9: RS-1..RS-4, R1).
# Mock style, like test_veyqon_gapfill_safety.py.

import inspect
from contextlib import ExitStack, contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.ury.api import ury_dashboard as dash
from ury.ury.api import ury_service_line as sl
from ury.ury.report_api import commission, items, operations, utils

POS = "ury.ury_pos.api"
DASHBOARD_ENDPOINTS = (
	"get_dashboard_stats", "get_cancelled_invoices_count", "get_needs_attention", "get_shift_metrics",
	"get_department_activity", "get_baseline", "get_baseline_comparison", "get_floor_load",
)


@contextmanager
def _as(roles, branches=("B1",), user="u@x.test"):
	"""Session user with these roles and URY User rows on these branches."""
	with ExitStack() as stack:
		stack.enter_context(patch("frappe.get_roles", return_value=list(roles)))
		stack.enter_context(patch("frappe.session", SimpleNamespace(user=user), create=True))
		stack.enter_context(patch(f"{POS}.user_branches", return_value=list(branches)))
		stack.enter_context(patch.object(utils, "user_branches", return_value=list(branches)))
		for mod in (commission, items, operations):  # the stub's getdate returns strings
			stack.enter_context(patch.object(mod, "validate_date_range"))
		get_branch = stack.enter_context(patch(f"{POS}.getBranch"))
		if branches:
			get_branch.return_value = branches[0]
		else:
			get_branch.side_effect = frappe.ValidationError("User is not Associated with any Branch")
		yield


class TestDashboardScope(FrappeTestCase):
	"""R1: dashboard role and one of the user's branches on every ury_dashboard stats endpoint."""

	def test_every_endpoint_checks_first(self):
		for name in DASHBOARD_ENDPOINTS:
			src = inspect.getsource(getattr(dash, name))
			check = src.index("require_dashboard_branch(branch)")
			reads = [i for i in (src.find(t) for t in ("frappe.cache", "frappe.db", "frappe.get_all", "frappe.has_permission")) if i >= 0]
			self.assertLess(check, min(reads), name)

	def test_role_required(self):
		with _as(["URY Chef"]), self.assertRaises(frappe.PermissionError):
			utils.require_dashboard_branch("B1")

	def test_branch_required_and_own_only(self):
		for branch in (None, "", "B2"):
			with _as(["URY Cashier"]), self.assertRaises(frappe.PermissionError, msg=branch):
				utils.require_dashboard_branch(branch)

	def test_own_and_second_branch_ok(self):
		with _as(["URY Cashier"], branches=("B1", "B2")):
			utils.require_dashboard_branch("B1")
			utils.require_dashboard_branch("B2")

	def test_owner_without_ury_user_row_is_permission_error(self):
		# review M3: a 403 the UI maps to its no-access state, not getBranch()'s ValidationError
		with _as(["URY Owner"], branches=()), self.assertRaises(frappe.PermissionError) as ctx:
			utils.require_dashboard_branch("B1")
		self.assertIn("Ask an administrator to add you as a URY User", str(ctx.exception))

	def test_superuser_may_aggregate(self):
		with _as(["System Manager"], branches=()):
			utils.require_dashboard_branch(None)

	def test_endpoint_refuses_before_cache(self):
		cache = MagicMock()
		with _as(["URY Manager"]), patch.object(dash.frappe, "cache", cache, create=True):
			with self.assertRaises(frappe.PermissionError):
				dash.get_floor_load("B2")
		cache.assert_not_called()


class TestServiceLineScope(FrappeTestCase):
	"""RS-4: branch access on service line / running low; deterministic POS Profile."""

	def test_other_branch_denied(self):
		for fn in (sl.get_service_line, sl.get_running_low):
			with _as(["URY Cashier"]), self.assertRaises(frappe.PermissionError, msg=fn.__name__):
				fn("B2")
			with _as(["URY Cashier"]), self.assertRaises(frappe.PermissionError, msg=fn.__name__):
				fn(None)

	def test_running_low_uses_enabled_profile_first_by_name(self):
		get_all = MagicMock(return_value=[frappe._dict(warehouse="WH-1")])
		cache = MagicMock()
		cache.return_value.get_value.return_value = None
		sql = MagicMock(return_value=[frappe._dict(item_code="I1", item_name="I1", qty_sold=4)])
		with _as(["URY Cashier"]), patch.object(sl.frappe, "get_all", get_all), \
			patch.object(sl.frappe, "cache", cache, create=True), patch.object(sl.frappe.db, "sql", sql, create=True), \
			patch.object(sl, "today", return_value="2026-11-14"), 			patch.object(sl, "get_datetime", side_effect=[datetime(2026, 11, 14), datetime(2026, 11, 14, 4)]), 			patch.object(sl, "_bin_actual_qty", return_value=10) as bin_qty:
			out = sl.get_running_low("B1")
		get_all.assert_called_once_with(
			"POS Profile", filters={"branch": "B1", "disabled": 0}, fields=["warehouse"], order_by="name asc", limit=1
		)
		bin_qty.assert_called_once_with("I1", "WH-1")
		self.assertEqual(out[0]["remaining"], 10)


class TestCommissionScope(FrappeTestCase):
	"""RS-1: one of the user's branches; invoices of that branch's company only."""

	def test_branch_required_and_own_only(self):
		for fn, args in (
			(commission.get_employee_commission, ("2026-11-01", "2026-11-14")),
			(commission.get_employee_commission_detail, ("E1", "2026-11-01", "2026-11-14")),
		):
			for branch in (None, "B2"):
				with _as(["URY Manager"]), self.assertRaises(frappe.PermissionError, msg=(fn.__name__, branch)):
					fn(*args, branch=branch)

	def _fetch(self, company):
		sql = MagicMock(return_value=[])
		with patch.object(commission.frappe.db, "get_value", return_value=company, create=True), \
			patch.object(commission.frappe.db, "sql", sql, create=True), \
			patch.object(commission, "report_settings_join", return_value=""), \
			patch.object(commission, "get_business_day_range_condition", return_value="1=1"):
			commission._fetch_invoices("2026-11-01", "2026-11-14", "B1", {"commission_base": "Net Total", "include_returns": False})
		return sql

	def test_invoices_filtered_by_branch_company(self):
		sql = self._fetch("C1")
		self.assertIn("b.`company` = %(company)s", sql.call_args.args[0])
		self.assertEqual(sql.call_args.args[1]["company"], "C1")

	def test_branch_without_company_fails_closed(self):
		with self.assertRaises(frappe.ValidationError):
			self._fetch(None)


class TestUserCompanies(FrappeTestCase):
	def test_superuser_unlimited(self):
		with _as(["System Manager"], branches=()):
			self.assertIsNone(utils.user_companies())

	def test_companies_of_user_branches(self):
		get_all = MagicMock(return_value=["C2", "C1", None, "C1"])
		with _as(["URY Manager"], branches=("B1", "B2")), patch.object(utils.frappe, "get_all", get_all):
			self.assertEqual(utils.user_companies(), ["C1", "C2"])
		self.assertEqual(get_all.call_args.kwargs["filters"], {"name": ["in", ["B1", "B2"]]})

	def test_no_branch_no_company(self):
		with _as(["URY Manager"], branches=()):
			self.assertEqual(utils.user_companies(), [])


class TestWorkOrderScope(FrappeTestCase):
	"""RS-2: Work Order read, companies of the user's branches."""

	def _call(self, companies, readable=True):
		sql = MagicMock(return_value=[])
		with _as(["URY Manager"]), patch.object(operations.frappe, "has_permission", return_value=readable, create=True), \
			patch.object(operations, "user_companies", return_value=companies), \
			patch.object(operations.frappe.db, "sql", sql, create=True):
			out = operations.get_completed_work_orders("2026-11-01", "2026-11-14")
		return out, sql

	def test_read_permission_required(self):
		with self.assertRaises(frappe.PermissionError):
			self._call(["C1"], readable=False)

	def test_company_filter(self):
		_, sql = self._call(["C1"])
		self.assertIn("`company` IN %(companies)s", sql.call_args.args[0])
		self.assertEqual(sql.call_args.args[1]["companies"], ["C1"])

	def test_no_company_reads_nothing(self):
		out, sql = self._call([])
		sql.assert_not_called()
		self.assertEqual(out["work_orders"], [])

	def test_superuser_unfiltered(self):
		_, sql = self._call(None)
		self.assertNotIn("%(companies)s", sql.call_args.args[0])


class TestPurchaseHistoryScope(FrappeTestCase):
	"""RS-3: Purchase Invoice read, companies of the user's branches, never other companies."""

	def _call(self, companies, readable=True):
		sql = MagicMock(side_effect=lambda q, *a, **k: [{"total": 0, "total_qty": 0, "total_amount": 0}] if "AS total" in q else [])
		with _as(["URY Manager"]), patch.object(items.frappe, "has_permission", return_value=readable, create=True), \
			patch.object(items, "user_companies", return_value=companies), \
			patch.object(items.frappe.db, "sql", sql, create=True):
			out = items.get_item_wise_purchase_history("2026-11-01", "2026-11-14")
		return out, sql

	def test_read_permission_required(self):
		with self.assertRaises(frappe.PermissionError):
			self._call(["C1"], readable=False)

	def test_every_query_company_filtered(self):
		out, sql = self._call(["C1"])
		self.assertEqual(sql.call_count, 3)
		for c in sql.call_args_list:
			self.assertIn("a.`company` IN %(companies)s", c.args[0])
			self.assertEqual(c.args[1]["companies"], ["C1"])
		self.assertEqual(out["branch_scope"], "company")

	def test_no_company_matches_nothing(self):
		_, sql = self._call([])
		for c in sql.call_args_list:
			self.assertEqual(c.args[1]["companies"], [""])

	def test_superuser_unfiltered(self):
		_, sql = self._call(None)
		for c in sql.call_args_list:
			self.assertNotIn("%(companies)s", c.args[0])

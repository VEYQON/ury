# VEYQON shared-tenant patch tests for POS / Captain endpoints (P-5, P-9, P-15, SC-1)
# and the branch / billing helpers in ury.ury_pos.api.

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.ury_pos import api as pos_api
from ury.ury.api import ury_print
from ury.ury.doctype.sub_pos_closing.sub_pos_closing import SubPOSClosing

POS = "ury.ury_pos.api"
PRINT = "ury.ury.api.ury_print"
SPC = "ury.ury.doctype.sub_pos_closing.sub_pos_closing"


def _as(user, roles=(), branches=("B1",)):
	"""Patch the session user, roles and URY User branches seen by ury.ury_pos.api."""
	session = patch(f"{POS}.frappe.session", frappe._dict(user=user))
	get_roles = patch(f"{POS}.frappe.get_roles", return_value=list(roles))
	user_branches = patch(f"{POS}.user_branches", return_value=list(branches))
	return session, get_roles, user_branches


class _Ctx:
	def __init__(self, *patchers):
		self.patchers = patchers

	def __enter__(self):
		return [p.start() for p in self.patchers]

	def __exit__(self, *a):
		for p in self.patchers:
			p.stop()


class TestBranchAndBillingHelpers(FrappeTestCase):
	def test_same_branch_passes(self):
		with _Ctx(*_as("c@x"), patch(f"{POS}.getBranch", return_value="B1")):
			pos_api.assert_user_branch("B1")

	def test_other_branch_refused(self):
		with _Ctx(*_as("c@x"), patch(f"{POS}.getBranch", return_value="B1")):
			with self.assertRaises(frappe.PermissionError):
				pos_api.assert_user_branch("B2")

	def test_second_branch_of_multi_branch_user_passes(self):
		# review fix #4: any of the user's URY User branches
		with _Ctx(*_as("c@x", branches=["B1", "B2"]), patch(f"{POS}.getBranch", return_value="B1")):
			pos_api.assert_user_branch("B2")

	def test_no_ury_user_row_is_permission_error(self):
		# review M3: owners/admins/managers without a URY User row get a 403, not getBranch()'s ValidationError
		with _Ctx(*_as("o@x", ["URY Owner"], branches=()), patch(f"{POS}.getBranch", side_effect=AssertionError)):
			with self.assertRaises(frappe.PermissionError) as ctx:
				pos_api.assert_user_branch("B1", "Not permitted to print a bill of another branch.")
		self.assertIn("You are not assigned to branch B1", str(ctx.exception))

	def test_other_branch_names_branch(self):
		with _Ctx(*_as("c@x")), self.assertRaises(frappe.PermissionError) as ctx:
			pos_api.assert_user_branch("B2")
		self.assertIn("You are not assigned to branch B2. Ask an administrator", str(ctx.exception))

	def test_user_branches_reads_every_row(self):
		with patch(f"{POS}.frappe.session", frappe._dict(user="c@x")), 			 patch(f"{POS}.frappe.db.sql", return_value=[("B1",), ("B2",)], create=True) as sql:
			self.assertEqual(pos_api.user_branches(), ["B1", "B2"])
		self.assertEqual(sql.call_args[0][1], "c@x")

	def test_missing_branch_refused(self):
		with _Ctx(*_as("c@x"), patch(f"{POS}.getBranch", return_value="B1")):
			with self.assertRaises(frappe.PermissionError):
				pos_api.assert_user_branch(None)

	def test_system_manager_skips_branch(self):
		with _Ctx(*_as("m@x", ["System Manager"]), patch(f"{POS}.getBranch", side_effect=AssertionError)):
			pos_api.assert_user_branch("B2")

	def test_billing_role_on_profile(self):
		with _Ctx(*_as("c@x", ["URY Cashier"])), patch(f"{POS}.frappe.get_all", return_value=["URY Cashier"]) as get_all:
			pos_api.assert_billing_user("POS-1")
		self.assertEqual(get_all.call_args.kwargs["filters"]["parentfield"], ["in", ["role_allowed_for_billing"]])

	def test_no_billing_role_refused(self):
		with _Ctx(*_as("w@x", ["URY Captain"]), patch(f"{POS}.frappe.get_all", return_value=["URY Cashier"])):
			with self.assertRaises(frappe.PermissionError):
				pos_api.assert_billing_user("POS-1")

	def test_no_profile_refused(self):
		with _Ctx(*_as("c@x", ["URY Cashier"])):
			with self.assertRaises(frappe.PermissionError):
				pos_api.assert_billing_user(None)


class TestOpeningEntryProfileScope(FrappeTestCase):
	"""P-9: the profile must list the user (or list nobody) and sit on the user's branch."""

	def _profile(self, users=(), branch="B1"):
		doc = MagicMock()
		doc.branch, doc.restaurant, doc.company = branch, "R1", "C1"
		doc.get.return_value = [frappe._dict(user=u) for u in users]
		return doc

	def _open(self, profile, user="c@x"):
		opening = MagicMock()
		get_doc = lambda dt, *a, **k: profile if dt == "POS Profile" else opening
		with _Ctx(*_as(user), patch(f"{POS}.frappe.has_permission", return_value=True),
				  patch(f"{POS}.frappe.get_doc", side_effect=get_doc),
				  patch(f"{POS}.getBranch", return_value="B1"),
				  patch(f"{POS}.frappe.utils.get_datetime", return_value="2026-11-14 08:00:00"),
				  patch(f"{POS}.frappe.utils.getdate", return_value="2026-11-14")):
			pos_api.create_pos_opening_entry("POS-1")
		return opening

	def test_listed_user_on_own_branch_opens(self):
		self._open(self._profile(users=["c@x"])).submit.assert_called_once()

	def test_profile_without_users_opens(self):
		self._open(self._profile()).submit.assert_called_once()

	def test_unlisted_user_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._open(self._profile(users=["other@x"]))

	def test_other_branch_profile_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._open(self._profile(branch="B2"))


class TestBillPrintGate(FrappeTestCase):
	"""P-5: only a billing user of the invoice's branch may mark a bill printed."""

	def _invoice(self, branch="B1"):
		inv = MagicMock()
		inv.pos_profile, inv.branch = "POS-1", branch
		return inv

	def _ctx(self, invoice, roles=("URY Cashier",), allowed=("URY Cashier",)):
		return _Ctx(*_as("c@x", roles), patch(f"{POS}.getBranch", return_value="B1"),
					patch(f"{POS}.frappe.get_all", return_value=list(allowed)),
					patch(f"{PRINT}.frappe.get_doc", return_value=invoice),
					patch(f"{PRINT}.frappe.has_permission", return_value=True),
					patch(f"{PRINT}.frappe.db.set_value"))

	def test_captain_cannot_mark_printed(self):
		inv = self._invoice()
		with self._ctx(inv, roles=("URY Captain",)) as mocks:
			with self.assertRaises(frappe.PermissionError):
				ury_print.qz_print_update("INV-1")
		mocks[-1].assert_not_called()

	def test_other_branch_cannot_mark_printed(self):
		with self._ctx(self._invoice(branch="B2")) as mocks:
			with self.assertRaises(frappe.PermissionError):
				ury_print.qz_print_update("INV-1")
		mocks[-1].assert_not_called()

	def test_billing_user_marks_printed_and_is_recorded(self):
		inv = self._invoice()
		trigger = MagicMock()
		with self._ctx(inv) as mocks, \
			 patch(f"{PRINT}.frappe.db.get_value", side_effect=[None, 0, 1]), \
			 patch("ury.ury.api.ury_fulfilment_posting_service.trigger_manufacture_on_bill_print", trigger, create=True):
			self.assertEqual(ury_print.qz_print_update("INV-1"), {"status": "Success"})
		mocks[-1].assert_called_once()
		inv.add_comment.assert_called_once()

	def test_print_pos_page_gates_the_invoice_whatever_the_doctype(self):
		with self._ctx(self._invoice(branch="B2")) as mocks:
			with self.assertRaises(frappe.PermissionError):
				ury_print.print_pos_page("Note", "INV-1", "Standard")
		mocks[-1].assert_not_called()

	def _network(self, inv, printers=("P-1",), access_error=None, doctype="POS Invoice", printer="P-1", roles=("URY Captain",)):
		engine = MagicMock()
		engine.print_doc.return_value = {"status": "Failure"}
		get_doc = lambda dt, name: inv if dt == "POS Invoice" else engine
		with self._ctx(inv, roles=roles), patch(f"{PRINT}.frappe.get_doc", side_effect=get_doc), 			 patch(f"{PRINT}.validate_print_permission"), 			 patch(f"{PRINT}._enforce_order_access", side_effect=access_error) as access, 			 patch(f"{PRINT}._invoice_printers", return_value=set(printers)), 			 patch(f"{PRINT}.frappe.db.get_value", return_value="Std"):
			ury_print.network_printing(doctype, "INV-1", printer, doc=frappe._dict(pos_profile="EVIL"))
		return engine, access

	def test_captain_with_order_access_prints_bill(self):
		# review fix #1: order access + branch, no billing role needed
		inv = self._invoice()
		engine, access = self._network(inv)
		access.assert_called_once()
		self.assertIs(access.call_args[0][0], inv)
		self.assertIsNone(engine.print_doc.call_args.kwargs["doc"])  # review fix #7: client doc ignored

	def test_captain_without_order_access_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._network(self._invoice(), access_error=frappe.PermissionError)

	def test_captain_other_branch_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._network(self._invoice(branch="B2"))

	def test_printer_not_on_profile_or_room_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._network(self._invoice(), printers=("Kitchen-Of-Other-Branch",))

	def test_network_printing_only_for_pos_invoice(self):
		with self.assertRaises(frappe.PermissionError):
			self._network(self._invoice(), doctype="Sales Invoice")

	def test_invoice_printers_cover_profile_and_room(self):
		inv = frappe._dict(pos_profile="POS-1", restaurant_table="T1")
		rows = {"POS-1": ["P-1"], "R1": ["P-2"]}
		with patch(f"{PRINT}.frappe.db.get_value", return_value="R1"), 			 patch(f"{PRINT}.frappe.get_all", side_effect=lambda dt, filters, pluck: rows[filters["parent"]]):
			self.assertEqual(ury_print._invoice_printers(inv), {"P-1", "P-2"})

	def test_print_pos_page_publishes_to_branch_users_only(self):
		inv = self._invoice()
		with self._ctx(inv), \
			 patch(f"{PRINT}.frappe.get_all", side_effect=lambda dt, **k: ["a@x", "b@x"] if dt == "URY User" else ["URY Cashier"]), \
			 patch(f"{PRINT}.frappe.db.get_value", side_effect=[("T1", "B1", "INV-1"), 1]), \
			 patch(f"{PRINT}.frappe.publish_realtime", create=True) as publish, \
			 patch("ury.ury.api.ury_fulfilment_posting_service.trigger_manufacture_on_bill_print", create=True):
			ury_print.print_pos_page("POS Invoice", "INV-1", "Standard")
		self.assertEqual([c.kwargs["user"] for c in publish.call_args_list], ["a@x", "b@x"])


class _Doc(frappe._dict):
	def set(self, key, value):
		self[key] = [frappe._dict(v) for v in value]

	def append(self, key, value):
		self.setdefault(key, []).append(frappe._dict(value))


class TestSubPosClosingRecompute(FrappeTestCase):
	"""SC-1: owner enforced, opening entry must be the user's open session, totals recomputed."""

	def _opening(self, **kw):
		values = dict(docstatus=1, status="Open", user="c@x", pos_profile="POS-1", period_start_date="2026-11-14 08:00:00",
			balance_details=[frappe._dict(mode_of_payment="Cash", opening_amount=100)])
		values.update(kw)
		return frappe._dict(values)

	def _check(self, doc, opening, session_user="c@x", roles=()):
		with patch(f"{SPC}.frappe.session", frappe._dict(user=session_user)), \
			 patch(f"{SPC}.frappe.get_roles", return_value=list(roles)), \
			 patch(f"{SPC}.frappe.get_doc", return_value=opening):
			return SubPOSClosing._validate_owner_and_opening(doc)

	def test_cannot_close_another_cashiers_till(self):
		doc = _Doc(user="other@x", pos_opening_entry="OPE-1", pos_profile="POS-1")
		with self.assertRaises(frappe.PermissionError):
			self._check(doc, self._opening(user="other@x"))

	def test_supervisor_may_close_for_a_cashier(self):
		doc = _Doc(user="other@x", pos_opening_entry="OPE-1", pos_profile="POS-1")
		self._check(doc, self._opening(user="other@x"), session_user="mgr@x", roles=["URY Manager"])
		self.assertEqual(doc.period_start_date, "2026-11-14 08:00:00")

	def test_opening_of_another_user_refused(self):
		doc = _Doc(user="c@x", pos_opening_entry="OPE-1", pos_profile="POS-1")
		with self.assertRaises(frappe.ValidationError):
			self._check(doc, self._opening(user="other@x"))

	def test_closed_opening_refused(self):
		doc = _Doc(user="c@x", pos_opening_entry="OPE-1", pos_profile="POS-1")
		with self.assertRaises(frappe.ValidationError):
			self._check(doc, self._opening(status="Closed"))

	def test_client_amounts_are_recomputed(self):
		doc = _Doc(payment_reconciliation=[
			frappe._dict(mode_of_payment="Cash", opening_amount=999, expected_amount=50, closing_amount=150, difference=0),
		], grand_total=1)
		invoices = [frappe._dict(name="INV-1", grand_total=60, net_total=50, total_qty=2, change_amount=10,
			account_for_change_amount="Cash Acc", payments=[
				frappe._dict(mode_of_payment="Cash", amount=70, account="Cash Acc"),
				frappe._dict(mode_of_payment="Card", amount=0, account="Card Acc"),
			])]
		SubPOSClosing._set_totals(doc, self._opening(), invoices)
		cash, card = doc.payment_reconciliation
		self.assertEqual((cash.opening_amount, cash.expected_amount, cash.difference), (100, 160, -10))
		self.assertEqual((card.mode_of_payment, card.expected_amount, card.difference), ("Card", 0, 0))
		self.assertEqual((doc.grand_total, doc.net_total, doc.total_quantity), (60, 50, 2))
		self.assertEqual([t.pos_invoice for t in doc.pos_transactions], ["INV-1"])

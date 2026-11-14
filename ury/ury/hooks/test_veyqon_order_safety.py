# VEYQON shared-tenant patch tests for orders / KOT generate (P-1..P-4, C-1, C-2, C-4)
# and the realtime rooms of change_table_in_kot, reload_ro and pos_invoice_updated.

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.ury.api import ury_kot_generate as kot
from ury.ury.doctype.ury_order import ury_order as order
from ury.ury.hooks import ury_pos_invoice as hook

POS = "ury.ury_pos.api"
ORD = "ury.ury.doctype.ury_order.ury_order"
KOT = "ury.ury.api.ury_kot_generate"
HOOK = "ury.ury.hooks.ury_pos_invoice"


class _Ctx:
	def __init__(self, *patchers):
		self.patchers = patchers

	def __enter__(self):
		return [p.start() for p in self.patchers]

	def __exit__(self, *a):
		for p in self.patchers:
			p.stop()


def _as(user="c@x", roles=("URY Cashier",), branch="B1", allowed=("URY Cashier",)):
	"""Session user, roles, URY branch and the roles the POS Profile allows."""
	return (
		patch(f"{POS}.frappe.session", frappe._dict(user=user)),
		patch(f"{POS}.frappe.get_roles", return_value=list(roles)),
		patch(f"{POS}.getBranch", return_value=branch),
		patch(f"{POS}.user_branches", return_value=[branch], create=True),
		patch(f"{POS}.frappe.get_all", return_value=list(allowed)),
	)


class _Inv(frappe._dict):
	pass


def _invoice(**kw):
	inv = _Inv(name="INV-1", docstatus=0, branch="B1", pos_profile="POS-1", is_return=0,
			   rounded_total=100, grand_total=99.6, paid_amount=100, write_off_amount=0,
			   restaurant_table="T1", custom_merged_tables=None, custom_merged_pos_invoice=None)
	inv.update(kw)
	inv.submit = MagicMock()
	inv.save = MagicMock()
	inv.flags = frappe._dict()
	return inv


def _has_perm(allowed):
	def fn(*a, throw=False, **k):
		if not allowed and throw:
			raise frappe.PermissionError
		return allowed
	return fn


class TestMakeInvoiceGate(FrappeTestCase):
	"""P-1: billing role on the profile, invoice and profile on the user's branch."""

	def _run(self, inv, profile_branch="B1", **who):
		with _Ctx(*_as(**who),
				  patch(f"{ORD}._validate_additional_discount", return_value=0),
				  patch(f"{ORD}.frappe.get_value", return_value="Dine In"),
				  patch(f"{ORD}.get_order_invoice", return_value=inv),
				  patch(f"{ORD}.frappe.db.get_value", return_value=profile_branch)):
			return order.make_invoice("C", [], "c@x", "POS-1", "c@x", invoice="INV-1", submit_invoice=0)

	def test_captain_cannot_bill(self):
		inv = _invoice()
		with self.assertRaises(frappe.PermissionError):
			self._run(inv, roles=("URY Captain",))
		inv.save.assert_not_called()

	def test_other_branch_invoice_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._run(_invoice(branch="B2"))

	def test_other_branch_profile_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._run(_invoice(), profile_branch="B2")


class TestSubmitOrderInvoiceGate(FrappeTestCase):
	"""P-2: submit right, billing role, own branch, fully paid."""

	def _run(self, inv, perm=True, partial=0, **who):
		with _Ctx(*_as(**who),
				  patch(f"{ORD}.frappe.get_doc", return_value=inv),
				  patch(f"{ORD}.frappe.has_permission", side_effect=_has_perm(perm)),
				  patch(f"{ORD}.frappe.db.get_value", return_value=partial),
				  patch(f"{ORD}._free_tables_if_no_open_invoices")):
			return order.submit_order_invoice("INV-1")

	def test_full_payment_with_rounding_submits(self):
		inv = _invoice()
		self.assertEqual(self._run(inv)["status"], "Submitted")
		inv.submit.assert_called_once()

	def test_change_returned_submits(self):
		inv = _invoice(paid_amount=500)
		self._run(inv)
		inv.submit.assert_called_once()

	def test_grand_total_used_when_rounding_off(self):
		inv = _invoice(rounded_total=0, grand_total=99.6, paid_amount=99.6)
		self._run(inv)
		inv.submit.assert_called_once()

	def test_zero_total_submits(self):
		inv = _invoice(rounded_total=0, grand_total=0, paid_amount=0)
		self._run(inv)
		inv.submit.assert_called_once()

	def test_partial_payment_refused(self):
		inv = _invoice(paid_amount=99.6)
		with self.assertRaises(frappe.ValidationError):
			self._run(inv)
		inv.submit.assert_not_called()

	def test_profile_allowing_partial_payment_submits(self):
		inv = _invoice(paid_amount=10)
		self._run(inv, partial=1)
		inv.submit.assert_called_once()

	def test_no_submit_permission_refused(self):
		inv = _invoice()
		with self.assertRaises(frappe.PermissionError):
			self._run(inv, perm=False)
		inv.submit.assert_not_called()

	def test_captain_cannot_submit(self):
		with self.assertRaises(frappe.PermissionError):
			self._run(_invoice(), roles=("URY Captain",))

	def test_other_branch_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._run(_invoice(branch="B2"))


class TestTableMergeAccess(FrappeTestCase):
	"""P-4 / C-1: own branch, then URY Table write or a transfer/billing role."""

	def _run(self, write=False, table_branch="B1", allowed=(), **who):
		def get_all(doctype, **k):
			return ["POS-1"] if doctype == "POS Profile" else list(allowed)
		with _Ctx(*_as(**who),
				  patch(f"{ORD}.frappe.db.get_value", return_value=table_branch),
				  patch(f"{ORD}.frappe.has_permission", return_value=write),
				  patch(f"{ORD}.frappe.get_all", side_effect=get_all)):
			order._assert_table_merge_access("T1")

	def test_other_branch_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._run(write=True, table_branch="B2")

	def test_table_write_passes(self):
		self._run(write=True, roles=("URY Captain",), allowed=())

	def test_transfer_or_billing_role_passes(self):
		self._run(roles=("URY Captain",), allowed=("URY Captain",))

	def test_no_write_no_role_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._run(roles=("URY Captain",), allowed=("URY Cashier",))

	def test_merge_and_unmerge_call_the_gate(self):
		with patch(f"{ORD}._assert_table_merge_access", side_effect=frappe.PermissionError) as gate, \
			 patch(f"{ORD}.frappe.db.get_value", return_value="Hall"):
			with self.assertRaises(frappe.PermissionError):
				order.merge_free_tables("T1", "T2")
			with self.assertRaises(frappe.PermissionError):
				order.unmerge_tables("T1")
		self.assertEqual([c.args[0] for c in gate.call_args_list], ["T1", "T1"])


class TestKotExecuteGate(FrappeTestCase):
	"""P-3 / C-2: own branch, and the KOT list may not exceed the saved invoice."""

	def _inv(self, branch="B1"):
		return _Inv(name="INV-1", branch=branch, pos_profile="POS-1",
					items=[frappe._dict(item_code="I1", qty=2), frappe._dict(item_code="I1", qty=1)])

	def test_items_within_invoice_pass(self):
		kot._assert_items_on_invoice(self._inv(), [{"item": "I1", "qty": 3}, {"item_code": "I2", "qty": 0}])

	def test_item_not_on_invoice_refused(self):
		with self.assertRaises(frappe.PermissionError):
			kot._assert_items_on_invoice(self._inv(), [{"item": "I9", "qty": 1}])

	def test_qty_above_invoice_refused(self):
		with self.assertRaises(frappe.PermissionError):
			kot._assert_items_on_invoice(self._inv(), [{"item": "I1", "qty": 2}, {"item": "I1", "qty": 1.5}])

	def test_negative_qty_refused(self):
		with self.assertRaises(frappe.ValidationError):
			kot._assert_items_on_invoice(self._inv(), [{"item": "I1", "qty": -1}])

	def test_other_branch_invoice_refused(self):
		with _Ctx(*_as(),
				  patch(f"{KOT}.frappe.get_doc", return_value=self._inv(branch="B2")),
				  patch(f"{KOT}.frappe.has_permission", return_value=True)), \
			 patch(f"{KOT}.process_items_for_kot") as make_kot:
			with self.assertRaises(frappe.PermissionError):
				kot.kot_execute("INV-1", "C", "T1", [{"item": "I1", "qty": 1}], [])
		make_kot.assert_not_called()


class TestRealtimeRooms(FrappeTestCase):
	def test_change_table_in_kot_publishes_to_kot_room(self):
		with patch(f"{ORD}.frappe.get_all", return_value=[frappe._dict(name="K1")]), \
			 patch(f"{ORD}.frappe.db.set_value"), \
			 patch(f"{ORD}.frappe.db.get_value", return_value="P1"), \
			 patch(f"{ORD}.frappe.publish_realtime", create=True) as pub:
			order.change_table_in_kot("INV-1", "T2", "B1")
		pub.assert_called_once_with("kot_update_B1_P1", room="doctype:URY KOT")

	def test_reload_ro_publishes_to_pos_invoice_room(self):
		with patch(f"{HOOK}.frappe.publish_realtime", create=True) as pub:
			hook.ro_reload_submit(frappe._dict(name="INV-1"), None)
		self.assertEqual(pub.call_args.kwargs["room"], "doctype:POS Invoice")

	def test_pos_invoice_updated_publishes_to_pos_invoice_room(self):
		target = _invoice(name="INV-2", paid_amount=0, invoice_printed=1)
		doc = _invoice(custom_merged_pos_invoice="INV-2", paid_amount=0, invoice_printed=1)
		with patch(f"{HOOK}.frappe.flags", frappe._dict()), \
			 patch(f"{HOOK}.frappe.db.exists", return_value=True), \
			 patch(f"{HOOK}.frappe.get_doc", return_value=target), \
			 patch(f"{HOOK}.frappe.publish_realtime", create=True) as pub:
			hook.sync_merged_invoice(doc)
		self.assertEqual(pub.call_args.args[0], "pos_invoice_updated")
		self.assertEqual(pub.call_args.kwargs["room"], "doctype:POS Invoice")


class _Draft(frappe._dict):
	def __init__(self, before=None, **kw):
		super().__init__(**kw)
		self._before = before

	def get_doc_before_save(self):
		return self._before


def _row(code="I1", qty=1, rate=10):
	return frappe._dict(item_code=code, qty=qty, rate=rate, discount_percentage=0, discount_amount=0)


def _draft(items, before_items=None, new=False, **kw):
	fields = dict(restaurant="R1", restaurant_table="T1", pos_profile="POS-1", is_return=0,
				  additional_discount_percentage=0, discount_amount=0, apply_discount_on="Grand Total")
	fields.update(kw)
	before = None if new else _Draft(docstatus=0, items=before_items, **fields)
	return _Draft(before, docstatus=0, items=items, **fields)


class TestUryItemGuard(FrappeTestCase):
	"""C-4: URY draft items/prices change only through the URY APIs."""

	def _run(self, doc, flags=None, roles=("URY Captain",), profile_restaurant="R1"):
		with _Ctx(*_as(user="w@x", roles=roles),
				  patch(f"{HOOK}.frappe.flags", frappe._dict(flags or {})),
				  patch(f"{HOOK}.frappe.db.get_value", return_value=profile_restaurant)):
			hook.guard_ury_item_changes(doc)

	def test_rate_change_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._run(_draft([_row(rate=1)], [_row(rate=10)]))

	def test_added_item_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._run(_draft([_row(), _row("I2")], [_row()]))

	def test_header_discount_refused(self):
		doc = _draft([_row()], [_row()])
		doc.additional_discount_percentage = 50
		with self.assertRaises(frappe.PermissionError):
			self._run(doc)

	def test_unchanged_items_pass(self):
		self._run(_draft([_row(), _row("I2")], [_row("I2"), _row()]))

	def test_new_invoice_with_items_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._run(_draft([_row()], new=True))

	def test_new_empty_invoice_passes(self):
		self._run(_draft([], new=True))

	def test_ury_api_flags_pass(self):
		for flag in hook.URY_ITEM_WRITE_FLAGS:
			self._run(_draft([_row(rate=1)], [_row()]), flags={flag: True})

	def test_superuser_passes(self):
		self._run(_draft([_row(rate=1)], [_row()]), roles=("System Manager",))

	def test_return_passes(self):
		self._run(_draft([_row(qty=-1)], new=True, is_return=1))

	def test_non_ury_invoice_untouched(self):
		doc = _draft([_row(rate=1)], [_row()], restaurant=None, restaurant_table=None)
		self._run(doc, profile_restaurant=None)

	def test_takeaway_on_restaurant_profile_guarded(self):
		doc = _draft([_row(rate=1)], [_row()], restaurant=None, restaurant_table=None)
		with self.assertRaises(frappe.PermissionError):
			self._run(doc)

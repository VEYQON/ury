# VEYQON payment safety tests (S9-25 online settlement, G-13 kiosk terminal payment).
# Mock style, like test_veyqon_selforder_safety.py.

import hashlib
import hmac
import inspect
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.ury.api import payment_terminal as pt
from ury.ury.api import self_ordering as so
from ury.ury.doctype.ury_payment_terminal_transaction.ury_payment_terminal_transaction import (
	URYPaymentTerminalTransaction,
)

SO = "ury.ury.api.self_ordering"
PT = "ury.ury.api.payment_terminal"
SECRET = "whsec"


def _ns(**kw):
	return SimpleNamespace(**kw)


def _pr(**kw):
	base = dict(name="PR-1", docstatus=1, status="Requested", grand_total=250.0, currency="INR",
		reference_doctype="POS Invoice", reference_name="INV-1")
	base.update(kw)
	return _ns(**base)


def _decorators(fn):
	src = inspect.getsource(inspect.unwrap(fn)).splitlines()
	return [line.split("  #")[0].strip() for line in src if line.strip().startswith("@")]


class TestEndpointsArePostOnly(FrappeTestCase):
	def test_new_endpoints_post_only_and_take_only_session_and_attempt(self):
		for fn in (pt.start_terminal_payment, pt.get_terminal_payment_status, pt.cancel_terminal_payment):
			self.assertEqual(_decorators(fn), ['@frappe.whitelist(allow_guest=True, methods=["POST"])'])
			# the guest never sends a status, amount or terminal; only its own attempt key (VEYQON: L)
			self.assertEqual(list(inspect.signature(fn).parameters), ["session", "attempt"])
		self.assertEqual(_decorators(so.payment_webhook), ['@frappe.whitelist(allow_guest=True, methods=["POST"])'])
		self.assertEqual(list(inspect.signature(so.payment_webhook).parameters), [])
		# H1: staff endpoint, never guest
		self.assertEqual(_decorators(so.get_paid_online), ['@frappe.whitelist(methods=["POST"])'])


def _settle_db(pr, invoice_docstatus=0, other_paid=None, paid_ref=None, calls=None):
	"""frappe.db.get_value stand-in for settle/fail: ref read, invoice lock, request lock, lookups."""
	def gv(doctype, filters=None, fields=None, **kw):
		if calls is not None:
			calls.append((doctype, bool(kw.get("for_update"))))
		if doctype == "POS Invoice":
			return invoice_docstatus
		if doctype == "Comment":
			return so._PAID_REF + paid_ref if paid_ref else None
		if isinstance(filters, dict):  # _paid_request(invoice)
			return other_paid
		return SimpleNamespace(**vars(pr)) if pr else None
	return gv


class TestSettle(FrappeTestCase):
	def _settle(self, pr, amount=250.0, currency="INR", reference="ref-1", **db):
		invoice = MagicMock()
		with patch(f"{SO}.frappe.db.get_value", side_effect=_settle_db(pr, **db)), \
			patch(f"{SO}.frappe.db.set_value") as set_value, \
			patch(f"{SO}.frappe.log_error") as log, \
			patch(f"{SO}.frappe.get_doc", return_value=invoice) as get_doc:
			result = so.settle_payment_request("PR-1", amount, currency, reference)
		return result, set_value, invoice, log, get_doc

	def test_marks_paid_comments_the_invoice_and_keeps_the_reference(self):
		result, set_value, invoice, _, get_doc = self._settle(_pr())
		self.assertEqual(result, "Paid")
		set_value.assert_called_once_with("Payment Request", "PR-1", "status", "Paid")
		invoice.add_comment.assert_called_once()
		stored = [c.args[0] for c in get_doc.call_args_list if isinstance(c.args[0], dict)]
		self.assertEqual(stored[0]["content"], so._PAID_REF + "ref-1")
		self.assertEqual(stored[0]["reference_name"], "PR-1")

	def test_amount_mismatch_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._settle(_pr(), amount=1.0)

	def test_currency_mismatch_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._settle(_pr(), currency="USD")

	def test_replay_with_same_reference_is_idempotent(self):
		result, set_value, _, log, _ = self._settle(_pr(status="Paid"), paid_ref="ref-1")
		self.assertEqual(result, "Already Paid")
		set_value.assert_not_called()
		log.assert_not_called()

	def test_h2_same_request_paid_with_other_reference_is_a_mismatch(self):
		with patch(f"{SO}.frappe.log_error") as log, self.assertRaises(frappe.ValidationError):
			with patch(f"{SO}.frappe.db.get_value", side_effect=_settle_db(_pr(status="Paid"), paid_ref="ref-OLD")), \
				patch(f"{SO}.frappe.db.set_value") as set_value:
				so.settle_payment_request("PR-1", 250.0, "INR", "ref-NEW")
		set_value.assert_not_called()
		self.assertEqual(log.call_args.args[1], "Self-Order Payment Mismatch")

	def test_h2_another_request_already_paid_is_a_mismatch(self):
		with patch(f"{SO}.frappe.log_error") as log, \
			patch(f"{SO}.frappe.db.get_value", side_effect=_settle_db(_pr(), other_paid="PR-0")), \
			patch(f"{SO}.frappe.db.set_value") as set_value, self.assertRaises(frappe.ValidationError):
			so.settle_payment_request("PR-1", 250.0, "INR", "r")
		set_value.assert_not_called()
		self.assertEqual(log.call_args.args[1], "Self-Order Payment Mismatch")
		self.assertIn("refund", log.call_args.args[0])

	def test_m1_failed_then_matching_paid_is_accepted(self):
		result, set_value, _, _, _ = self._settle(_pr(status="Failed"))
		self.assertEqual(result, "Paid")
		set_value.assert_called_once_with("Payment Request", "PR-1", "status", "Paid")

	def test_m1_cancelled_or_failed_mismatch_logged_without_throwing(self):
		for pr, amount in ((_pr(status="Cancelled"), 250.0), (_pr(status="Failed"), 1.0)):
			result, set_value, _, log, _ = self._settle(pr, amount=amount)
			self.assertEqual(result, "Mismatch")
			set_value.assert_not_called()
			self.assertEqual(log.call_args.args[1], "Self-Order Payment Mismatch")

	def test_m1_webhook_returns_200_for_a_mismatch(self):
		raw = json.dumps({"payment_request": "PR-1", "status": "Paid", "amount": 1, "currency": "INR"}).encode()
		signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
		with patch(f"{SO}.frappe.conf", {so.PAYMENT_WEBHOOK_SECRET_KEY: SECRET}, create=True), \
			patch(f"{SO}.frappe.request", _ns(get_data=lambda: raw), create=True), \
			patch(f"{SO}.frappe.get_request_header", return_value=signature, create=True), \
			patch(f"{SO}.frappe.db.get_value", side_effect=_settle_db(_pr(status="Failed"))), \
			patch(f"{SO}.frappe.log_error"), patch(f"{SO}.frappe.db.set_value"):
			self.assertEqual(so.payment_webhook(), {"status": "Mismatch"})

	def test_closed_invoice_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._settle(_pr(), invoice_docstatus=1)

	def test_only_submitted_pos_invoice_requests(self):
		for pr in (_pr(docstatus=0), _pr(reference_doctype="Sales Order"), None):
			with patch(f"{SO}.frappe.db.get_value", side_effect=_settle_db(pr)), self.assertRaises(frappe.ValidationError):
				so.settle_payment_request("PR-1", 250.0, "INR")

	def test_h3_invoice_locked_before_request(self):
		calls = []
		with patch(f"{SO}.frappe.db.get_value", side_effect=_settle_db(_pr(status="Paid"), calls=calls)):
			so.settle_payment_request("PR-1", 250.0, "INR")
		locks = [doctype for doctype, locked in calls if locked]
		self.assertEqual(locks[:2], ["POS Invoice", "Payment Request"])

	def test_failure_never_downgrades_paid(self):
		with patch(f"{SO}.frappe.db.get_value", side_effect=_settle_db(_pr(status="Paid"))), \
			patch(f"{SO}.frappe.db.set_value") as set_value:
			self.assertEqual(so.fail_payment_request("PR-1"), "Paid")
		set_value.assert_not_called()


class TestOpenRequestChannels(FrappeTestCase):
	"""VEYQON: H2 -- online and terminal never share a request or run at once; a new one cancels older ones."""

	def _open(self, channel, open_prs=(), terminal_prs=(), pending=False, reuse=None):
		invoice = _ns(name="INV-1", docstatus=0, modified="x")
		new = _ns(name="PR-NEW")

		def get_all(doctype, **kw):
			return list(open_prs) if doctype == "Payment Request" else list(terminal_prs)

		def get_value(doctype, filters=None, *a, **kw):
			if doctype == "POS Invoice":
				return "INV-1"
			if isinstance(filters, dict) and filters.get("status") == "Paid":
				return None
			self.reuse_filters = filters
			return reuse

		make = MagicMock(return_value=new)
		with patch(f"{SO}.frappe.get_doc", side_effect=lambda dt, name=None: invoice if dt == "POS Invoice" else _ns(name=name)), \
			patch(f"{SO}.frappe.get_all", side_effect=get_all), patch(f"{SO}.frappe.db.get_value", side_effect=get_value) as gv, \
			patch(f"{SO}.frappe.db.exists", return_value=pending), patch(f"{SO}.frappe.db.set_value") as set_value, \
			patch("erpnext.accounts.doctype.payment_request.payment_request.make_payment_request", make, create=True):
			pr = so._open_payment_request("INV-1", channel)
		self.assertTrue(gv.call_args_list[0].kwargs.get("for_update"), "invoice locked first (H3)")
		return pr, set_value, make

	def test_terminal_refused_while_an_online_request_is_open(self):
		with self.assertRaises(frappe.ValidationError):
			self._open("terminal", open_prs=["PR-ONLINE"], terminal_prs=[])

	def test_online_refused_while_a_terminal_payment_is_pending(self):
		with self.assertRaises(frappe.ValidationError):
			self._open("online", pending=True)

	def test_new_request_cancels_older_open_ones(self):
		pr, set_value, make = self._open("online", open_prs=["PR-OLD"], terminal_prs=["PR-OLD"])
		self.assertEqual(pr.name, "PR-NEW")
		make.assert_called_once()
		set_value.assert_called_once_with("Payment Request", "PR-OLD", "status", "Cancelled")
		# online never reuses the terminal's request
		self.assertEqual(self.reuse_filters["name"], ["in", [""]])

	def test_reuse_own_channel_request_without_cancelling(self):
		pr, set_value, make = self._open("terminal", open_prs=["PR-T"], terminal_prs=["PR-T"], reuse="PR-T")
		self.assertEqual(pr.name, "PR-T")
		self.assertEqual(self.reuse_filters["name"], ["in", ["PR-T"]])
		make.assert_not_called()
		set_value.assert_not_called()


class TestPaidOnlineEndpoint(FrappeTestCase):
	def _call(self, invoices, exists=True, rows=()):
		with patch(f"{SO}.frappe.parse_json", side_effect=json.loads, create=True), \
			patch(f"{SO}.frappe.db.exists", return_value=exists), \
			patch(f"{SO}.frappe.has_permission", create=True) as perm, \
			patch(f"{SO}.frappe.db.get_value", return_value="BR-1"), \
			patch("ury.ury_pos.api.assert_user_branch") as branch, \
			patch(f"{SO}.frappe.get_all", return_value=[frappe._dict(r) for r in rows]) as get_all:
			return so.get_paid_online(invoices), perm, branch, get_all

	def test_each_invoice_read_and_branch_checked_and_summed(self):
		rows = [
			{"reference_name": "INV-1", "grand_total": 10.1, "currency": "LKR", "mode_of_payment": "Online"},
			{"reference_name": "INV-1", "grand_total": 5, "currency": "LKR", "mode_of_payment": "Online"},
		]
		out, perm, branch, get_all = self._call('["INV-1", "INV-2", "INV-1"]', rows=rows)
		self.assertEqual(out, {"INV-1": {"amount": 15.1, "currency": "LKR", "mode": "Online"}})
		self.assertEqual([c.kwargs["doc"] for c in perm.call_args_list], ["INV-1", "INV-2"])
		self.assertTrue(all(c.kwargs.get("throw") for c in perm.call_args_list))
		self.assertEqual(branch.call_count, 2)
		self.assertEqual(get_all.call_args.kwargs["filters"]["status"], "Paid")

	def test_bad_or_oversized_input_refused(self):
		for bad in ("[" + ",".join(f'"I{i}"' for i in range(51)) + "]", '"INV-1"', '[1]', '[""]'):
			with self.assertRaises(frappe.ValidationError):
				self._call(bad)

	def test_unknown_invoice_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._call('["NOPE"]', exists=False)


class TestSubmitGuard(FrappeTestCase):
	"""VEYQON: H1 -- a bill paid online cannot be submitted as cash."""

	def _guard(self, payments, paid, ury=True):
		from ury.ury.hooks import ury_pos_invoice as hook
		doc = frappe._dict(name="INV-1", restaurant="R1" if ury else None, restaurant_table=None, pos_profile=None,
			payments=[frappe._dict(p) for p in payments])
		with patch.object(hook.frappe.db, "get_values", create=True, return_value=[frappe._dict(r) for r in paid]) as gv:
			hook.guard_paid_online(doc)
		return gv

	def test_cash_only_after_online_payment_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._guard([{"mode_of_payment": "Cash", "amount": 250}], [{"name": "PR-1", "grand_total": 250, "mode_of_payment": "Online"}])

	def test_short_online_row_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._guard([{"mode_of_payment": "Online", "amount": 100}], [{"name": "PR-1", "grand_total": 250, "mode_of_payment": "Online"}])

	def test_online_row_covering_the_paid_amount_passes_with_a_locking_read(self):
		gv = self._guard(
			[{"mode_of_payment": "Online", "amount": 250}, {"mode_of_payment": "Cash", "amount": 40}],
			[{"name": "PR-1", "grand_total": 250, "mode_of_payment": "Online"}],
		)
		self.assertTrue(gv.call_args.kwargs.get("for_update"))

	def test_nothing_paid_online_passes(self):
		self._guard([{"mode_of_payment": "Cash", "amount": 250}], [])

	def test_non_ury_invoice_untouched(self):
		gv = self._guard([{"mode_of_payment": "Cash", "amount": 250}], [{"name": "PR-1", "grand_total": 250, "mode_of_payment": "Online"}], ury=False)
		gv.assert_not_called()

	def test_runs_on_submit(self):
		from ury.ury.hooks import ury_pos_invoice as hook
		self.assertIn("guard_paid_online(doc)", inspect.getsource(hook.before_submit))


class TestWebhook(FrappeTestCase):
	def _call(self, body, secret=SECRET, signature=None):
		raw = json.dumps(body).encode()
		if signature is None:
			signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
		conf = {so.PAYMENT_WEBHOOK_SECRET_KEY: secret} if secret else {}
		with patch(f"{SO}.frappe.conf", conf, create=True), \
			patch(f"{SO}.frappe.request", _ns(get_data=lambda: raw), create=True), \
			patch(f"{SO}.frappe.get_request_header", return_value=signature, create=True), \
			patch(f"{SO}.settle_payment_request", return_value="Paid") as settle, \
			patch(f"{SO}.fail_payment_request", return_value="Failed") as fail:
			return so.payment_webhook(), settle, fail

	def test_valid_paid_event_settles_with_event_values(self):
		result, settle, _ = self._call({"payment_request": "PR-1", "status": "Paid", "amount": 250, "currency": "INR", "reference": "r"})
		self.assertEqual(result, {"status": "Paid"})
		settle.assert_called_once_with("PR-1", 250, "INR", "r")

	def test_failed_event(self):
		result, settle, fail = self._call({"payment_request": "PR-1", "status": "Failed"})
		fail.assert_called_once_with("PR-1")
		settle.assert_not_called()

	def test_bad_or_missing_signature_refused(self):
		for kw in ({"signature": "00"}, {"signature": ""}, {"secret": None}):
			with self.assertRaises(frappe.PermissionError):
				self._call({"payment_request": "PR-1", "status": "Paid"}, **kw)

	def test_other_status_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._call({"payment_request": "PR-1", "status": "Approved"})


class TestOnlinePaymentGate(FrappeTestCase):
	def test_off_without_webhook_secret(self):
		profile = _ns(enable_customer_payment=1, enable_payment_link=0)
		with patch(f"{SO}.frappe.conf", {}, create=True):
			self.assertFalse(so.online_payment_ready(profile))
		with patch(f"{SO}.frappe.conf", {so.PAYMENT_WEBHOOK_SECRET_KEY: SECRET}, create=True):
			self.assertTrue(so.online_payment_ready(profile))

	def test_create_payment_request_refused_when_not_ready(self):
		profile = _ns(enable_customer_payment=1, enable_payment_link=0)
		with patch(f"{SO}._resolve_session", return_value=_ns(ordering_profile="P1", invoice="INV-1")), \
			patch(f"{SO}.frappe.get_doc", return_value=profile), patch(f"{SO}.frappe.conf", {}, create=True), \
			self.assertRaises(frappe.ValidationError):
			so.create_payment_request("tok")

	def test_no_second_request_after_paid(self):
		invoice = _ns(name="INV-1", docstatus=0, modified="x")
		with patch(f"{SO}.frappe.get_doc", return_value=invoice), \
			patch(f"{SO}.frappe.db.get_value", return_value="PR-PAID"), self.assertRaises(frappe.ValidationError):
			so._open_payment_request("INV-1")


class _Provider(pt.PaymentTerminalProvider):
	def __init__(self, start=None, status=None, cancel=None):
		self.start = MagicMock(return_value=start or {"transaction_id": "T1", "status": "Pending"})
		self.status = MagicMock(return_value=status or {"status": "Pending"})
		self.cancel = MagicMock(return_value=cancel or {"status": "Cancelled"})

	def start_transaction(self, invoice_name, amount, currency):
		return self.start(invoice_name, amount, currency)

	def get_transaction_status(self, transaction_id):
		return self.status(transaction_id)

	def cancel_transaction(self, transaction_id):
		return self.cancel(transaction_id)


def _txn(**kw):
	base = dict(name="TXN-1", invoice="INV-1", attempt=None, transaction_id="T1", payment_request="PR-1", amount=250.0, currency="INR", status="Pending")
	base.update(kw)
	return _ns(**base)


class TestTerminalScope(FrappeTestCase):
	def tearDown(self):
		pt._payment_terminal_provider = pt._NoOpPaymentTerminalProvider()

	def _scope(self, session, terminal="TERM-1", enabled=1):
		with patch(f"{SO}._throttle_session"), patch(f"{SO}._resolve_session", return_value=session), \
			patch(f"{PT}.frappe.get_doc", return_value=_ns(enable_customer_payment=enabled)), \
			patch(f"{PT}.frappe.db.get_value", return_value=terminal) as gv:
			return pt._session_terminal("tok", "x", 1, 1), gv

	def test_noop_provider_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._scope(_ns(ordering_profile="P1", device="DEV-1", invoice="INV-1"))

	def test_terminal_resolved_from_session_device(self):
		pt._payment_terminal_provider = _Provider()
		(_, _, terminal), gv = self._scope(_ns(ordering_profile="P1", device="DEV-1", invoice="INV-1"))
		self.assertEqual(terminal, "TERM-1")
		self.assertEqual(gv.call_args.args[1], {"device": "DEV-1", "status": ["!=", "Offline"]})

	def test_qr_session_without_device_refused(self):
		pt._payment_terminal_provider = _Provider()
		with self.assertRaises(frappe.ValidationError):
			self._scope(_ns(ordering_profile="P1", device=None, invoice="INV-1"))

	def test_device_without_terminal_refused(self):
		pt._payment_terminal_provider = _Provider()
		with self.assertRaises(frappe.ValidationError):
			self._scope(_ns(ordering_profile="P1", device="DEV-1", invoice="INV-1"), terminal=None)

	def test_profile_payment_off_refused(self):
		pt._payment_terminal_provider = _Provider()
		with self.assertRaises(frappe.ValidationError):
			self._scope(_ns(ordering_profile="P1", device="DEV-1", invoice="INV-1"), enabled=0)


class TestTerminalStateMachine(FrappeTestCase):
	def _advance(self, txn, result):
		with patch(f"{PT}.frappe.db.set_value") as set_value, patch(f"{SO}.settle_payment_request") as settle:
			status = pt._advance(so, txn, result)
		return status, set_value, settle

	def test_approved_matching_amount_settles(self):
		status, set_value, settle = self._advance(_txn(), {"status": "Approved", "amount": 250.0, "currency": "inr", "reference": "R"})
		self.assertEqual(status, "Approved")
		set_value.assert_called_once_with(pt.TXN, "TXN-1", "status", "Approved")
		settle.assert_called_once_with("PR-1", 250.0, "INR", "R")

	def test_approved_wrong_or_missing_amount_is_error(self):
		for result in ({"status": "Approved", "amount": 1, "currency": "INR"}, {"status": "Approved"}):
			status, set_value, settle = self._advance(_txn(), result)
			self.assertEqual(status, "Error")
			settle.assert_not_called()

	def test_final_states_never_change(self):
		for final in pt.FINAL_STATES:
			status, set_value, settle = self._advance(_txn(status=final), {"status": "Approved", "amount": 250.0, "currency": "INR"})
			self.assertEqual(status, final)
			set_value.assert_not_called()
			settle.assert_not_called()

	def test_unknown_or_pending_result_ignored(self):
		for result in ({"status": "Paid"}, {"status": "Pending"}, None, "Approved"):
			status, set_value, _ = self._advance(_txn(), result)
			self.assertEqual(status, "Pending")
			set_value.assert_not_called()

	def test_declined_does_not_settle(self):
		status, _, settle = self._advance(_txn(), {"status": "Declined"})
		self.assertEqual(status, "Declined")
		settle.assert_not_called()

	def test_settle_failure_keeps_approved(self):
		with patch(f"{PT}.frappe.db.set_value"), patch(f"{SO}.settle_payment_request", side_effect=frappe.ValidationError("closed")):
			self.assertEqual(pt._advance(so, _txn(), {"status": "Approved", "amount": 250.0, "currency": "INR"}), "Approved")


ATTEMPT = "a" * 16


class TestTerminalEndpoints(FrappeTestCase):
	def setUp(self):
		self.session = _ns(ordering_profile="P1", device="DEV-1", invoice="INV-1")
		p = patch(f"{PT}._session_terminal", return_value=(so, self.session, "TERM-1"))
		p.start()
		self.addCleanup(p.stop)

	def tearDown(self):
		pt._payment_terminal_provider = pt._NoOpPaymentTerminalProvider()

	def _start(self, provider, get_value):
		pt._payment_terminal_provider = provider
		txn = MagicMock(flags=_ns(), status="Pending", amount=250.0, currency="INR")
		txn.name = "TXN-1"
		order = MagicMock()
		provider.start.side_effect = provider.start.side_effect or (lambda *a: (order.provider(), {"transaction_id": "T1", "status": "Pending"})[1])
		with patch(f"{PT}.frappe.db.get_value", side_effect=get_value), \
			patch(f"{SO}._open_payment_request", return_value=_ns(name="PR-1", grand_total=250.0, currency="INR")) as open_pr, \
			patch(f"{PT}.frappe.new_doc", return_value=txn), patch(f"{PT}.frappe.db.set_value") as set_value, \
			patch(f"{PT}.frappe.log_error", create=True), \
			patch(f"{PT}.frappe.db.commit", create=True, side_effect=lambda: order.commit()):
			try:
				return pt.start_terminal_payment("tok", ATTEMPT), txn, set_value, order, open_pr
			except frappe.ValidationError as e:
				self.raised = e
				return None, txn, set_value, order, open_pr

	def test_start_records_pending_and_commits_before_the_provider(self):
		provider = _Provider()
		out, txn, set_value, order, open_pr = self._start(provider, ["TERM-1", None, _txn(attempt=ATTEMPT, transaction_id=None)])
		provider.start.assert_called_once_with("INV-1", 250.0, "INR")
		open_pr.assert_called_once_with("INV-1", "terminal")  # H2: refused there while online is open
		values = txn.update.call_args.args[0]
		self.assertEqual((values["amount"], values["currency"], values["payment_request"], values["status"]), (250.0, "INR", "PR-1", "Pending"))
		self.assertEqual((values["terminal"], values["attempt"]), ("TERM-1", ATTEMPT))
		self.assertTrue(values["transaction_id"].startswith("pending:"))  # unique placeholder, never NULL
		self.assertTrue(txn.flags.ury_terminal_write)
		txn.insert.assert_called_once_with(ignore_permissions=True)
		# M2: the row is committed before the terminal is asked to charge
		self.assertEqual([c[0] for c in order.mock_calls][:2], ["commit", "provider"])
		set_value.assert_any_call(pt.TXN, "TXN-1", "transaction_id", "T1")
		self.assertEqual((out["status"], out["attempt"]), ("Pending", ATTEMPT))

	def test_provider_failure_marks_the_row_error(self):
		for fail in (RuntimeError("timeout"), None):
			provider = _Provider()
			provider.start.side_effect = fail or (lambda *a: {})
			self.raised = None
			out, txn, set_value, order, _ = self._start(provider, ["TERM-1", None])
			self.assertIsInstance(self.raised, frappe.ValidationError)
			set_value.assert_called_once_with(pt.TXN, "TXN-1", "status", "Error")
			self.assertEqual([c[0] for c in order.mock_calls].count("commit"), 2)

	def test_bad_attempt_refused(self):
		pt._payment_terminal_provider = _Provider()
		for bad in ("short", None, "a" * 15, "x" * 16 + "!"):
			with self.assertRaises(frappe.ValidationError):
				pt.start_terminal_payment("tok", bad)
			with self.assertRaises(frappe.ValidationError):
				pt.get_terminal_payment_status("tok", bad)

	def test_busy_terminal_for_another_order_refused(self):
		provider = pt._payment_terminal_provider = _Provider()
		with patch(f"{PT}.frappe.db.get_value", side_effect=["TERM-1", _txn(invoice="INV-OTHER", attempt="b" * 16)]), \
			self.assertRaises(frappe.ValidationError):
			pt.start_terminal_payment("tok", ATTEMPT)
		provider.start.assert_not_called()

	def test_second_start_returns_the_pending_one_with_its_attempt(self):
		provider = pt._payment_terminal_provider = _Provider()
		with patch(f"{PT}.frappe.db.get_value", side_effect=["TERM-1", _txn(invoice="INV-1", attempt="b" * 16)]):
			out = pt.start_terminal_payment("tok", ATTEMPT)
		provider.start.assert_not_called()
		self.assertEqual(out, {"status": "Pending", "amount": 250.0, "currency": "INR", "attempt": "b" * 16})

	def test_status_reads_only_the_named_attempt_and_asks_the_provider(self):
		provider = pt._payment_terminal_provider = _Provider(status={"status": "Declined"})
		with patch(f"{PT}.frappe.db.get_value", return_value=_txn(attempt=ATTEMPT)) as gv, patch(f"{PT}.frappe.db.set_value"):
			out = pt.get_terminal_payment_status("tok", ATTEMPT)
		self.assertEqual(gv.call_args.args[1], {"terminal": "TERM-1", "invoice": "INV-1", "attempt": ATTEMPT})
		self.assertTrue(gv.call_args.kwargs.get("for_update"))
		provider.status.assert_called_once_with("T1")
		self.assertEqual(out["status"], "Declined")

	def test_status_before_the_provider_answered_does_not_ask_it(self):
		provider = pt._payment_terminal_provider = _Provider()
		with patch(f"{PT}.frappe.db.get_value", return_value=_txn(attempt=ATTEMPT, transaction_id=None)):
			self.assertEqual(pt.get_terminal_payment_status("tok", ATTEMPT)["status"], "Pending")
		provider.status.assert_not_called()

	def test_unknown_attempt_is_none(self):
		pt._payment_terminal_provider = _Provider()
		with patch(f"{PT}.frappe.db.get_value", return_value=None):
			self.assertEqual(pt.get_terminal_payment_status("tok", ATTEMPT), {"status": None})

	def test_cancel_records_what_the_provider_reports(self):
		provider = pt._payment_terminal_provider = _Provider(cancel={"status": "Approved", "amount": 250.0, "currency": "INR"})
		with patch(f"{PT}.frappe.db.get_value", return_value=_txn(attempt=ATTEMPT)), patch(f"{PT}.frappe.db.set_value"), \
			patch(f"{SO}.settle_payment_request") as settle:
			out = pt.cancel_terminal_payment("tok", ATTEMPT)
		self.assertEqual(out["status"], "Approved")
		provider.cancel.assert_called_once_with("T1")
		settle.assert_called_once()


class TestTransactionDoctype(FrappeTestCase):
	def test_desk_and_rest_writes_refused(self):
		doc = URYPaymentTerminalTransaction.__new__(URYPaymentTerminalTransaction)
		doc.flags = frappe._dict()
		with self.assertRaises(frappe.PermissionError):
			doc.validate()
		doc.flags.ury_terminal_write = True
		doc.validate()

	def test_manager_has_read_only(self):
		import os
		path = os.path.join(os.path.dirname(pt.__file__), "..", "doctype", "ury_payment_terminal_transaction", "ury_payment_terminal_transaction.json")
		perms = {p["role"]: p for p in json.load(open(path, encoding="utf-8"))["permissions"]}
		for key in ("create", "write", "delete"):
			self.assertFalse(perms["URY Manager"].get(key))

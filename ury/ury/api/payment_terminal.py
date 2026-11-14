# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
#
# Phase 7 — payment terminal integration interface.
#
# This is intentionally an interface only, not a working integration.
# Physical card-terminal integrations are vendor-specific (each provider —
# e.g. Pine Labs, Razorpay POS, PayTM terminal, Ezetap — ships its own SDK/
# protocol) and need real hardware plus vendor credentials to build and
# verify against; neither is available in this environment. Building a
# fake implementation against unverifiable assumptions would be worse than
# no implementation — it would look done without being done.
#
# What this module DOES provide: the abstraction boundary a real
# vendor adapter should implement, and the no-op default that lets the
# rest of the self-ordering payment flow (create_payment_request(),
# get_payment_status() in ury/ury/api/self_ordering.py) exist independently
# of terminal hardware. A kiosk transaction using a real terminal should
# look like:
#
#   Customer confirms order
#       -> kiosk calls create_payment_terminal_transaction(session)
#       -> registered PaymentTerminalProvider.start_transaction(...)
#       -> vendor SDK drives the physical terminal
#       -> vendor SDK confirms/declines
#       -> settlement follows the same Payment Request path
#          create_payment_request()/get_payment_status() already use
#          (see self_ordering.py's Phase 6 payment module) --
#          a terminal transaction should resolve to the SAME settlement
#          primitive as a gateway/link payment, not a separate one.
#
# A future vendor integration should call register_payment_terminal_provider()
# from its own app's hooks.py (matching the register_communication_provider()
# pattern already established in self_ordering.py) rather than editing this
# module directly.

import re

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

TERMINAL = "URY Payment Terminal"
TXN = "URY Payment Terminal Transaction"


class PaymentTerminalProvider:
	"""Abstract interface a real vendor adapter must implement. All methods
	receive customer-safe, already-authorized inputs (the caller has
	already resolved the ordering session/invoice before calling here) --
	an adapter should never need to re-derive branch/table/permission
	itself.
	"""

	def start_transaction(self, invoice_name, amount, currency):
		"""Initiate a transaction on the physical terminal for `amount`
		`currency` against `invoice_name`. Must return a dict with at
		least {"transaction_id": str, "status": str} — status one of
		"Pending", "Approved", "Declined", "Error". Implementations
		should be non-blocking where the vendor SDK allows it (poll via
		get_transaction_status rather than blocking the request thread
		for however long a customer takes to tap/swipe/insert a card).
		"""
		raise NotImplementedError

	def get_transaction_status(self, transaction_id):
		"""Return {"status": ..., "reference": ...} for a previously
		started transaction. Called by the kiosk while it waits for the
		customer to complete the physical tap/swipe/insert. VEYQON: G-13 --
		an "Approved" result must also carry the captured "amount" and
		"currency" as the terminal reports them; a mismatch is recorded as Error."""
		raise NotImplementedError

	def cancel_transaction(self, transaction_id):
		"""Best-effort cancel of a pending terminal transaction (e.g. the
		customer walked away). Implementations should treat this as
		advisory — a terminal may have already captured payment by the
		time cancellation is requested. VEYQON: G-13 -- return the status the
		terminal reports afterwards (Cancelled, or Approved with amount/currency
		if it had already captured), never "Cancelled" by assumption."""
		raise NotImplementedError


class _NoOpPaymentTerminalProvider(PaymentTerminalProvider):
	"""Default provider: makes the interface's existence visible in
	behavior (a clear, honest error) rather than silently pretending a
	terminal is available."""

	def start_transaction(self, invoice_name, amount, currency):
		frappe.throw(
			_("No payment terminal is configured for this kiosk. Please use a card/UPI reader at the counter, or pay via the online payment option if enabled."),
			frappe.ValidationError,
		)

	def get_transaction_status(self, transaction_id):
		frappe.throw(_("No payment terminal is configured for this kiosk."), frappe.ValidationError)

	def cancel_transaction(self, transaction_id):
		frappe.throw(_("No payment terminal is configured for this kiosk."), frappe.ValidationError)


class _SimulatedPaymentTerminalProvider(PaymentTerminalProvider):
	"""Stub provider for testing/demo purposes only. It does NOT talk to any
	physical hardware: a started transaction reports Approved for its own
	amount on the first status poll. Never registered by default -- opt in
	explicitly via register_simulated_terminal_provider() for testing/demo,
	never for production use. VEYQON: G-13 -- it no longer writes transaction
	rows itself; start_terminal_payment() records them."""

	def start_transaction(self, invoice_name, amount, currency):
		return {"transaction_id": frappe.generate_hash(length=12), "status": "Pending"}

	def get_transaction_status(self, transaction_id):
		txn = frappe.db.get_value(TXN, {"transaction_id": transaction_id}, ["amount", "currency"], as_dict=True)
		if not txn:
			frappe.throw(_("No such simulated terminal transaction."), frappe.ValidationError)
		return {"status": "Approved", "reference": transaction_id, "amount": txn.amount, "currency": txn.currency}

	def cancel_transaction(self, transaction_id):
		return {"status": "Cancelled"}


_payment_terminal_provider = _NoOpPaymentTerminalProvider()


# VEYQON: S9-18 -- not whitelisted: any signed-in user could switch kiosks to auto-approve.
# Call it from bench console or a test only.
def register_simulated_terminal_provider():
	"""Opt-in helper for testing/demo: installs _SimulatedPaymentTerminalProvider
	as the active provider. Never called automatically -- the default stays
	_NoOpPaymentTerminalProvider so production kiosks fail honestly until a
	real vendor adapter is registered."""
	register_payment_terminal_provider(_SimulatedPaymentTerminalProvider())


def register_payment_terminal_provider(provider):
	"""Install a real vendor adapter: an instance of a PaymentTerminalProvider
	subclass. Call this from a future installed app's hooks.py rather than
	editing this module directly — mirrors
	ury.ury.api.self_ordering.register_communication_provider()."""
	global _payment_terminal_provider
	if not isinstance(provider, PaymentTerminalProvider):
		frappe.throw(_("Payment terminal provider must implement PaymentTerminalProvider"))
	_payment_terminal_provider = provider


def get_payment_terminal_provider():
	return _payment_terminal_provider


# ---------------------------------------------------------------------------
# VEYQON: G-13 -- kiosk card payments. Guest endpoints, scoped by the ordering
# session: the terminal is the one linked to the session's device, the amount
# and currency come from the invoice's Payment Request (server-made), and the
# transaction state moves only Pending -> a final state the provider reports
# to the server. Approved settles through self_ordering.settle_payment_request
# (the same path as online payment, S9-25). The guest never sends a status.
# ---------------------------------------------------------------------------

FINAL_STATES = ("Approved", "Declined", "Cancelled", "Error")


def _terminal_for_device(device):
	if not device:
		return None
	return frappe.db.get_value(TERMINAL, {"device": device, "status": ["!=", "Offline"]}, "name")


def terminal_payment_available(device):
	return not isinstance(_payment_terminal_provider, _NoOpPaymentTerminalProvider) and bool(
		_terminal_for_device(device)
	)


def _session_terminal(session_token, action, limit, seconds):
	from ury.ury.api import self_ordering as so

	so._throttle_session(action, session_token, limit, seconds)
	session = so._resolve_session(session_token)
	profile = frappe.get_doc("URY Self Ordering Profile", session.ordering_profile)
	if not profile.enable_customer_payment:
		frappe.throw(_("Card payment is not enabled"), frappe.ValidationError)
	terminal = _terminal_for_device(session.device)
	if not terminal or isinstance(_payment_terminal_provider, _NoOpPaymentTerminalProvider):
		frappe.throw(_("No payment terminal is set up for this kiosk. Please pay at the counter."), frappe.ValidationError)
	if not session.invoice:
		frappe.throw(_("No active order for this session"), frappe.ValidationError)
	return so, session, terminal


PENDING_TXN_PREFIX = "pending:"


def _has_provider_id(txn):
	return bool(txn.transaction_id) and not str(txn.transaction_id).startswith(PENDING_TXN_PREFIX)


TXN_FIELDS = ["name", "invoice", "attempt", "transaction_id", "payment_request", "amount", "currency", "status"]


def _attempt(attempt):
	"""VEYQON: L -- the kiosk's own random key for one start; status and cancel name it, never "latest"."""
	if not isinstance(attempt, str) or not re.fullmatch(r"[A-Za-z0-9]{16,64}", attempt):
		frappe.throw(_("Invalid payment attempt"), frappe.ValidationError)
	return attempt


def _txn(terminal, invoice, attempt=None, name=None):
	"""Locked row: by name, or this session's attempt on this terminal (lock order: transaction, invoice, request)."""
	filters = name or {"terminal": terminal, "invoice": invoice, "attempt": attempt}
	return frappe.db.get_value(TXN, filters, TXN_FIELDS, as_dict=True, for_update=True)


def _advance(so, txn, result):
	"""The only status change: Pending -> a final state the provider reported. Returns the new status."""
	new = result.get("status") if isinstance(result, dict) else None
	if txn.status != "Pending" or new not in FINAL_STATES:
		return txn.status
	if new == "Approved" and (
		flt(result.get("amount"), 2) != flt(txn.amount, 2)
		or str(result.get("currency") or "").upper() != str(txn.currency or "").upper()
	):
		frappe.log_error(
			f"Terminal {txn.transaction_id}: provider approved {result.get('amount')} {result.get('currency')}, expected {txn.amount} {txn.currency}",
			"Self-Order Payment Mismatch",
		)
		new = "Error"
	frappe.db.set_value(TXN, txn.name, "status", new)
	if new == "Approved":
		try:
			so.settle_payment_request(txn.payment_request, txn.amount, txn.currency, result.get("reference") or txn.transaction_id)
		except Exception as e:
			# The card was charged: keep Approved so staff see it; the error log says why it did not settle.
			frappe.log_error(f"Terminal {txn.transaction_id} approved but not settled: {e}", "Self-Order Payment Mismatch")
	return new


def _out(txn, status=None):
	return {"status": status or txn.status, "amount": txn.amount, "currency": txn.currency, "attempt": txn.attempt}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def start_terminal_payment(session, attempt):
	so, session, terminal = _session_terminal(session, "terminal_start", 5, 600)
	attempt = _attempt(attempt)
	provider = get_payment_terminal_provider()
	with so._elevated():
		frappe.db.get_value(TERMINAL, terminal, "name", for_update=True)  # one start per terminal at a time
		busy = frappe.db.get_value(TXN, {"terminal": terminal, "status": "Pending"}, TXN_FIELDS, as_dict=True)
		if busy:
			if busy.invoice != session.invoice:
				frappe.throw(_("The payment terminal is busy. Please try again in a moment."), frappe.ValidationError)
			return _out(busy)  # the kiosk follows the returned attempt
		pr = so._open_payment_request(session.invoice, "terminal")
		txn = frappe.new_doc(TXN)
		txn.update({
			"terminal": terminal,
			"invoice": session.invoice,
			"payment_request": pr.name,
			"amount": pr.grand_total,
			"currency": pr.currency,
			"attempt": attempt,
			# VEYQON: unique placeholder until the provider answers (a NULL/"" may not be accepted twice by the unique index)
			"transaction_id": PENDING_TXN_PREFIX + frappe.generate_hash(length=20),
			"status": "Pending",
			"created_at": now_datetime(),
		})
		txn.flags.ury_terminal_write = True
		txn.insert(ignore_permissions=True)
		# VEYQON: M2 -- recorded, and the terminal / invoice locks released, before the terminal charges.
		frappe.db.commit()
		try:
			result = provider.start_transaction(session.invoice, pr.grand_total, pr.currency) or {}
		except Exception as e:
			frappe.log_error(f"Terminal start for {session.invoice} failed: {e}", "Self-Order Payment")
			result = {}
		transaction_id = str(result.get("transaction_id") or "") if isinstance(result, dict) else ""
		if not transaction_id:
			frappe.db.set_value(TXN, txn.name, "status", "Error")
			frappe.db.commit()
			frappe.throw(_("The payment terminal did not start the payment. Please pay at the counter."), frappe.ValidationError)
		row = _txn(terminal, session.invoice, name=txn.name)
		frappe.db.set_value(TXN, row.name, "transaction_id", transaction_id)
		row.transaction_id = transaction_id
		frappe.db.set_value(TERMINAL, terminal, {"last_transaction_id": transaction_id, "last_seen": now_datetime()})
		status = _advance(so, row, result)
		# The terminal is now charging a card: the row must survive a later error in this request.
		frappe.db.commit()
	return _out(row, status)


def _poll(session, attempt, action, limit, seconds, ask):
	so, session, terminal = _session_terminal(session, action, limit, seconds)
	attempt = _attempt(attempt)
	with so._elevated():
		txn = _txn(terminal, session.invoice, attempt)
		if not txn:
			return {"status": None}
		# no transaction id yet = the start request is still talking to the terminal
		status = _advance(so, txn, ask(txn.transaction_id)) if txn.status == "Pending" and _has_provider_id(txn) else txn.status
	return _out(txn, status)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def get_terminal_payment_status(session, attempt):
	return _poll(session, attempt, "terminal_status", 60, 60, lambda tid: get_payment_terminal_provider().get_transaction_status(tid))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def cancel_terminal_payment(session, attempt):
	return _poll(session, attempt, "terminal_cancel", 10, 600, lambda tid: get_payment_terminal_provider().cancel_transaction(tid))

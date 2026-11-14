import frappe
from frappe import _


def session_actor(actor=None):
	"""VEYQON: the acting user is always the session user (S10-16, K-3, K-4).

	Whitelisted services used to accept a client `actor` and authorize it, so
	`actor="Administrator"` skipped every role check. The parameter stays in
	the signatures so callers do not break: omitted or equal to the session
	user is accepted, anything else is refused. Server code (seeds, hooks)
	already passes `frappe.session.user`.
	"""
	user = frappe.session.user
	if actor and actor != user:
		frappe.throw(_("actor must be the logged-in user"), frappe.PermissionError)
	return user

"""Meta (WhatsApp Cloud API) webhook signature guard for frappe_whatsapp.

frappe_whatsapp's webhook has no X-Hub-Signature-256 check. guard_whatsapp_webhook
is a before_request hook that closes that gap for the webhook path only.
The app secret is read from site config key `ury_whatsapp_app_secret`.
"""

import hashlib
import hmac
import json

WEBHOOK_PATHS = frozenset(
	{
		"/api/method/frappe_whatsapp.utils.webhook.webhook",
		"/api/method/frappe_whatsapp.utils.webhook.webhook/",
	}
)
SECRET_CONFIG_KEY = "ury_whatsapp_app_secret"
CHAIRMAN_ACCOUNT_KEY = "ury_chairman_whatsapp_account"  # same key as chairman_whatsapp.ACCOUNT_CONF_KEY
PHONE_ID_CONFIG_KEY = "ury_whatsapp_phone_number_id"  # VEYQON: W-6 optional, extra Meta phone_number_id(s) URY owns
_PREFIX = "sha256="


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
	"""Pure check of `sha256=<hexdigest of HMAC-SHA256(app_secret, raw_body)>`."""
	if not app_secret or not header or not isinstance(header, str):
		return False
	if not header.startswith(_PREFIX):
		return False
	given = header[len(_PREFIX) :].strip().lower()
	if len(given) != 64:
		return False
	try:
		int(given, 16)
	except ValueError:
		return False
	if isinstance(raw_body, str):
		raw_body = raw_body.encode("utf-8")
	expected = hmac.new(app_secret.encode("utf-8"), raw_body or b"", hashlib.sha256).hexdigest()
	return hmac.compare_digest(expected.encode("ascii"), given.encode("ascii"))


def _as_list(value):
	if not value:
		return []
	return list(value) if isinstance(value, (list, tuple, set)) else [value]


def payload_phone_ids(raw_body):
	"""Meta `phone_number_id` of every change in a webhook body; None if it is not a Meta JSON object."""
	try:
		payload = json.loads(raw_body or b"{}")
		entries = payload.get("entry") or []
		ids = set()
		for entry in [entries] if isinstance(entries, dict) else entries:
			changes = entry.get("changes") or []
			for change in [changes] if isinstance(changes, dict) else changes:
				phone_id = ((change.get("value") or {}).get("metadata") or {}).get("phone_number_id")
				if phone_id:
					ids.add(str(phone_id))
		return ids
	except (ValueError, TypeError, AttributeError):
		return None


def _ury_phone_ids(frappe):
	"""phone_number_id of URY's WhatsApp Account(s) (chairman channel) plus any configured explicitly."""
	ids = {str(p) for p in _as_list(frappe.conf.get(PHONE_ID_CONFIG_KEY))}
	for account in _as_list(frappe.conf.get(CHAIRMAN_ACCOUNT_KEY)):
		phone_id = frappe.db.get_value("WhatsApp Account", account, "phone_id")
		if phone_id:
			ids.add(str(phone_id))
	return ids


def guard_whatsapp_webhook(*args, **kwargs):
	"""before_request hook. Raises frappe.PermissionError for bad POSTs to the webhook only."""
	import frappe

	request = getattr(frappe.local, "request", None)
	if request is None or request.path not in WEBHOOK_PATHS:
		return
	if request.method != "POST":
		return  # GET verify-token challenge untouched

	secret = frappe.conf.get(SECRET_CONFIG_KEY) or ""
	# VEYQON: W-6 -- enforce only when URY uses WhatsApp (secret set, or the chairman
	# channel is on). Otherwise other apps' frappe_whatsapp webhooks pass, as before URY.
	if not secret and not frappe.conf.get(CHAIRMAN_ACCOUNT_KEY):
		return
	raw = request.get_data(cache=True) or b""  # cached so form parsing still works
	# VEYQON: review fix W-6 -- enforce only for payloads addressed to URY's WhatsApp number(s).
	# Other apps' accounts pass unchanged. Unknown URY ids or a non-Meta body: still enforced.
	ury_ids = _ury_phone_ids(frappe)
	if ury_ids:
		ids = payload_phone_ids(raw)
		if ids is not None and ids and not (ids & ury_ids):
			return
	header = request.headers.get("X-Hub-Signature-256")
	if not verify_signature(raw, header, secret):
		frappe.throw("Invalid webhook signature", frappe.PermissionError)

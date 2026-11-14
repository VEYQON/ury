import hashlib
import hmac
import sys
import types
import unittest
from unittest import mock

from ury.ury.api import wa_signature as w

SECRET = "synthetic-secret"


def sign(body: bytes, secret=SECRET):
	return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestVerify(unittest.TestCase):
	def test_valid(self):
		b = b'{"entry":[]}'
		self.assertTrue(w.verify_signature(b, sign(b), SECRET))

	def test_unicode_body(self):
		b = '{"text":"مرحبا café ☕"}'.encode()
		self.assertTrue(w.verify_signature(b, sign(b), SECRET))

	def test_tampered_body(self):
		self.assertFalse(w.verify_signature(b'{"a":2}', sign(b'{"a":1}'), SECRET))

	def test_wrong_secret(self):
		b = b"x"
		self.assertFalse(w.verify_signature(b, sign(b, "other"), SECRET))

	def test_wrong_prefix(self):
		b = b"x"
		self.assertFalse(w.verify_signature(b, sign(b).replace("sha256=", "sha1="), SECRET))
		self.assertFalse(w.verify_signature(b, sign(b)[len("sha256="):], SECRET))

	def test_missing_or_malformed(self):
		for h in (None, "", "sha256=", "sha256=zz", "sha256=abc"):
			self.assertFalse(w.verify_signature(b"x", h, SECRET))

	def test_empty_secret(self):
		b = b"x"
		self.assertFalse(w.verify_signature(b, sign(b, ""), ""))


class _PermErr(Exception):
	pass


def _fake_frappe(path, method, body, header, secret, chairman_account=None, phone_ids=None):
	f = types.ModuleType("frappe")
	f.db = mock.Mock()
	f.db.get_value.side_effect = lambda dt, name, field: (phone_ids or {}).get(name)
	f.PermissionError = _PermErr
	def throw(msg, exc=Exception):
		raise exc(msg)
	f.throw = throw
	req = mock.Mock(path=path, method=method)
	req.get_data.return_value = body
	req.headers = {"X-Hub-Signature-256": header} if header is not None else {}
	f.local = types.SimpleNamespace(request=req)
	f.conf = {w.SECRET_CONFIG_KEY: secret} if secret is not None else {}
	if chairman_account:
		f.conf[w.CHAIRMAN_ACCOUNT_KEY] = chairman_account
	return f


class TestGuard(unittest.TestCase):
	P = "/api/method/frappe_whatsapp.utils.webhook.webhook"

	def run_guard(self, *a):
		with mock.patch.dict(sys.modules, {"frappe": _fake_frappe(*a)}):
			return w.guard_whatsapp_webhook()

	def test_valid_post_passes(self):
		b = b"{}"
		self.assertIsNone(self.run_guard(self.P, "POST", b, sign(b), SECRET))

	def test_bad_post_403(self):
		with self.assertRaises(_PermErr):
			self.run_guard(self.P, "POST", b"{}", "sha256=" + "0" * 64, SECRET)

	def test_missing_header_403(self):
		with self.assertRaises(_PermErr):
			self.run_guard(self.P, "POST", b"{}", None, SECRET)

	def test_no_secret_but_chairman_channel_on_403(self):
		with self.assertRaises(_PermErr):
			self.run_guard(self.P, "POST", b"{}", sign(b"{}"), None, "Owner WA")

	def test_no_secret_and_no_chairman_channel_passes(self):
		# VEYQON: W-6 -- URY does not use WhatsApp here; other apps' webhooks are untouched.
		self.assertIsNone(self.run_guard(self.P, "POST", b"{}", None, None))

	def test_other_apps_number_passes_unsigned(self):
		# VEYQON: review fix W-6 -- a webhook for another app's WhatsApp number is not URY's to check.
		b = b'{"entry":[{"changes":[{"value":{"metadata":{"phone_number_id":"999"}}}]}]}'
		self.assertIsNone(self.run_guard(self.P, "POST", b, None, SECRET, "Owner WA", {"Owner WA": "111"}))

	def test_urys_number_needs_signature(self):
		b = b'{"entry":[{"changes":[{"value":{"metadata":{"phone_number_id":"111"}}}]}]}'
		with self.assertRaises(_PermErr):
			self.run_guard(self.P, "POST", b, None, SECRET, "Owner WA", {"Owner WA": "111"})
		self.assertIsNone(self.run_guard(self.P, "POST", b, sign(b), SECRET, "Owner WA", {"Owner WA": "111"}))

	def test_mixed_or_unparseable_payload_still_checked(self):
		for b in (b'{"entry":[{"changes":[{"value":{"metadata":{"phone_number_id":"999"}}},'
				  b'{"value":{"metadata":{"phone_number_id":"111"}}}]}]}', b"not json", b'{"entry":"x"}'):
			with self.assertRaises(_PermErr, msg=b):
				self.run_guard(self.P, "POST", b, None, SECRET, "Owner WA", {"Owner WA": "111"})

	def test_payload_phone_ids(self):
		self.assertEqual(w.payload_phone_ids(b'{"entry":{"changes":{"value":{"metadata":{"phone_number_id":5}}}}}'), {"5"})
		self.assertEqual(w.payload_phone_ids(b"{}"), set())
		self.assertIsNone(w.payload_phone_ids(b"[1]"))

	def test_get_untouched(self):
		self.assertIsNone(self.run_guard(self.P, "GET", b"", None, None))

	def test_other_path_untouched(self):
		self.assertIsNone(self.run_guard("/api/method/ping", "POST", b"{}", None, None))


if __name__ == "__main__":
	unittest.main()

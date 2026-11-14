# VEYQON self-order / integrations safety tests (S9-16/17/18/20/23/24, G-1, G-3, W-1, W-9, GF-1).
# Mock style, like test_veyqon_shared_tenant.py. W-6 is covered in ury/ury/api/test_wa_signature.py.

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.ury.api import payment_terminal, self_ordering, ury_ai_settings
from ury.ury.api import ury_department_profitability as dept
from ury.ury.hooks import checklist

SO = "ury.ury.api.self_ordering"
GUEST_FNS = (
	"get_ordering_context", "assign_device_table", "get_customer_menu", "get_customer_product",
	"get_customer_order", "add_customer_items", "request_bill", "get_order_status",
	"create_payment_request", "get_payment_status", "share_payment_link",
)


def _decorators(fn):
	src = inspect.getsource(inspect.unwrap(fn)).splitlines()
	return [line.split("  #")[0].strip() for line in src if line.strip().startswith("@")]


class _FakeCache:
	def __init__(self):
		self.store = {}

	def make_key(self, key):
		return key

	def incrby(self, key, amount):
		self.store[key] = self.store.get(key, 0) + amount
		return self.store[key]

	def expire(self, key, seconds):
		pass


class _Throttled(FrappeTestCase):
	"""Runs each test as an HTTP request with an in-memory Redis."""

	def setUp(self):
		self.cache = _FakeCache()
		for p in (
			patch(f"{SO}.frappe.cache", self.cache, create=True),
			patch.object(frappe.local, "request", object(), create=True),
			patch.object(frappe.local, "request_ip", "10.0.0.1", create=True),
		):
			p.start()
			self.addCleanup(p.stop)


class TestGuestEndpointsPostOnly(FrappeTestCase):
	def test_every_guest_function_is_post_only(self):
		self.assertEqual(self_ordering._GUEST, {"allow_guest": True, "methods": ["POST"]})
		for name in GUEST_FNS:
			self.assertEqual(_decorators(getattr(self_ordering, name)), ["@frappe.whitelist(**_GUEST)"], name)

	def test_enroll_and_rotate_are_post_only(self):
		for fn in (self_ordering.enroll_device, self_ordering.rotate_qr_signing_secret):
			self.assertEqual(_decorators(fn), ['@frappe.whitelist(methods=["POST"])'])

	def test_simulated_terminal_provider_not_whitelisted(self):
		self.assertEqual(_decorators(payment_terminal.register_simulated_terminal_provider), [])
		self.assertNotIn(payment_terminal.register_simulated_terminal_provider, getattr(frappe, "whitelisted", ()))


class TestSecretsArePasswords(FrappeTestCase):
	def test_signing_secret_read_decrypted(self):
		with patch(f"{SO}.get_decrypted_password", return_value="s3cret") as dec:
			self.assertEqual(self_ordering._get_profile_secret("P1"), "s3cret")
		dec.assert_called_once_with("URY Self Ordering Profile", "P1", "qr_signing_secret", raise_exception=False)

	def test_missing_secret_fails_closed(self):
		with patch(f"{SO}.get_decrypted_password", return_value=None), self.assertRaises(frappe.ValidationError):
			self_ordering._get_profile_secret("P1")

	def test_rotate_needs_write_permission(self):
		doc = MagicMock()
		doc.check_permission.side_effect = frappe.PermissionError
		with patch(f"{SO}.frappe.get_doc", return_value=doc), self.assertRaises(frappe.PermissionError):
			self_ordering.rotate_qr_signing_secret("P1")
		doc.save.assert_not_called()

	def test_rotate_sets_new_secret(self):
		doc = MagicMock(qr_signing_secret="old")
		with patch(f"{SO}.frappe.get_doc", return_value=doc), patch(f"{SO}.frappe.generate_hash", return_value="new", create=True):
			self_ordering.rotate_qr_signing_secret("P1")
		self.assertEqual(doc.qr_signing_secret, "new")
		doc.save.assert_called_once_with()


class TestStaffPin(_Throttled):
	def _assign(self, pin):
		profile = MagicMock(branch="B1")
		profile.name = "P1"
		with patch(f"{SO}._resolve_device", return_value=(profile, None, "Kiosk", "DEV-1", "Tablet")), \
			patch(f"{SO}.frappe.db.get_value", return_value="Selectable"), \
			patch(f"{SO}.get_decrypted_password", return_value="1234"):
			return self_ordering.assign_device_table("dev-1", "cred", pin, "T1")

	def test_wrong_pin_rejected_then_rate_limited(self):
		for _i in range(5):
			with self.assertRaises(frappe.PermissionError):
				self._assign("0000")
		with self.assertRaises(frappe.TooManyRequestsError):
			self._assign("1234")  # even the right PIN, once the window is used up


class TestResolveDeviceBranch(FrappeTestCase):
	def _resolve(self, device, table_branch="B1"):
		profile = MagicMock(branch="B1", enabled=1, enable_kiosk_ordering=1)

		def get_value(dt, *a, **k):
			return "DEV-1" if dt == "URY Ordering Device" else table_branch

		def get_doc(dt, name):
			return device if dt == "URY Ordering Device" else profile

		with patch(f"{SO}.frappe.db.get_value", side_effect=get_value), patch(f"{SO}.frappe.get_doc", side_effect=get_doc), \
			patch(f"{SO}.frappe.set_user", create=True), patch(f"{SO}.frappe.session", SimpleNamespace(user="Guest"), create=True), \
			patch(f"{SO}.now_datetime", return_value="now"):
			return self_ordering._resolve_device("dev-1", "cred")

	def _device(self, **kw):
		d = MagicMock(branch=None, table_mode="Fixed", fixed_table="T1", device_type="Table Tablet", layout="Tablet", ordering_profile="P1")
		d.name = "DEV-1"
		for k, v in kw.items():
			setattr(d, k, v)
		return d

	def test_fixed_table_in_profile_branch_resolves(self):
		self.assertEqual(self._resolve(self._device())[1], "T1")

	def test_fixed_table_in_other_branch_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._resolve(self._device(), table_branch="B2")

	def test_fixed_mode_without_table_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._resolve(self._device(fixed_table=None))

	def test_device_branch_mismatch_rejected(self):
		with self.assertRaises(frappe.PermissionError):
			self._resolve(self._device(branch="B2"))


class TestAddItemsLimits(_Throttled):
	def _add(self, items, session="tok"):
		sess = MagicMock(invoice=None, table=None, ordering_profile="P1")
		profile = MagicMock(enabled=1, allow_add_to_running_table=1, branch="B1")
		menu = {"items": [{"item": "TEA"}]}
		with patch(f"{SO}._resolve_session", return_value=sess), patch(f"{SO}.frappe.get_doc", return_value=profile), \
			patch(f"{SO}.resolve_restaurant_menu", return_value=menu), patch(f"{SO}.frappe.set_user", create=True), \
			patch(f"{SO}.frappe.session", SimpleNamespace(user="Guest"), create=True), \
			patch(f"{SO}._ensure_admin_branch_mapping", side_effect=RuntimeError("reached invoice")):
			return self_ordering.add_customer_items(session, items)

	def test_bad_quantities_rejected(self):
		for qty in (0.5, 1.5, 0, -1, 21, 1e9, "x"):
			with self.assertRaises(frappe.ValidationError, msg=qty):
				self._add([{"item": "TEA", "qty": qty}])

	def test_whole_quantity_passes_validation(self):
		with self.assertRaisesRegex(RuntimeError, "reached invoice"):
			self._add([{"item": "TEA", "qty": "2"}])

	def test_lines_per_session_capped(self):
		batch = [{"item": "TEA", "qty": 1}] * 20
		for _i in range(2):
			with self.assertRaisesRegex(RuntimeError, "reached invoice"):
				self._add(batch)
		with self.assertRaises(frappe.TooManyRequestsError):
			self._add([{"item": "TEA", "qty": 1}])

	def test_other_session_has_its_own_budget(self):
		for _i in range(2):
			with self.assertRaisesRegex(RuntimeError, "reached invoice"):
				self._add([{"item": "TEA", "qty": 1}] * 20)
		with self.assertRaisesRegex(RuntimeError, "reached invoice"):
			self._add([{"item": "TEA", "qty": 1}], session="other")

	def test_no_throttle_outside_http_request(self):
		with patch.object(frappe.local, "request", None):
			for _i in range(15):
				with self.assertRaisesRegex(RuntimeError, "reached invoice"):
					self._add([{"item": "TEA", "qty": 1}])


class TestReadRateLimit(_Throttled):
	def test_menu_reads_limited_per_session(self):
		with patch(f"{SO}._resolve_session", side_effect=RuntimeError("ok")):
			for _i in range(60):
				with self.assertRaisesRegex(RuntimeError, "ok"):
					self_ordering.get_customer_menu("tok")
			with self.assertRaises(frappe.TooManyRequestsError):
				self_ordering.get_customer_menu("tok")


class TestPaymentRequestReuse(_Throttled):
	def test_open_request_reused(self):
		sess = MagicMock(invoice="INV-1", ordering_profile="P1")
		profile = MagicMock(enable_customer_payment=1)
		invoice = MagicMock(docstatus=0, modified="2026-11-14 10:00")
		invoice.name = "INV-1"
		pr = MagicMock(grand_total=10, currency="LKR", status="Requested")
		pr.name = "PR-1"
		pr.get_payment_url.return_value = "https://pay"
		docs = {"URY Self Ordering Profile": profile, "POS Invoice": invoice, "Payment Request": pr}
		make = MagicMock()
		with patch(f"{SO}._resolve_session", return_value=sess), patch(f"{SO}.frappe.get_doc", side_effect=lambda dt, n=None: docs[dt]), \
			patch(f"{SO}.frappe.db.get_value", return_value="PR-1") as gv, patch(f"{SO}.frappe.set_user", create=True), \
			patch(f"{SO}.frappe.session", SimpleNamespace(user="Guest"), create=True), \
			patch("erpnext.accounts.doctype.payment_request.payment_request.make_payment_request", make):
			result = self_ordering.create_payment_request("tok")
		make.assert_not_called()
		self.assertEqual(result["payment_request"], "PR-1")
		self.assertEqual(gv.call_args[0][1]["creation"], [">=", "2026-11-14 10:00"])

	def test_share_link_rejects_non_contact_recipient(self):
		sess = MagicMock(invoice="INV-1", ordering_profile="P1")
		with patch(f"{SO}._resolve_session", return_value=sess), patch(f"{SO}.frappe.get_doc", return_value=MagicMock(enable_payment_link=1)):
			for bad in ("", "x", "<script>", "a@b", None):
				with self.assertRaises(frappe.ValidationError, msg=bad):
					self_ordering.share_payment_link("tok", bad)


class TestProviderKeyPermission(FrappeTestCase):
	AI = "ury.ury.api.ury_ai_settings"

	def test_key_write_needs_provider_write_permission(self):
		doc = MagicMock()
		doc.check_permission.side_effect = frappe.PermissionError
		with patch(f"{self.AI}.require_manager"), patch(f"{self.AI}._huf_installed", return_value=True), \
			patch(f"{self.AI}.frappe.db.exists", return_value=True, create=True), patch(f"{self.AI}.frappe.get_doc", return_value=doc), \
			self.assertRaises(frappe.PermissionError):
			ury_ai_settings.set_provider_api_key("OpenAI", "sk-x")
		doc.save.assert_not_called()

	def test_key_saved_with_permissions_checked(self):
		doc = MagicMock()
		with patch(f"{self.AI}.require_manager"), patch(f"{self.AI}._huf_installed", return_value=True), \
			patch(f"{self.AI}.frappe.db.exists", return_value=True, create=True), patch(f"{self.AI}.frappe.get_doc", return_value=doc):
			ury_ai_settings.set_provider_api_key("OpenAI", " sk-x ")
		doc.check_permission.assert_called_once_with("write")
		doc.save.assert_called_once_with()
		self.assertEqual(doc.api_key, "sk-x")


class TestQualityReviewScope(FrappeTestCase):
	CL = "ury.ury.hooks.checklist"

	def test_condition_only_limits_ury_reviews(self):
		with patch(f"{self.CL}.frappe.db.sql", return_value=[{"permit_to_view": 0}], create=True), \
			patch(f"{self.CL}.frappe.db.escape", side_effect=lambda v: f"'{v}'", create=True):
			cond = checklist.permission_checklists("u@x.test")
		self.assertIn("ifnull(`tabQuality Review`.branch, '') = ''", cond)
		self.assertIn("owner = 'u@x.test'", cond)

	def test_permitted_employee_unrestricted(self):
		with patch(f"{self.CL}.frappe.db.sql", return_value=[{"permit_to_view": 1}], create=True):
			self.assertIsNone(checklist.permission_checklists("u@x.test"))

	def test_validate_only_for_branch_reviews(self):
		checklist.validate_checklist_review(frappe._dict(branch=None, employee=None))  # other apps: fine
		checklist.validate_checklist_review(frappe._dict(branch="B1", employee="u@x.test"))
		with self.assertRaises(frappe.MandatoryError):
			checklist.validate_checklist_review(frappe._dict(branch="B1", employee=None))


class TestDepartmentProfitabilityAccess(FrappeTestCase):
	DP = "ury.ury.api.ury_department_profitability"

	def _access(self, roles, user="u@x.test"):
		with patch(f"{self.DP}.frappe.get_roles", return_value=roles), \
			patch(f"{self.DP}.frappe.session", SimpleNamespace(user=user), create=True):
			return dept._resolve_access("C1", "B1", None)

	def test_unshipped_generic_roles_denied(self):
		for role in ("Finance", "Finance Manager", "URY Finance", "Inventory Manager"):
			with self.assertRaises(frappe.PermissionError, msg=role):
				self._access([role])

	def test_no_access_role_wins_over_granting_role(self):
		for extra in ("Chef", "URY Chef", "Production"):
			with self.assertRaises(frappe.PermissionError, msg=extra):
				self._access(["URY Cashier", extra])

	def test_system_manager_and_administrator_full(self):
		self.assertEqual(self._access(["System Manager"])[0], "full")
		self.assertEqual(self._access(["URY Cashier"], user="Administrator")[0], "full")

	def test_chef_alone_still_quantity_only(self):
		self.assertEqual(self._access(["Chef"])[:2], ("quantity_only", True))

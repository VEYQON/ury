# VEYQON shared-tenant patch tests: the one-draft-per-branch rule must not apply
# to Stock Reconciliations without a branch (other apps on the same site).

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.ury.utils.stock_reconciliation_guards import validate

MOD = "ury.ury.utils.stock_reconciliation_guards"


class TestStockReconciliationGuard(FrappeTestCase):
	def test_branchless_draft_is_not_checked(self):
		doc = frappe._dict(name="MAT-RECO-2", branch=None)
		with patch(f"{MOD}.frappe.db.exists", return_value="MAT-RECO-1") as exists:
			validate(doc, "validate")
		exists.assert_not_called()

	def test_second_draft_for_same_branch_is_refused(self):
		doc = frappe._dict(name="MAT-RECO-2", branch="Downtown")
		with patch(f"{MOD}.frappe.db.exists", return_value="MAT-RECO-1"):
			with self.assertRaises(frappe.ValidationError):
				validate(doc, "validate")

	def test_same_document_resaved_is_allowed(self):
		doc = frappe._dict(name="MAT-RECO-1", branch="Downtown")
		with patch(f"{MOD}.frappe.db.exists", return_value="MAT-RECO-1"):
			validate(doc, "validate")

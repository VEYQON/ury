# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class URYPaymentTerminalTransaction(Document):
	# VEYQON: G-13 / S9-19 -- rows are written only by ury.ury.api.payment_terminal, which sets the
	# flag; status moves there with db.set_value. Desk/REST cannot create or edit one ("Approved" forgery).
	def validate(self):
		if not self.flags.ury_terminal_write:
			frappe.throw(_("Terminal transactions are recorded by the payment terminal integration only"), frappe.PermissionError)

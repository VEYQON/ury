# VEYQON: S9-16 / S9-17 -- qr_signing_secret and staff_pin moved from Data to Password.
# The schema sync keeps the old plaintext in the column; move it into __Auth (encrypted)
# and leave only asterisks behind, as Frappe does when it saves a Password field.
# Existing QR codes keep working: the secret value is unchanged.

import frappe
from frappe.utils.password import set_encrypted_password

DOCTYPE = "URY Self Ordering Profile"
FIELDS = ("qr_signing_secret", "staff_pin")


def execute():
	# raw SQL: the query builder may refuse to read Password columns
	for row in frappe.db.sql(
		"select name, qr_signing_secret, staff_pin from `tabURY Self Ordering Profile`", as_dict=True
	):
		for field in FIELDS:
			value = row.get(field)
			if not value or set(value) == {"*"}:
				continue  # empty, or already moved
			set_encrypted_password(DOCTYPE, row.name, value, field)
			frappe.db.set_value(DOCTYPE, row.name, field, "*" * len(value), update_modified=False)

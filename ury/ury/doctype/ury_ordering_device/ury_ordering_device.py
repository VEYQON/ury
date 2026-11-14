# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class URYOrderingDevice(Document):
    # Credential hashing/enrollment logic is handled by the API layer in a
    # later phase, not in this doctype controller.

    def validate(self):
        # VEYQON: S9-20 -- a device, its profile and its fixed table share one branch.
        profile_branch = frappe.db.get_value("URY Self Ordering Profile", self.ordering_profile, "branch")
        if self.branch and self.branch != profile_branch:
            frappe.throw(_("Branch must match the ordering profile's branch ({0})").format(profile_branch))
        self.branch = profile_branch
        if self.table_mode == "Fixed":
            if not self.fixed_table:
                frappe.throw(_("Fixed Table is required when Table Mode is Fixed"))
            if frappe.db.get_value("URY Table", self.fixed_table, "branch") != profile_branch:
                frappe.throw(_("Table {0} is not in branch {1}").format(self.fixed_table, profile_branch))

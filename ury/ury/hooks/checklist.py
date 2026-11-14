import frappe
from frappe import _


def permission_checklists(user):
    user = user or frappe.session.user  # VEYQON: W-9 -- honour the user Frappe asks about
    employee = frappe.db.sql(
        """ SELECT
				`tabEmployee`.user_id ,
				`tabEmployee`.branch ,
				`tabEmployee`.permit_to_view
			FROM
				`tabEmployee`
			WHERE
				`tabEmployee`.user_id = %s
		""",
        (user),
        as_dict=True,
    )
    for i in employee:
        if i["permit_to_view"] == 0:
            # VEYQON: W-9 -- limit only URY checklist reviews (they carry URY's `branch`
            # field); Quality Reviews of other apps stay visible as before URY.
            return "(ifnull(`tabQuality Review`.branch, '') = '' or `tabQuality Review`.owner = {user})".format(
                user=frappe.db.escape(user)
            )


def validate_checklist_review(doc, method=None):
    """VEYQON: W-9 -- `branch` / `employee` are no longer reqd site-wide; a URY
    checklist review (one with a branch) still needs its employee."""
    if doc.get("branch") and not doc.get("employee"):
        frappe.throw(_("Employee is required for a restaurant checklist review"), frappe.MandatoryError)

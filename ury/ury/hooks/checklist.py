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


# VEYQON: review fix W-9 -- the Quality Goals URY ships (fixtures/quality_goal.json).
# shortcut: fixed names plus URY's `custom_role` field; add a URY flag on Quality Goal if restaurants
# build checklists from goals with neither.
URY_CHECKLIST_GOALS = frozenset({
    "Cashier Opening Checklist", "Cashier Closing Checklist", "Order Taker Opening Checklist",
    "Order Taker Closing Checklist", "RM Opening Checklist", "Restaurant Manager Closing Checklist",
})


def is_ury_checklist_review(doc):
    goal = doc.get("goal")
    return bool(goal) and (goal in URY_CHECKLIST_GOALS or bool(frappe.db.get_value("Quality Goal", goal, "custom_role")))


def validate_checklist_review(doc, method=None):
    """VEYQON: W-9 -- `branch` / `employee` are no longer reqd site-wide. A URY
    checklist review needs both, so it cannot leave branch empty to dodge the
    visibility filter; any review with a branch still needs its employee."""
    if is_ury_checklist_review(doc) and not doc.get("branch"):
        frappe.throw(_("Branch is required for a restaurant checklist review"), frappe.MandatoryError)
    if doc.get("branch") and not doc.get("employee"):
        frappe.throw(_("Employee is required for a restaurant checklist review"), frappe.MandatoryError)

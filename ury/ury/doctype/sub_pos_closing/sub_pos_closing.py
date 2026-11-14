
import frappe
from frappe import _
from frappe.utils import flt, get_datetime
from datetime import datetime, timedelta
from frappe.model.document import Document
import json
import requests
from datetime import datetime
from ury.ury_pos.api import getBranch
from frappe.utils import  get_datetime,now


class SubPOSClosing(Document):
    def validate(self):
        owner = None
        opening = self._validate_owner_and_opening()  # VEYQON: SC-1
        branch = frappe.db.get_value("POS Profile", self.pos_profile, "branch")

        draft_invoices = frappe.get_all(
            "POS Invoice",
            fields=["name"],
            filters={"branch": branch, "status": "Draft", "docstatus": "0","cashier":self.user},
        )
        if draft_invoices:
            frappe.throw("Submit/Delete Draft Invoices")

        date_time = now()
        if isinstance(date_time, str):
            formatted_date_time = date_time.split('.')[0]
        else:
            formatted_date_time = date_time.strftime('%Y-%m-%d %H:%M:%S')
        self.period_end_date = date_time

        time_part = formatted_date_time.split(' ')[1]
        self.posting_time = time_part

        # VEYQON: SC-1 — the same invoice set the form loads (get_pos_invoices), and
        # totals / expected / difference recomputed here instead of trusted from the client.
        self._set_totals(opening, _closing_invoices(self.period_start_date, self.period_end_date, self.pos_profile, self.user))

        multiple_cashier = frappe.db.get_value("POS Profile", self.pos_profile, "custom_enable_multiple_cashier")
        if multiple_cashier:
            get_cashier = frappe.get_doc("POS Profile", self.pos_profile)
            for user_details in get_cashier.applicable_for_users:
                if user_details.custom_main_cashier:
                    owner = user_details.user
            if frappe.session.user == owner:
                frappe.throw("The Main Cashier cannot close a Sub POS Closing entry.")
        else:
            pass

    def _validate_owner_and_opening(self):
        # VEYQON: SC-1 — a cashier closes only their own open session.
        session_user = frappe.session.user
        is_supervisor = session_user == "Administrator" or bool(set(frappe.get_roles(session_user)) & SUPERVISOR_ROLES)
        if self.user != session_user and not is_supervisor:
            frappe.throw(_("You can only close your own POS session."), frappe.PermissionError)
        if not self.pos_opening_entry:
            frappe.throw(_("POS Opening Entry is required."))
        opening = frappe.get_doc("POS Opening Entry", self.pos_opening_entry)
        if opening.docstatus != 1 or opening.status != "Open" or opening.user != self.user or opening.pos_profile != self.pos_profile:
            frappe.throw(
                _("POS Opening Entry {0} is not an open session of {1} on {2}.").format(self.pos_opening_entry, self.user, self.pos_profile),
                frappe.ValidationError,
            )
        self.period_start_date = opening.period_start_date
        return opening

    def _set_totals(self, opening, invoices):
        opening_amounts = {}
        for row in opening.get("balance_details") or []:
            opening_amounts[row.mode_of_payment] = opening_amounts.get(row.mode_of_payment, 0) + flt(row.opening_amount)

        sales = {}
        self.set("pos_transactions", [])
        self.grand_total = self.net_total = self.total_quantity = 0
        for inv in invoices:
            self.append("pos_transactions", {
                "pos_invoice": inv.name,
                "posting_date": inv.posting_date,
                "customer": inv.customer,
                "grand_total": inv.grand_total,
                "base_grand_total": inv.base_grand_total,
            })
            self.grand_total += flt(inv.grand_total)
            self.net_total += flt(inv.net_total)
            self.total_quantity += flt(inv.total_qty)
            for p in inv.get("payments") or []:
                # change is given back from the change account's mode, as the form does
                change = flt(inv.change_amount) if p.account == inv.account_for_change_amount else 0
                sales[p.mode_of_payment] = sales.get(p.mode_of_payment, 0) + flt(p.amount) - change

        have = {r.mode_of_payment for r in self.get("payment_reconciliation") or []}
        for mode in list(opening_amounts) + list(sales):
            if mode not in have:
                self.append("payment_reconciliation", {"mode_of_payment": mode, "closing_amount": 0})
                have.add(mode)
        for r in self.payment_reconciliation:
            r.opening_amount = opening_amounts.get(r.mode_of_payment, 0)
            r.expected_amount = r.opening_amount + sales.get(r.mode_of_payment, 0)
            r.difference = flt(r.closing_amount) - r.expected_amount

    def on_submit(self):
        opening_entry = frappe.get_doc("POS Opening Entry", self.pos_opening_entry)
        opening_entry.custom_sub_pos_close_entry = self.name
        opening_entry.status = "Closed"
        opening_entry.save()
    
    def on_cancel(self):
        opening_entry = frappe.get_doc("POS Opening Entry", self.pos_opening_entry)
        opening_entry.custom_sub_pos_close_entry = self.name
        opening_entry.status = "Open"
        opening_entry.save()


@frappe.whitelist()
def get_pos_profile():
    branch = getBranch()
    pos_profile = frappe.db.get_value("POS Profile", {"branch": branch}, "name")
    return pos_profile


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_cashiers(doctype, txt, searchfield, start, page_len, filters):
    cashiers_list = frappe.get_all(
        "POS Profile User", filters=filters, fields=["user"], as_list=1
    )
    return [c for c in cashiers_list]


SUPERVISOR_ROLES = {"URY Manager", "System Manager"}


@frappe.whitelist()
def get_pos_invoices(start, end, pos_profile, user):
    frappe.has_permission("POS Invoice", "read", throw=True)

    session_user = frappe.session.user
    is_supervisor = session_user == "Administrator" or bool(
        set(frappe.get_roles(session_user)) & SUPERVISOR_ROLES
    )

    # Non-supervisors may only query their own invoices
    if not is_supervisor:
        user = session_user

    # Branch scoping: the POS Profile must belong to the session user's branch
    try:
        session_branch = getBranch()
    except Exception:
        if is_supervisor:
            # Supervisors may not be mapped to a branch
            session_branch = None
        else:
            raise

    profile_branch = frappe.db.get_value(
        "POS Profile", pos_profile, "branch"
    )

    if not profile_branch:
        frappe.throw(
            _("POS Profile {0} not found.").format(pos_profile),
            frappe.DoesNotExistError,
        )

    if session_branch and profile_branch != session_branch:
        frappe.throw(
            _("You do not have permission to access invoices for POS Profile {0}.").format(
                pos_profile
            ),
            frappe.PermissionError,
        )

    return [d.as_dict() for d in _closing_invoices(start, end, pos_profile, user)]


def _closing_invoices(start, end, pos_profile, user):
    """Submitted, unconsolidated POS Invoices of `user` on `pos_profile` in [start, end] (no permission check)."""
    # VEYQON: SC-1 — split out of get_pos_invoices so validate() recomputes from the same set.
    data = frappe.db.sql(
        """
        select
            name, timestamp(posting_date, posting_time) as "timestamp"
        from
            `tabPOS Invoice`
        where
            cashier = %s and docstatus = 1 and pos_profile = %s and ifnull(consolidated_invoice,'') = '' and status != "Consolidated"
        """,
        (user, pos_profile),
        as_dict=1,
    )

    data = list(
        filter(
            lambda d: get_datetime(start)
            <= get_datetime(d.timestamp)
            <= get_datetime(end),
            data,
        )
    )
    # need to get taxes and payments so can't avoid get_doc
    return [frappe.get_doc("POS Invoice", d.name) for d in data]
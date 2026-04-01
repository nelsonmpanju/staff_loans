# Copyright (c) 2023, VV Systems Developer and contributors
# For license information, please see license.txt


import json
import math

import frappe
from frappe import _
from frappe.utils import get_link_to_form
from frappe.utils import (
	add_days,
	add_months,
	date_diff,
	flt,
	get_last_day,
	get_first_day,
	getdate,
	now_datetime,
	nowdate,
)
import os
import erpnext
from erpnext.accounts.doctype.journal_entry.journal_entry import get_payment_entry
from erpnext.controllers.accounts_controller import AccountsController

class StaffLoan(AccountsController):
	
	def before_update_after_submit(self):
		self.recalculate_repayment_schedule()
		
	def before_save(self):
		self.recalculate_repayment_schedule()
		self.check_staff_loan_settings()

	def recalculate_repayment_schedule(self):
		total_amount_paid = 0.00
		for data in self.repayment_schedule:
			if data.is_paid == 1:
				total_amount_paid += data.total_payment
		
		if self.total_amount_paid != total_amount_paid:
			self.total_amount_paid = total_amount_paid

		if self.total_payment == self.total_amount_paid and self.status != "Closed":
			self.status = "Closed"

	def check_staff_loan_settings(self):
		enable_multi_company = frappe.db.get_single_value('Staff Loan Settings', 'enable_multi_company')
		
		if not enable_multi_company:
			if not frappe.db.get_single_value('Staff Loan Settings', 'credit_account'):
				frappe.throw("Please complete settings on Staff Loan Settings Doctype")

		# Check if the "Staff Loan" Salary Component exists
		if enable_multi_company:
			if not frappe.db.exists("Staff Loan Company Setting",{'company':self.company}):
				frappe.throw("Please Create a Staff Loan Company Setting or disable Multi Company Support on Staff Loan Settings")

	def onload(self):
		self.check_staff_loan_settings()

	def on_update(self):
		self.validate_accounts()
		self.validate_cost_center()
		# self.set_status_from_docstatus()

	def after_submit_on_update(self):
		self.set_status_from_docstatus(self)

	def before_insert(self):
		self.status = "Sanctioned"
		self.disbursed_amount = 0.00

	def validate(self):
		self.validate_loan_application()
		self.validate_employee_status()
		self.set_loan_amount()
		self.validate_loan_amount()
		self.set_missing_fields()
		self.validate_cost_center()
		self.validate_accounts()
		self.check_sanctioned_amount_limit()

		if self.is_term_loan:
			validate_repayment_method(
				self.repayment_method,
				self.loan_amount,
				self.monthly_repayment_amount,
				self.repayment_periods,
				self.is_term_loan,
			)
			self.make_repayment_schedule()
			self.set_repayment_period()

		self.calculate_totals()

	def validate_loan_application(self):
		if self.loan_application:
			status = frappe.db.get_value("Staff Loan Application",self.loan_application,"status")
			docstatus = frappe.db.get_value("Staff Loan Application",self.loan_application,"docstatus")
			if int(docstatus) != 1 or status != "Approved": 
				frappe.throw(f"Please Submit or Approve Staff Loan Application ({self.loan_application}) before referencing it")

	def validate_employee_status(self):
		employee_status = frappe.db.get_value("Employee",self.applicant,"status")
		if employee_status != "Active":
			frappe.throw(_("Can Only Select an Active Employee."))
	
	def validate_accounts(self):
		for fieldname in [
			"payment_account",
			"loan_account",
		]:
			company = frappe.get_value("Account", self.get(fieldname), "company")

			if company != self.company:
				frappe.throw(
					_("Account {0} does not belongs to company {1}").format(
						frappe.bold(self.get(fieldname)), frappe.bold(self.company)
					)
				)

	def validate_cost_center(self):
		if not self.cost_center and self.rate_of_interest != 0.0:
			self.cost_center = frappe.db.get_value("Company", self.company, "cost_center")

			if not self.cost_center:
				frappe.throw(_("Cost center is mandatory for loans having rate of interest greater than 0"))

	# def on_submit(self):
		# self.link_loan_security_pledge()
		# Interest accrual for backdated term loans
		# self.accrue_loan_interest()

	def after_submit(self):
		# For non-opening balance loans, set status from docstatus
		if not self.is_opening_balance:
			self.set_status_from_docstatus()

	def on_cancel(self):
		self.before_cancel_document()
		self.ignore_linked_doctypes = ["GL Entry", "Payment Ledger Entry"]
		
	def before_cancel(self):
		if self.status == "Disbursed":
			frappe.throw("You can't Cancel a Disbursed Loan, Please Write Off the Loan")

	def before_cancel_document(self):
		connected_docs = frappe.get_list("Journal Entry", filters={"cheque_no": self.name},fields={"docstatus","name"})
			
		for doc in connected_docs:
			if doc.docstatus == 1:
				link = get_link_to_form("Journal Entry", doc.name)
				frappe.throw(_("You must cancel journal entry {0} before cancelling this document").format(link))
				
		connected_doc = frappe.get_list("Staff Loan Repayment", filters={"loan": self.name},fields={"docstatus","name"})

		if len(connected_doc)> 0:
			for doc in connected_doc:
				if doc.docstatus == 1:
					link = get_link_to_form("Staff Loan Repayment", doc.name)
					frappe.throw(_("You must cancel connected repayment entries {0} before cancelling this document").format(link))

	# frappe.db.after_cancel("Payment Entry", cancel_linked_journal_entry)

	def set_status_from_docstatus(self):
		self.status = self.docstatus

	def disburse_opening_balance_loan(self):
		"""
		Disburse opening balance loan without creating accounting entries.
		Sets the loan status to Disbursed directly.
		"""
		# Update each field individually using db_set
		self.db_set("status", "Disbursed", update_modified=False)
		self.db_set("disbursement_date", self.posting_date, update_modified=False)
		self.db_set("disbursed_amount", self.loan_amount, update_modified=False)

		frappe.msgprint(_("Opening Balance Loan {0} has been disbursed without creating accounting entries").format(
			frappe.bold(self.name)
		))


	def set_missing_fields(self):
		if not self.company:
			self.company = erpnext.get_default_company()

		if not self.posting_date:
			self.posting_date = nowdate()

		if self.loan_type and not self.rate_of_interest:
			self.rate_of_interest = frappe.db.get_value("Staff Loan Type", self.loan_type, "rate_of_interest")

		if self.repayment_method == "Repay Over Number of Periods":
			self.monthly_repayment_amount = get_monthly_repayment_amount(
				self.loan_amount, self.rate_of_interest, self.repayment_periods
			)

	def check_sanctioned_amount_limit(self):
		sanctioned_amount_limit = get_sanctioned_amount_limit(
			self.applicant_type, self.applicant, self.company
		)
		if sanctioned_amount_limit:
			total_loan_amount = get_total_loan_amount(self.applicant_type, self.applicant, self.company)

		if sanctioned_amount_limit and flt(self.loan_amount) + flt(total_loan_amount) > flt(
			sanctioned_amount_limit
		):
			frappe.throw(
				_("Sanctioned Amount limit crossed for {0} {1}").format(
					self.applicant_type, frappe.bold(self.applicant)
				)
			)

	def make_repayment_schedule(self):
		if not self.repayment_start_date:
			frappe.throw(_("Repayment Start Date is mandatory for term loans"))

		schedule_type_details = frappe.db.get_value(
			"Staff Loan Type", self.loan_type, ["repayment_schedule_type", "repayment_date_on"], as_dict=1
		)

		self.repayment_schedule = []
		payment_date = get_first_day(self.repayment_start_date)
		balance_amount = self.loan_amount

		while balance_amount > 0:
			interest_amount, principal_amount, balance_amount, total_payment = self.get_amounts(
				payment_date,
				balance_amount,
				schedule_type_details.repayment_schedule_type,
				schedule_type_details.repayment_date_on,
			)

			if schedule_type_details.repayment_schedule_type == "Pro-rated calendar months":
				next_payment_date = get_last_day(payment_date)
				if schedule_type_details.repayment_date_on == "Start of the next month":
					next_payment_date = add_days(next_payment_date, 1)

				payment_date = next_payment_date

			self.add_repayment_schedule_row(
				payment_date, principal_amount, total_payment, balance_amount
			)

			if (
				schedule_type_details.repayment_schedule_type == "Monthly as per repayment start date"
				or schedule_type_details.repayment_date_on == "End of the current month"
			):
				next_payment_date = add_single_month(payment_date)
				payment_date = next_payment_date

	def get_amounts(self, payment_date, balance_amount, schedule_type, repayment_date_on):
		if schedule_type == "Monthly as per repayment start date":
			days = 1
			months = 12
		else:
			expected_payment_date = get_last_day(payment_date)
			if repayment_date_on == "Start of the next month":
				expected_payment_date = add_days(expected_payment_date, 1)

			if expected_payment_date == payment_date:
				# using 30 days for calculating interest for all full months
				days = 30
				months = 365
			else:
				days = date_diff(get_last_day(payment_date), payment_date)
				months = 365

		interest_amount = flt(balance_amount * flt(self.rate_of_interest) * days / (months * 100))
		principal_amount = self.monthly_repayment_amount - interest_amount
		balance_amount = flt(balance_amount + interest_amount - self.monthly_repayment_amount)
		if balance_amount < 0:
			principal_amount += balance_amount
			balance_amount = 0.0

		total_payment = principal_amount + interest_amount

		return interest_amount, principal_amount, balance_amount, total_payment

	def add_repayment_schedule_row(
		self, payment_date, principal_amount, total_payment, balance_loan_amount
	):
		self.append(
			"repayment_schedule",
			{
				"payment_date": payment_date,
				"principal_amount": principal_amount,
				"total_payment": total_payment,
				"balance_loan_amount": balance_loan_amount,
			},
		)

	def set_repayment_period(self):
		if self.repayment_method == "Repay Fixed Amount per Period":
			repayment_periods = len(self.repayment_schedule)

			self.repayment_periods = repayment_periods

	def calculate_totals(self):
		self.total_payment = 0
		self.total_interest_payable = 0
		self.total_amount_paid = 0

		if self.is_term_loan:
			for data in self.repayment_schedule:
				self.total_payment += data.total_payment
		else:
			self.total_payment = self.loan_amount

	def set_loan_amount(self):
		if self.loan_application and not self.loan_amount:
			self.loan_amount = frappe.db.get_value("Staff Loan Application", self.loan_application, "loan_amount")

	def validate_loan_amount(self):
		if self.maximum_loan_amount and self.loan_amount > self.maximum_loan_amount:
			msg = _("Loan amount cannot be greater than {0}").format(self.maximum_loan_amount)
			frappe.throw(msg)

		if not self.loan_amount:
			frappe.throw(_("Loan amount is mandatory"))

def update_total_amount_paid(doc):
	total_amount_paid = 0
	for data in doc.repayment_schedule:
		if data.paid:
			total_amount_paid += data.total_payment
	frappe.db.set_value("Staff Loan", doc.name, "total_amount_paid", total_amount_paid)


def get_total_loan_amount(applicant_type, applicant, company):
	pending_amount = 0
	loan_details = frappe.db.get_all(
		"Staff Loan",
		filters={
			"applicant_type": applicant_type,
			"company": company,
			"applicant": applicant,
			"docstatus": 1,
			"status": ("!=", "Closed"),
		},
		fields=[
			"status",
			"total_payment",
			"disbursed_amount",
			"total_interest_payable",
			"total_principal_paid",
			"written_off_amount",
		],
	)

	interest_amount = 0.0

	for loan in loan_details:
		if loan.status in ("Disbursed", "Loan Closure Requested"):
			pending_amount += (
				flt(loan.total_payment)
				- flt(loan.total_interest_payable)
				- flt(loan.total_principal_paid)
				- flt(loan.written_off_amount)
			)
		elif loan.status == "Partially Disbursed":
			pending_amount += (
				flt(loan.disbursed_amount)
				- flt(loan.total_interest_payable)
				- flt(loan.total_principal_paid)
				- flt(loan.written_off_amount)
			)
		elif loan.status == "Sanctioned":
			pending_amount += flt(loan.total_payment)

	pending_amount += interest_amount

	return pending_amount


def get_sanctioned_amount_limit(applicant_type, applicant, company):
	if not frappe.db.exists("DocType", "Sanctioned Loan Amount"):
		return None
	return frappe.db.get_value(
		"Sanctioned Loan Amount",
		{"applicant_type": applicant_type, "company": company, "applicant": applicant},
		"sanctioned_amount_limit",
	)

@frappe.whitelist()
def validate_repayment_method(
	repayment_method, loan_amount, monthly_repayment_amount, repayment_periods, is_term_loan
):

	if is_term_loan and not repayment_method:
		frappe.throw(_("Repayment Method is mandatory for term loans"))

	if repayment_method == "Repay Over Number of Periods" and not repayment_periods:
		frappe.throw(_("Please enter Repayment Periods"))

	if repayment_method == "Repay Fixed Amount per Period":
		if not monthly_repayment_amount:
			frappe.throw(_("Please enter repayment Amount"))
		if monthly_repayment_amount > loan_amount:
			frappe.throw(_("Monthly Repayment Amount cannot be greater than Loan Amount"))

@frappe.whitelist()
def get_monthly_repayment_amount(loan_amount, rate_of_interest, repayment_periods):
	if rate_of_interest:
		monthly_interest_rate = flt(rate_of_interest) / (12 * 100)
		monthly_repayment_amount = math.ceil(
			(loan_amount * monthly_interest_rate * (1 + monthly_interest_rate) ** repayment_periods)
			/ ((1 + monthly_interest_rate) ** repayment_periods - 1)
		)
	else:
		repayment_periods = int(repayment_periods)
		monthly_repayment_amount = math.ceil(flt(loan_amount) / repayment_periods)
	return monthly_repayment_amount


@frappe.whitelist()
def request_loan_closure(loan,loan_amount, total_amount_paid):

	pending_amount = flt(loan_amount) - flt(total_amount_paid)

	if pending_amount > 0:
		frappe.throw(_("Cannot close loan as there is an outstanding of {0}").format(pending_amount))

	frappe.db.set_value("Staff Loan", loan, "status", "Loan Closure Requested")

@frappe.whitelist()
def get_loan_application(loan_application):
	loan = frappe.get_doc("Staff Loan Application", loan_application)
	if loan:
		return loan.as_dict()

def close_loan(loan, total_amount_paid):
	frappe.db.set_value("Staff Loan", loan, "total_amount_paid", total_amount_paid)
	frappe.db.set_value("Staff Loan", loan, "status", "Closed")

@frappe.whitelist()
def make_loan_write_off(loan, company=None, posting_date=None, amount=0, as_dict=0):
	if not company:
		company = frappe.get_value("Staff Loan", loan, "company")

	if not posting_date:
		posting_date = frappe.get_value("Staff Loan", loan, "posting_date")

	amount = frappe.get_value("Staff Loan", loan, "loan_amount")
	amt = 0
	pending_amount = frappe.get_value("Staff Loan", loan, "total_amount_paid")

	amt = amount - pending_amount

	payment_date = getdate()

	# get default write off account from company master
	write_off_account = frappe.get_value("Company", company, "write_off_account")

	write_off = frappe.new_doc("Staff Loan Repayment")
	write_off.applicant = frappe.get_value("Staff Loan", loan, "applicant")
	write_off.loan = loan
	write_off.cheque_date = posting_date
	write_off.payment_date = payment_date
	write_off.repayment_type = "Loan Write Off"
	write_off.write_off = write_off_account
	write_off.write_off_amount = amt
	write_off.company = company
	write_off.save()

	if as_dict:
		return write_off.as_dict()
	else:
		return write_off

@frappe.whitelist()
def make_loan_write_off_by_external_sources_entry(loan, company=None, posting_date=None, amount=0, as_dict=0):
	if not company:
		company = frappe.get_value("Staff Loan", loan, "company")

	if not posting_date:
		posting_date = frappe.get_value("Staff Loan", loan, "posting_date")

	amount = frappe.get_value("Staff Loan", loan, "loan_amount")
	amt = 0
	pending_amount = frappe.get_value("Staff Loan", loan, "total_amount_paid")

	amt = amount - pending_amount

	payment_date = getdate()

	write_off_by_external_sources = frappe.new_doc("Staff Loan Repayment")
	write_off_by_external_sources.applicant = frappe.get_value("Staff Loan", loan, "applicant")
	write_off_by_external_sources.loan = loan
	write_off_by_external_sources.cheque_date = posting_date
	write_off_by_external_sources.payment_date = payment_date
	write_off_by_external_sources.repayment_type = "External Sources"
	write_off_by_external_sources.repayment_amount = amt
	write_off_by_external_sources.description = "Loan Repayment By External Sources i.e. Cash, Cheque, Bank Transfer, etc."
	write_off_by_external_sources.company = company

	if as_dict:
		return write_off_by_external_sources.as_dict()
	else:
		return write_off_by_external_sources

def add_single_month(date):
	if getdate(date) == get_last_day(date):
		return get_last_day(add_months(date, 1))
	else:
		return add_months(date, 1)

@frappe.whitelist()
def make_refund_jv(loan, amount=0, reference_number=None, reference_date=None, submit=0):
	loan_details = frappe.db.get_value(
		"Staff Loan",
		loan,
		[
			"applicant_type",
			"applicant",
			"loan_account",
			"payment_account",
			"posting_date",
			"company",
			"name",
			"total_payment",
			"total_principal_paid",
		],
		as_dict=1,
	)

	loan_details.doctype = "Staff Loan"
	loan_details[loan_details.applicant_type.lower()] = loan_details.applicant

	if not amount:
		amount = flt(loan_details.total_principal_paid - loan_details.total_payment)

		if amount < 0:
			frappe.throw(_("No excess amount pending for refund"))

	refund_jv = get_payment_entry(
		loan_details,
		{
			"party_type": loan_details.applicant_type,
			"party_account": loan_details.loan_account,
			"amount_field_party": "debit_in_account_currency",
			"amount_field_bank": "credit_in_account_currency",
			"amount": amount,
			"bank_account": loan_details.payment_account,
		},
	)

	if reference_number:
		refund_jv.cheque_no = reference_number

	if reference_date:
		refund_jv.cheque_date = reference_date

	if submit:
		refund_jv.submit()

	return refund_jv


@frappe.whitelist()
def disburse_opening_balance(loan_name):
	"""
	Whitelisted method to disburse opening balance loan.
	Called from JavaScript after document submission.
	"""
	loan = frappe.get_doc("Staff Loan", loan_name)

	if not loan.is_opening_balance:
		frappe.throw(_("This loan is not marked as Opening Balance"))

	if loan.docstatus != 1:
		frappe.throw(_("Loan must be submitted first"))

	if loan.status == "Disbursed":
		return {"status": "already_disbursed"}

	# Update the loan status directly in database
	frappe.db.set_value("Staff Loan", loan_name, {
		"status": "Disbursed",
		"disbursement_date": loan.posting_date,
		"disbursed_amount": loan.loan_amount
	}, update_modified=False)

	frappe.msgprint(_("Opening Balance Loan {0} has been disbursed without creating accounting entries").format(
		frappe.bold(loan_name)
	))

	return {"status": "success"}


@frappe.whitelist()
def recalculate_schedule(loan_name):
	"""
	Recalculate the repayment schedule for a single Staff Loan.
	Verifies actual payments from Additional Salary and Staff Loan Repayment documents,
	then redistributes the remaining balance equally across new monthly installments.

	For existing data where schedule may be out of sync with actual payments,
	this function checks submitted payment documents as the source of truth.
	"""
	from dateutil.relativedelta import relativedelta

	staff_loan = frappe.get_doc("Staff Loan", loan_name)

	if staff_loan.docstatus != 1:
		frappe.throw(_("Loan must be submitted first"))

	if staff_loan.status == "Closed":
		frappe.throw(_("Cannot recalculate schedule for a closed loan"))

	monthly_repayment_amount = staff_loan.monthly_repayment_amount
	if not monthly_repayment_amount:
		frappe.throw(_("Monthly Repayment Amount is not set"))

	# Get staff loan salary component
	enable_multi_company = frappe.db.get_single_value('Staff Loan Settings', 'enable_multi_company')
	if enable_multi_company:
		staff_loan_component = frappe.db.get_value("Staff Loan Company Setting",
			{'company': staff_loan.company}, "staff_loan_component")
	else:
		staff_loan_component = frappe.db.get_single_value('Staff Loan Settings', 'salary_component')

	# Step 1: Find all confirmed salary payments for this loan
	# Key by Additional Salary reference NAME to prevent duplicates
	confirmed_salary_payments = {}  # Key: reference (name), Value: {amount, reference, payment_date}

	# Check schedule entries with payment_reference to submitted Additional Salary
	for d in staff_loan.repayment_schedule:
		if d.payment_reference:
			add_sal = frappe.db.get_value("Additional Salary", d.payment_reference,
				["docstatus", "amount", "payroll_date"], as_dict=True)
			if add_sal and add_sal.docstatus == 1:
				ref_name = d.payment_reference
				if ref_name not in confirmed_salary_payments:
					confirmed_salary_payments[ref_name] = {
						"amount": flt(add_sal.amount),
						"reference": ref_name,
						"payment_date": d.payment_date
					}

	# Also check for Additional Salary with ref_docname linking (newer data)
	if staff_loan_component:
		additional_salaries = frappe.get_all("Additional Salary", filters={
			"ref_doctype": "Staff Loan",
			"ref_docname": loan_name,
			"docstatus": 1,
			"salary_component": staff_loan_component,
		}, fields=["name", "amount", "payroll_date"])

		for add_sal in additional_salaries:
			ref_name = add_sal.name
			payment_date = add_sal.payroll_date.replace(day=1) if add_sal.payroll_date else None
			if payment_date and ref_name not in confirmed_salary_payments:
				confirmed_salary_payments[ref_name] = {
					"amount": flt(add_sal.amount),
					"reference": ref_name,
					"payment_date": payment_date
				}

	# Step 2: Find confirmed external/write-off payments
	# Key by repayment reference NAME to prevent duplicates (same doc found via schedule + direct query)
	confirmed_external_payments = {}  # Key: repayment_reference (name), Value: {amount, reference, type, payment_date}

	# Check schedule entries with repayment_reference
	for d in staff_loan.repayment_schedule:
		if d.outsource and d.repayment_reference:
			repayment = frappe.db.get_value("Staff Loan Repayment", d.repayment_reference,
				["docstatus", "repayment_amount", "write_off_amount", "repayment_type", "payment_date"], as_dict=True)
			if repayment and repayment.docstatus == 1:
				ref_name = d.repayment_reference
				if ref_name not in confirmed_external_payments:
					amount = repayment.repayment_amount if repayment.repayment_type == "External Sources" else repayment.write_off_amount
					confirmed_external_payments[ref_name] = {
						"amount": flt(amount),
						"reference": ref_name,
						"type": repayment.repayment_type,
						"payment_date": d.payment_date
					}

	# Also check Staff Loan Repayment docs directly
	repayments = frappe.get_all("Staff Loan Repayment", filters={
		"loan": loan_name,
		"docstatus": 1,
	}, fields=["name", "repayment_amount", "write_off_amount", "repayment_type", "payment_date"])

	for rep in repayments:
		ref_name = rep.name
		if ref_name not in confirmed_external_payments:
			amount = rep.repayment_amount if rep.repayment_type == "External Sources" else rep.write_off_amount
			confirmed_external_payments[ref_name] = {
				"amount": flt(amount),
				"reference": ref_name,
				"type": rep.repayment_type,
				"payment_date": rep.payment_date
			}

	# Step 3: Cancel Additional Salary entries that are NOT confirmed
	confirmed_refs = set(confirmed_salary_payments.keys())
	for d in staff_loan.repayment_schedule:
		if d.payment_reference and d.payment_reference not in confirmed_refs:
			if frappe.db.exists("Additional Salary", {"name": d.payment_reference, "docstatus": 1}):
				try:
					add_sal = frappe.get_doc("Additional Salary", d.payment_reference)
					add_sal.cancel()
				except Exception:
					pass

	# Step 4: Rebuild schedule with confirmed payments
	staff_loan.repayment_schedule = []
	balance = flt(staff_loan.loan_amount)

	# Collect all payment dates and sort
	salary_payment_dates = [p["payment_date"] for p in confirmed_salary_payments.values()]
	external_payment_dates = [p["payment_date"] for p in confirmed_external_payments.values()]
	all_payment_dates = sorted(set(salary_payment_dates + external_payment_dates))

	# Add confirmed salary payments to schedule (sorted by payment_date)
	for payment in sorted(confirmed_salary_payments.values(), key=lambda x: x["payment_date"]):
		balance -= flt(payment["amount"])
		staff_loan.append("repayment_schedule", {
			"payment_date": payment["payment_date"],
			"principal_amount": payment["amount"],
			"total_payment": payment["amount"],
			"balance_loan_amount": balance,
			"is_paid": 1,
			"outsource": 0,
			"payment_reference": payment["reference"],
		})

	# Add confirmed external payments to schedule (sorted by payment_date)
	for payment in sorted(confirmed_external_payments.values(), key=lambda x: x["payment_date"]):
		balance -= flt(payment["amount"])
		staff_loan.append("repayment_schedule", {
			"payment_date": payment["payment_date"],
			"principal_amount": payment["amount"],
			"total_payment": payment["amount"],
			"balance_loan_amount": balance,
			"is_paid": 1,
			"outsource": 1,
			"repayment_reference": payment["reference"],
		})

	# Step 5: Determine next payment date for new installments
	if all_payment_dates:
		last_payment = max(all_payment_dates)
		next_date = last_payment.replace(day=1) + relativedelta(months=1)
	elif staff_loan.repayment_start_date:
		next_date = staff_loan.repayment_start_date.replace(day=1)
	else:
		next_date = getdate(nowdate()).replace(day=1)

	# Step 6: Create new equal installments for remaining balance
	remaining_balance = balance
	if remaining_balance > 0 and monthly_repayment_amount > 0:
		payment_date = next_date
		bal = remaining_balance
		while bal > 0:
			installment = min(flt(bal), flt(monthly_repayment_amount))
			bal = flt(bal - installment)
			staff_loan.append("repayment_schedule", {
				"payment_date": payment_date,
				"principal_amount": installment,
				"total_payment": installment,
				"balance_loan_amount": bal,
				"is_paid": 0,
			})
			payment_date = payment_date + relativedelta(months=1)

	# Re-index and recalculate balances in correct order
	sorted_schedule = sorted(staff_loan.repayment_schedule, key=lambda x: (x.payment_date, -x.is_paid))
	balance = flt(staff_loan.loan_amount)
	for i, d in enumerate(sorted_schedule):
		d.idx = i + 1
		if d.is_paid:
			balance -= flt(d.total_payment)
		d.balance_loan_amount = balance

	staff_loan.save()

	total_paid = flt(staff_loan.loan_amount) - balance
	return {
		"status": "success",
		"message": _("Schedule recalculated: {0} paid, {1} remaining").format(
			frappe.format_value(total_paid, {"fieldtype": "Currency"}),
			frappe.format_value(remaining_balance, {"fieldtype": "Currency"})
		)
	}

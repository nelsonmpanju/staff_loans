# Copyright (c) 2023, VV System Developers LTD and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from dateutil.relativedelta import relativedelta

from frappe import _
from frappe.utils import cint, flt
from datetime import datetime, timedelta, date
from frappe.utils import nowdate

class StaffLoanRepayment(Document):
	def before_save(self):
		self.set_missing_values()
		self.validate_amount()
		
	def on_submit(self):
		if self.repayment_type == "Loan Write Off":
			self.reschedule_repayment_schedule()
			self.update_outstanding_amount()
		elif self.repayment_type == "External Sources":
			self.process_external_repayment()
		self.create_journal_entry()

	def create_journal_entry(self):
		posting_date = self.payment_date or nowdate()
		if self.repayment_type == "External Sources":
			journal_entry = frappe.new_doc("Journal Entry")
			journal_entry.voucher_type = "Journal Entry"
			journal_entry.company = self.company
			journal_entry.posting_date = posting_date
			journal_entry.user_remark = "Loan Repayment \n" + self.description
			journal_entry.cheque_no = self.name
			journal_entry.cheque_date = self.cheque_date

			journal_entry.append("accounts", {
				"account": self.repayment_account,
				"debit_in_account_currency": self.repayment_amount,
				"credit_in_account_currency": 0
				})
			journal_entry.append("accounts", {
				"account": self.loan_account,
				"party_type": self.applicant_type,
				"party": self.applicant,
				"credit_in_account_currency": self.repayment_amount,
				"debit_in_account_currency": 0
				})
			journal_entry.save()
			journal_entry.submit()
		elif self.repayment_type == "Loan Write Off":
			journal_entry = frappe.new_doc("Journal Entry")
			journal_entry.voucher_type = "Journal Entry"
			journal_entry.company = self.company
			journal_entry.posting_date = posting_date
			journal_entry.user_remark = "Loan Write Off"
			journal_entry.cheque_no = self.name
			journal_entry.cheque_date = self.cheque_date

			journal_entry.append("accounts", {
				"account": self.write_off,
				"debit_in_account_currency": self.write_off_amount,
				"credit_in_account_currency": 0
				})
			journal_entry.append("accounts", {
				"account": self.loan_account,
				"party_type": self.applicant_type,
				"party": self.applicant,
				"credit_in_account_currency": self.write_off_amount,
				"debit_in_account_currency": 0
				})
			journal_entry.save()
			journal_entry.submit()

	def on_cancel(self):
		if self.repayment_type == "Loan Write Off":
			self.cancel_reschedule_repayment_schedule()
			self.update_outstanding_amount(cancel=1)
		elif self.repayment_type == "External Sources":
			self.cancel_external_repayment()

	def update_outstanding_amount(self, cancel=0):
		written_off_amount = frappe.db.get_value("Staff Loan", self.loan, "written_off_amount")
		total_amount_paid = frappe.db.get_value("Staff Loan", self.loan, "total_amount_paid")

		if cancel:
			written_off_amount -= self.write_off_amount
			total_amount_paid -= self.write_off_amount
		else:
			written_off_amount += self.write_off_amount
			total_amount_paid += self.write_off_amount

		frappe.db.set_value("Staff Loan", self.loan, "written_off_amount", written_off_amount)
		frappe.db.set_value("Staff Loan", self.loan, "total_amount_paid", total_amount_paid)

	def update_outstanding_amount2(self, cancel=0):
		rep_amount = frappe.db.get_value("Staff Loan", self.loan, "total_amount_paid")

		if cancel:
			rep_amount -= self.repayment_amount
		else:
			rep_amount += self.repayment_amount

		frappe.db.set_value("Staff Loan", self.loan, "total_amount_paid", rep_amount)

	def process_external_repayment(self):
		"""
		Process external/cash repayment:
		1. Cancel Additional Salary entries linked to unpaid schedule rows
		2. Remove all unpaid schedule entries
		3. Add a paid entry for the external payment
		4. Redistribute remaining balance equally across new monthly installments
		"""
		staff_loan = frappe.get_doc("Staff Loan", self.loan)
		monthly_repayment_amount = staff_loan.monthly_repayment_amount

		# Cancel Additional Salary entries linked to unpaid schedule rows
		for d in staff_loan.repayment_schedule:
			if d.is_paid == 0 and d.payment_reference:
				if frappe.db.exists("Additional Salary", d.payment_reference):
					add_sal = frappe.get_doc("Additional Salary", d.payment_reference)
					if add_sal.docstatus == 1:
						add_sal.cancel()

		# Determine next payment date from last paid salary entry
		paid_dates = [d.payment_date for d in staff_loan.repayment_schedule if d.is_paid == 1]
		if paid_dates:
			next_date = max(paid_dates).replace(day=1) + relativedelta(months=1)
		else:
			next_date = (staff_loan.repayment_start_date or self.payment_date).replace(day=1)

		# Collect paid entries to keep (exclude any existing entry for THIS repayment to avoid duplicates)
		paid_entries = []
		for d in staff_loan.repayment_schedule:
			if d.is_paid == 1 and d.repayment_reference != self.name:
				paid_entries.append({
					"payment_date": d.payment_date,
					"principal_amount": d.principal_amount,
					"total_payment": d.total_payment,
					"is_paid": 1,
					"outsource": getattr(d, "outsource", 0),
					"repayment_reference": d.repayment_reference,
					"payment_reference": d.payment_reference,
				})

		# Rebuild schedule from scratch
		staff_loan.repayment_schedule = []

		# Re-add existing paid entries
		balance = flt(staff_loan.loan_amount)
		for entry in paid_entries:
			balance -= flt(entry["total_payment"])
			entry["balance_loan_amount"] = balance
			staff_loan.append("repayment_schedule", entry)

		# Calculate remaining balance after external payment
		remaining_balance = flt(balance) - flt(self.repayment_amount)

		# Add paid entry for the external payment
		staff_loan.append("repayment_schedule", {
			"payment_date": self.payment_date,
			"principal_amount": self.repayment_amount,
			"total_payment": self.repayment_amount,
			"balance_loan_amount": remaining_balance,
			"is_paid": 1,
			"outsource": 1,
			"repayment_reference": self.name,
		})

		# Create new equal installments for remaining balance
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

	def cancel_external_repayment(self):
		"""
		Cancel external repayment:
		1. Cancel Additional Salary entries linked to unpaid schedule rows
		2. Remove external payment entry and all unpaid rows
		3. Redistribute remaining balance (with repayment added back) equally
		"""
		staff_loan = frappe.get_doc("Staff Loan", self.loan)
		monthly_repayment_amount = staff_loan.monthly_repayment_amount

		# Cancel Additional Salary entries linked to unpaid schedule rows
		for d in staff_loan.repayment_schedule:
			if d.is_paid == 0 and d.payment_reference:
				if frappe.db.exists("Additional Salary", d.payment_reference):
					add_sal = frappe.get_doc("Additional Salary", d.payment_reference)
					if add_sal.docstatus == 1:
						add_sal.cancel()

		# Keep only salary-paid entries (exclude external payment entry and unpaid rows)
		paid_entries = []
		for d in staff_loan.repayment_schedule:
			if d.is_paid == 1 and d.repayment_reference != self.name:
				paid_entries.append({
					"payment_date": d.payment_date,
					"principal_amount": d.principal_amount,
					"total_payment": d.total_payment,
					"is_paid": 1,
					"outsource": getattr(d, "outsource", 0),
					"repayment_reference": d.repayment_reference,
					"payment_reference": d.payment_reference,
				})

		# Determine next payment date
		paid_dates = [e["payment_date"] for e in paid_entries]
		if paid_dates:
			next_date = max(paid_dates).replace(day=1) + relativedelta(months=1)
		else:
			next_date = (staff_loan.repayment_start_date or self.payment_date).replace(day=1)

		# Rebuild schedule
		staff_loan.repayment_schedule = []

		balance = flt(staff_loan.loan_amount)
		for entry in paid_entries:
			balance -= flt(entry["total_payment"])
			entry["balance_loan_amount"] = balance
			staff_loan.append("repayment_schedule", entry)

		# Remaining balance (external payment reversed, so full remaining)
		remaining_balance = balance

		# Create new equal installments for remaining balance
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

	def validate_amount(self):
		precision = cint(frappe.db.get_default("currency_precision")) or 2
		total_payment, total_amount_paid = frappe.get_value("Staff Loan",self.loan,["total_payment", "total_amount_paid"],)

		pending_amount = flt(
			flt(total_payment) - flt(total_amount_paid),
			precision,
			)
		if self.repayment_type == "Loan Write Off":
			if self.write_off_amount > pending_amount:
				frappe.throw(_("Write off amount cannot be greater than pending loan amount"))
		elif self.repayment_type == "External Sources":
			if self.repayment_amount > pending_amount:
				frappe.throw(_("Repayment amount cannot be greater than pending loan amount"))

	def set_missing_values(self):
		if self.repayment_type == "Loan Write Off":
			self.repayment_amount = 0
			self.repayment_account = ""
			self.description = ""
		elif self.repayment_type == "External Sources":
			self.write_off_amount = 0
			self.write_off = ""

	def reschedule_repayment_schedule(self):
		staff_loan = frappe.get_doc("Staff Loan", self.loan)
		options = []
		option2 = []
		for d in staff_loan.repayment_schedule:
			if d.is_paid == 1:
				options.append({'value': d.payment_date, 'label': d.payment_date})
			if d.is_paid == 0:
				option2.append({'value': d.payment_date, 'label': d.payment_date})
		if len(options) == 0:
			last_payment_date = option2[0]['value']
			# last_payment_date = datetime.strptime(self.payment_date, "%Y-%m-%d")
			# last_payment_date = last_payment_date.replace(day=1)
		else:
			last_payment_date = options[-1]['value']
			last_payment_date = last_payment_date.replace(day=1)
			last_payment_date = last_payment_date + relativedelta(months=1)
			last_payment_date = last_payment_date.replace(day=1)

		if self.repayment_type == "External Sources":
			repayment_amount = self.repayment_amount
		elif self.repayment_type == "Loan Write Off":
			repayment_amount = self.write_off_amount

		loan_amount = staff_loan.loan_amount - staff_loan.total_amount_paid
		monthly_repayment_amount = staff_loan.monthly_repayment_amount
		loan_amount -= repayment_amount

		# frappe.throw("amount: " + str(repayment_amount) + " loan: " + str(self.loan) + " date: " + str(last_payment_date) + " loan_amount: " + str(loan_amount) + " input_amount: " + str(repayment_amount) + " input_date: " + str(last_payment_date) + " type: " + str("Repayment") + " source: " + str(self.name))
		# args = {
		# 	"amount": repayment_amount,
		# 	"loan": self.loan,
		# 	"payment_date": last_payment_date.strftime("%Y-%m-%d"),
		# 	"loan_amount": loan_amount,
		# 	"input_amount": repayment_amount,
		# 	"input_date": last_payment_date.strftime("%Y-%m-%d"),
		# 	"type": "Repayment",
		# 	"source": self.name
		# }
		# frappe.throw("args: " + str(args))
		response = frappe.call(
    		"staff_loans.custom.loan.update_additional_salary",
			amount=repayment_amount,
			loan=self.loan,
			payment_date=last_payment_date.strftime("%Y-%m-%d"),
			loan_amount=loan_amount,
			input_amount=repayment_amount,
			input_date=last_payment_date.strftime("%Y-%m-%d"),
			type="Repayment",
			source=self.name
		)

		if response == "pass":
			frappe.msgprint("Repayment schedule refreshed")

		# repayment_schedule = []
		# payment_counter = 0

		# while loan_amount > 0:
		# 	payment = {}
		# 	payment_date = last_payment_date
		# 	payment["payment_date"] = payment_date.replace(day=1)
		# 	payment["payment_date"] = payment["payment_date"] + relativedelta(months=1 * payment_counter)
		# 	payment["principal_amount"] = min(loan_amount, monthly_repayment_amount)
		# 	payment["total_payment"] = payment["principal_amount"]
		# 	loan_amount -= payment["principal_amount"]
		# 	payment["balance_loan_amount"] = loan_amount
		# 	repayment_schedule.append(payment)

		# 	payment_counter += 1

		# to_remove = []
		# to_add = []
		# for d in staff_loan.repayment_schedule:
		# 	if d.is_paid == 0:
		# 		to_remove.append(d)
		# for d in to_remove:
		# 	staff_loan.remove(d)
		# for i, d in enumerate(staff_loan.repayment_schedule):
		# 	d.idx = i + 1

		# # for d in staff_loan.repayment_schedule:
		# # 	if not d.is_paid:
		# # 		staff_loan.repayment_schedule.remove(d)
		# loan_amountt = staff_loan.loan_amount - staff_loan.total_amount_paid
		# loan_amountt -= repayment_amount
		
		# # staff_loan.repayment_schedule = []
		# payment_dt = last_payment_date
		# payment_dt = payment_dt.replace(day=1)
		# staff_loan.append("repayment_schedule", {
		# 	"payment_date": payment_dt.strftime("%Y-%m-%d"),
		# 	"principal_amount": 0,
		# 	"total_payment": repayment_amount,
		# 	"balance_loan_amount": loan_amountt,
		# 	"is_paid": 1,
		# 	"outsource": 1,
		# 	"repayment_reference": self.name

		# })
		# # staff_loan.save()

		# for d in repayment_schedule:
		# 	payment_date = d["payment_date"]
		# 	payment_datee = payment_date.replace(day=1)
		# 	staff_loan.append("repayment_schedule", {
		# 		"payment_date": payment_datee.strftime("%Y-%m-%d"),
		# 		"principal_amount": 0,
		# 		"total_payment": d["total_payment"],
		# 		"balance_loan_amount": d["balance_loan_amount"],
		# 		"is_paid": 0
		# 	})
		
		# staff_loan.save()

	def cancel_reschedule_repayment_schedule(self):
		staff_loan = frappe.get_doc("Staff Loan", self.loan)
		options = []
		options2 = []
		options4 = []
		options5 = []

		payment_d = self.payment_date
		first_day_of_month = payment_d.replace(day=1)

		for d in staff_loan.repayment_schedule:
			if d.is_paid == 1 and d.payment_date > first_day_of_month:
				options.append({'value': d.total_payment, 'label': d.total_payment})
				options2.append({'value': d.payment_date, 'label': d.payment_date})
			if d.is_paid == 1 and d.payment_date < first_day_of_month:
				options4.append({'value': d.total_payment, 'label': d.total_payment})
				options5.append({'value': d.payment_date, 'label': d.payment_date})

		# prev_month = payment_date.replace(day=1) - timedelta(days=1)
		# first_day_prev_month = prev_month.replace(day=1)
		# print("First day of previous month:", first_day_prev_month.strftime("%Y-%m-%d"))
		if len(options) > 0:
			last_payment_date = options2[-1]['value']
			last_payment_date = last_payment_date + relativedelta(months=1)
			last_payment_date = last_payment_date.replace(day=1)
			# options[-1]["value"]
		else:
			if len(options5) > 0:
				last_payment_date = options5[-1]['value']
				last_payment_date = last_payment_date + relativedelta(months=1)
				last_payment_date = last_payment_date.replace(day=1)
			else:
				last_payment_date = first_day_of_month
			
		if self.repayment_type == "External Sources":
			repayment_amount = self.repayment_amount
		elif self.repayment_type == "Loan Write Off":
			repayment_amount = self.write_off_amount

		loan_amount = staff_loan.loan_amount - staff_loan.total_amount_paid
		monthly_repayment_amount = staff_loan.monthly_repayment_amount
		loan_amount += repayment_amount

		repayment_schedule = []
		payment_counter = 0

		while loan_amount > 0:
			payment = {}
			payment_date = last_payment_date
			# next_month = payment_date + relativedelta(months=1)
			payment["payment_date"] = payment_date.replace(day=1)
			payment["payment_date"] = payment["payment_date"] + relativedelta(months=1 * payment_counter)
			payment["principal_amount"] = min(loan_amount, monthly_repayment_amount)
			payment["total_payment"] = payment["principal_amount"]
			loan_amount -= payment["principal_amount"]
			payment["balance_loan_amount"] = loan_amount
			repayment_schedule.append(payment)

			payment_counter += 1

		to_remove = []
		to_add = []
		for d in staff_loan.repayment_schedule:
			if d.is_paid == 0 or d.payment_date == first_day_of_month:
				to_remove.append(d)
			if d.is_paid == 1 and d.payment_date != first_day_of_month:
				to_add.append(d)

		for d in to_remove:
			staff_loan.remove(d)
		staff_loan.repayment_schedule = []
		loan_amounts = staff_loan.loan_amount
		for d in to_add:
			loan_amounts -= d.total_payment
			staff_loan.append("repayment_schedule", {
				"payment_date": d.payment_date,
				"principal_amount": d.principal_amount,
				"total_payment": d.total_payment,
				"balance_loan_amount": loan_amounts,
				"is_paid": 1,
				"outsource": d.outsource,
				"repayment_reference": d.repayment_reference

			})
		for i, d in enumerate(staff_loan.repayment_schedule):
			d.idx = i + 1

		# for d in staff_loan.repayment_schedule:
		# 	if not d.is_paid:
		# 		staff_loan.repayment_schedule.remove(d)
		# loan_amountt = staff_loan.loan_amount - staff_loan.total_amount_paid
		# loan_amountt -= repayment_amount
		
		# # staff_loan.repayment_schedule = [] # very dangerous code but may come in handy
		# payment_date = datetime.strptime(self.payment_date, "%Y-%m-%d").date()
		# payment_date = payment_date.replace(day=1)
		# staff_loan.append("repayment_schedule", {
		# 	"payment_date": payment_date.strftime("%Y-%m-%d"),
		# 	"principal_amount": 0,
		# 	"total_payment": repayment_amount,
		# 	"balance_loan_amount": loan_amountt,
		# 	"is_paid": 1
		# })
		# staff_loan.save()

		for d in repayment_schedule:
			staff_loan.append("repayment_schedule", {
				"payment_date": d["payment_date"],
				"principal_amount": d["principal_amount"],
				"total_payment": d["total_payment"],
				"balance_loan_amount": d["balance_loan_amount"]
			})
		
		staff_loan.save()
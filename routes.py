# app/routes.py
from flask import Blueprint, render_template, redirect, url_for, request, flash, send_file
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from datetime import datetime, date
from models import db, User, Staff, IncrementHistory, SalaryRecord, ProfessionalTax
from utils import calculate_salary_components
from fpdf import FPDF
import io
import pandas as pd
import calendar
from flask import jsonify

routes = Blueprint('routes', __name__)

# -------------------------
# LOGIN MANAGER
# -------------------------
login_manager = LoginManager()
login_manager.login_view = 'routes.login'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# -------------------------
# HOME & AUTH
# -------------------------
@routes.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('routes.home_page'))
    return redirect(url_for('routes.login'))


@routes.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            flash('Login successful!', 'success')
            return redirect(url_for('routes.home_page'))

        else:
            flash('Invalid username or password.', 'error')
            return redirect(url_for('routes.login'))

    return render_template('login.html', title='Login')


@routes.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('routes.login'))

@routes.route('/home')
@login_required
def home_page():
    """
    Displays the role selection page (Admin, Accounts, HR).
    """
    return render_template('home.html', title='Home')

@routes.route('/role/<role>')
@login_required
def role_access(role):
    """
    Restricts users to access only their own role.
    Admin → Admin pages
    Accounts → Accounts pages
    HR → HR pages
    Superuser → allowed everywhere
    """
    role = role.lower()

    # SUPERUSER: always allowed
    if current_user.is_superuser:
        # If superuser wants the HR landing, send them to lop_page (same behaviour as hr)
        if role == 'hr':
            return redirect(url_for('routes.lop_page'))
        # For admin/accounts, dashboard is fine as a common landing
        return redirect(url_for('routes.dashboard'))

    # Normal role checks
    if role == 'admin':
        if current_user.is_admin:
            return redirect(url_for('routes.dashboard'))
        else:
            flash("Access denied: Only Admins can access this section.", "error")
            return redirect(url_for('routes.home_page'))

    elif role == 'accounts':
        if current_user.is_accounts:
            return redirect(url_for('routes.dashboard'))
        else:
            flash("Access denied: Only Accounts users can access this section.", "error")
            return redirect(url_for('routes.home_page'))

    elif role == 'hr':
        if current_user.is_hr:
            return redirect(url_for('routes.lop_page'))
        else:
            flash("Access denied: Only HR users can access this section.", "error")
            return redirect(url_for('routes.home_page'))

    else:
        flash("Invalid role selected.", "error")
        return redirect(url_for('routes.home_page'))

# -------------------------
# DASHBOARD
# -------------------------
@routes.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', title='Dashboard', user=current_user)


# -------------------------
# STAFF DETAILS
# -------------------------
@routes.route('/staff/details')
@login_required
def staff_details():
    if not (current_user.is_accounts or current_user.is_superuser):
        flash("Access denied: Only Accounts and Admin users can view staff details.", "error")
        return redirect(url_for('routes.dashboard'))

    search_query = request.args.get('search', '').lower().strip()

    if search_query:
        staff_list = Staff.query.filter(
            (Staff.name.ilike(f"%{search_query}%")) |
            (Staff.department.ilike(f"%{search_query}%")) |
            (Staff.staff_id.like(f"%{search_query}%"))
        ).all()
    else:
        staff_list = Staff.query.order_by(Staff.staff_id.asc()).all()

    return render_template('staff_details.html',
                           title='Staff Details',
                           staff_list=staff_list)


# -------------------------
# NEW STAFF ENTRY
# -------------------------
@routes.route('/staff/new', methods=['GET', 'POST'])
@login_required
def new_staff():
    if not (current_user.is_hr or current_user.is_superuser):
        flash("Access denied: HR users only.", "error")
        return redirect(url_for('routes.dashboard'))

    last_staff = Staff.query.order_by(Staff.staff_id.desc()).first()
    next_staff_id = (last_staff.staff_id + 1) if last_staff else 1001

    if request.method == 'POST':
        staff = Staff(
            staff_id=next_staff_id,
            name=request.form['name'],
            category=request.form['category'],
            department=request.form['department'],
            designation=request.form['designation'],
            base_salary=float(request.form['base_salary'] or 0),
            epf_eligible = (request.form.get('epf_eligible') == "Yes"),
            allowances=float(request.form.get('allowances', 0) or 0),
            deductions=float(request.form.get('deductions', 0) or 0),
            date_joined=datetime.strptime(request.form['date_joined'], '%Y-%m-%d'),
            bank_account=request.form['bank_account'],
            aadhar=request.form['aadhar'],
            pf_number=request.form.get('pf_number'),
            esi_number=request.form.get('esi_number'),
            active=True 
        )

        db.session.add(staff)
        db.session.commit()

        flash(f"Staff member '{staff.name}' added successfully with ID {staff.staff_id}!", "success")
        return redirect(url_for('routes.new_staff'))

    return render_template('new_staff.html', title='New Staff Entry', next_staff_id=next_staff_id)


# -------------------------
# LOSS OF PAY ENTRY (ADMIN ONLY) - ONLY SAVES LOP, NO SALARY RECORD
# -------------------------
@routes.route('/lop', methods=['GET', 'POST'])
@login_required
def lop_page():
    if not (current_user.is_admin or current_user.is_superuser):
        flash("Access denied: Admins users only.", "error")
        return redirect(url_for('routes.dashboard'))

    staff_list = Staff.query.filter_by(active=True).order_by(Staff.name.asc()).all()
    today = date.today()

    # Default month/year = current
    selected_month_str = request.form.get('lop_month') or request.args.get('month')
    if selected_month_str:
        year, month = map(int, selected_month_str.split('-'))
    else:
        month, year = today.month, today.year
        selected_month_str = f"{year}-{month:02d}"

    if request.method == 'POST':
        try:
            for staff in staff_list:
                lop_value = request.form.get(f'lop_{staff.id}')
                if lop_value and lop_value.strip() != '':
                    lop_days = float(lop_value)
                    
                    # Find or create a "draft" salary record with only LOP data
                    record = SalaryRecord.query.filter_by(
                        staff_id=staff.id,
                        month=month,
                        year=year
                    ).first()

                    if record:
                        # Update existing record's LOP
                        record.lop_days = lop_days
                    else:
                        # Create new record with only LOP and gross salary
                        # Don't compute final net salary yet - that happens in D&R page
                        new_record = SalaryRecord(
                            staff_id=staff.id,
                            month=month,
                            year=year,
                            lop_days=lop_days,
                            gross_salary=staff.base_salary + (staff.allowances or 0),
                            # All other fields remain 0/default
                            net_salary=0  # Will be calculated in D&R page
                        )
                        db.session.add(new_record)
            
            db.session.commit()
            flash(f"LOP days for {selected_month_str} updated successfully!", "success")

        except Exception as e:
            db.session.rollback()
            flash(f"Error updating LOP days: {e}", "error")

    return render_template(
        'lop.html',
        title='Loss of Pay',
        staff_list=staff_list,
        selected_month=selected_month_str,
        selected_year=year,
        selected_month_int=month,
        current_year=today.year,
        current_month=today.month
    )

@routes.route('/api/get_lop', methods=['GET'])
@login_required
def api_get_lop():
    """
    Return lop_days for a given staff_id/month/year if a SalaryRecord exists.
    Query params: staff_id, month, year
    """
    if not (current_user.is_superuser or current_user.is_accounts or current_user.is_admin):
        return jsonify({"error": "Access denied"}), 403

    staff_id = request.args.get('staff_id', type=int)
    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)

    if not (staff_id and month and year):
        return jsonify({"lop_days": 0.0, "message": "missing params"}), 200

    rec = SalaryRecord.query.filter_by(staff_id=staff_id, month=month, year=year).first()
    lop_days = float(rec.lop_days) if rec and rec.lop_days is not None else 0.0
    return jsonify({"lop_days": lop_days}), 200

# -------------------------
# DEDUCTIONS & REIMBURSEMENTS - COMPLETES THE SALARY RECORD
# -------------------------
@routes.route('/d_r', methods=['GET', 'POST'])
@login_required
def d_r_page():
    if not (current_user.is_accounts or current_user.is_superuser):
        flash("Access denied: Accounts users only.", "error")
        return redirect(url_for('routes.dashboard'))

    staff_list = Staff.query.filter_by(active=True).order_by(Staff.name.asc()).all()
    result = None
    selected_staff_id = None
    selected_month = None

    if request.method == 'POST':
        try:
            staff_db_id = request.form.get('employee_name')
            month_str = request.form.get('salary_month')

            if not (staff_db_id and month_str):
                flash("Missing staff or month selection.", "error")
                return redirect(url_for('routes.d_r_page'))

            year, month = map(int, month_str.split('-'))
            selected_month = month_str
            selected_staff_id = int(staff_db_id)

            staff = Staff.query.get(staff_db_id)
            if not staff:
                flash("Invalid staff selected.", "error")
                return redirect(url_for('routes.d_r_page'))

            # Find existing salary record (should have LOP data from lop_page)
            existing_record = SalaryRecord.query.filter_by(
                staff_id=staff.id, month=month, year=year
            ).first()
            
            # Get LOP days (from existing record or default to 0)
            lop_days = existing_record.lop_days if existing_record else 0

            gross = staff.base_salary + (getattr(staff, 'allowances', 0) or 0)
            
            deductions = {
                "it": float(request.form.get('income_tax', 0)),
                "loan": float(request.form.get('loan', 0)),
                "advance": float(request.form.get('advance', 0)),
                "uniform": float(request.form.get('uniform', 0)),
                "cd": float(request.form.get('cd', 0)),
                "hostel": float(request.form.get('hostel', 0)),
                "misc": float(request.form.get('misc', 0))
            }
            
            reimbursements = [
                float(x or 0)
                for x in request.form.getlist('reimbursement_amount[]')
                if x and str(x).strip()
            ]

            # Apply Professional Tax Logic
            pt_deduction = 0
            if month in [2, 8]:
                slab = ProfessionalTax.query.filter(
                    ProfessionalTax.range_from <= staff.base_salary,
                    ProfessionalTax.range_to >= staff.base_salary
                ).first()
                if slab:
                    pt_deduction = slab.tax_amount
                    deductions["professional_tax"] = pt_deduction
            else:
                deductions["professional_tax"] = 0

            epf_eligible = getattr(staff, "epf_eligible", False)

            # Compute salary components
            result = calculate_salary_components(
                gross_salary=gross,
                lop_days=lop_days,
                deductions=deductions,
                reimbursements=reimbursements,
                month=month,
                year=year,
                epf_eligible=epf_eligible
            )

            # Update or create the final salary record
            if existing_record:
                # Update existing record with complete data
                existing_record.gross_salary = result['adjusted_gross']
                existing_record.lop_days = lop_days
                existing_record.lop_amount = result['lop_amount']
                existing_record.epf = result['epf']
                existing_record.esi = result['esi']
                existing_record.it = deductions["it"]
                existing_record.loan = deductions["loan"]
                existing_record.advance = deductions["advance"]
                existing_record.uniform = deductions["uniform"]
                existing_record.cd = deductions["cd"]
                existing_record.hostel = deductions["hostel"]
                existing_record.misc = deductions["misc"]
                existing_record.total_deductions = result['total_deductions']
                existing_record.total_reimbursements = result['total_reimbursements']
                existing_record.net_salary = result['net_salary']
            else:
                # Create new record (in case LOP wasn't entered)
                record = SalaryRecord(
                    staff_id=staff.id,
                    month=month,
                    year=year,
                    gross_salary=result['adjusted_gross'],
                    lop_days=lop_days,
                    lop_amount=result['lop_amount'],
                    epf=result['epf'],
                    esi=result['esi'],
                    it=deductions["it"],
                    loan=deductions["loan"],
                    advance=deductions["advance"],
                    uniform=deductions["uniform"],
                    cd=deductions["cd"],
                    hostel=deductions["hostel"],
                    misc=deductions["misc"],
                    total_deductions=result['total_deductions'],
                    total_reimbursements=result['total_reimbursements'],
                    net_salary=result['net_salary']
                )
                db.session.add(record)

            db.session.commit()

            flash(f"Salary record for {staff.name} (Staff ID: {staff.staff_id}) for {month_str} saved successfully!", "success")

            return render_template(
                'd_r.html',
                title='Deductions & Reimbursements',
                staff_list=staff_list,
                result=result,
                selected_staff_id=selected_staff_id,
                selected_month=selected_month
            )

        except Exception as e:
            db.session.rollback()
            flash(f"Error saving salary record: {str(e)}", "error")

    return render_template('d_r.html',
                           title='Deductions & Reimbursements',
                           staff_list=staff_list,
                           result=result,
                           selected_staff_id=selected_staff_id,
                           selected_month=selected_month)


# -------------------------
# SALARY OVERVIEW + EXPORTS
# -------------------------
@routes.route('/salary_overview', methods=['GET'])
@login_required
def salary_overview():
    if not (current_user.is_admin or current_user.is_superuser):
        flash("Access denied: Admin users only.", "error")
        return redirect(url_for('routes.dashboard'))

    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)

    query = SalaryRecord.query.join(Staff).filter(SalaryRecord.net_salary > 0)  # Only show completed records
    if month:
        query = query.filter(SalaryRecord.month == month)
    if year:
        query = query.filter(SalaryRecord.year == year)

    records = query.order_by(SalaryRecord.year.desc(), SalaryRecord.month.desc()).all()
    for r in records:
        r.days_in_month = calendar.monthrange(r.year, r.month)[1]

    return render_template(
        'salary_overview.html',
        title='Salary Overview',
        records=records,
        selected_month=month,
        selected_year=year
    )


@routes.route('/export_salary_excel')
@login_required
def export_salary_excel():
    if not current_user.is_accounts and not current_user.is_admin:
        flash("Access denied: Accounts users only.", "error")
        return redirect(url_for('routes.dashboard'))

    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)

    query = SalaryRecord.query.join(Staff).filter(SalaryRecord.net_salary > 0)
    if month:
        query = query.filter(SalaryRecord.month == month)
    if year:
        query = query.filter(SalaryRecord.year == year)

    records = query.all()
    if not records:
        flash("No records to export for the selected period.", "warning")
        return redirect(url_for('routes.salary_overview', month=month, year=year))

    data = []
    for r in records:
        days_in_month = calendar.monthrange(r.year, r.month)[1]
        lop_per_day = (r.gross_salary / days_in_month) if days_in_month else 0
        data.append({
            "Staff ID": r.staff_ref.staff_id,
            "Name": r.staff_ref.name,
            "Department": r.staff_ref.department,
            "Designation": r.staff_ref.designation,
            "Base Pay": r.gross_salary,
            "LOP Days": r.lop_days,
            "LOP/Day": round(lop_per_day, 2),
            "LOP Amount": r.lop_amount,
            "EPF": r.epf,
            "ESI": r.esi,
            "IT": r.it or 0,
            "Loan": r.loan or 0,
            "Advance": r.advance or 0,
            "Uniform": r.uniform or 0,
            "CD": r.cd or 0,
            "Hostel": r.hostel or 0,
            "Misc": r.misc or 0,
            "Total Deductions": r.total_deductions,
            "Reimbursements": r.total_reimbursements,
            "Net Salary": r.net_salary
        })

    df = pd.DataFrame(data)
    output = io.BytesIO()
    df.to_excel(output, index=False, sheet_name="Salary Overview")
    output.seek(0)

    filename = f"Salary_Overview_{year or 'All'}_{month or 'All'}.xlsx"
    return send_file(output, as_attachment=True,
                     download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@routes.route('/export_salary_pdf')
@login_required
def export_salary_pdf():
    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)

    query = SalaryRecord.query.join(Staff).filter(SalaryRecord.net_salary > 0)
    if month:
        query = query.filter(SalaryRecord.month == month)
    if year:
        query = query.filter(SalaryRecord.year == year)

    records = query.all()
    if not records:
        flash("No records found for the selected period.", "error")
        return redirect(url_for('routes.salary_overview'))

    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.add_page()
    pdf.set_font('Arial', 'B', 14)
    pdf.cell(0, 10, f'Salary Overview Report - {month}/{year}', 0, 1, 'C')

    pdf.set_font('Arial', 'B', 9)
    headers = [
        "Staff ID", "Name", "Dept", "Desig", "Base Pay", "LOP Days", "LOP Amt",
        "EPF", "ESI", "IT", "Loan", "Adv", "Uniform", "CD", "Hostel", "Misc", "Net Pay"
    ]
    col_widths = [18, 30, 25, 25, 20, 20, 20, 18, 18, 18, 18, 18, 18, 18, 18, 18, 22]

    for i, h in enumerate(headers):
        pdf.cell(col_widths[i], 8, h, border=1, align='C')
    pdf.ln()

    pdf.set_font('Arial', '', 8)
    for r in records:
        pdf.cell(col_widths[0], 8, str(r.staff_ref.staff_id), border=1)
        pdf.cell(col_widths[1], 8, r.staff_ref.name[:20], border=1)
        pdf.cell(col_widths[2], 8, r.staff_ref.department[:15], border=1)
        pdf.cell(col_widths[3], 8, r.staff_ref.designation[:15], border=1)
        pdf.cell(col_widths[4], 8, f"{r.gross_salary:.0f}", border=1, align='R')
        pdf.cell(col_widths[5], 8, f"{r.lop_days}", border=1, align='C')
        pdf.cell(col_widths[6], 8, f"{r.lop_amount:.0f}", border=1, align='R')
        pdf.cell(col_widths[7], 8, f"{r.epf:.0f}", border=1, align='R')
        pdf.cell(col_widths[8], 8, f"{r.esi:.0f}", border=1, align='R')
        pdf.cell(col_widths[9], 8, f"{r.it:.0f}", border=1, align='R')
        pdf.cell(col_widths[10], 8, f"{r.loan:.0f}", border=1, align='R')
        pdf.cell(col_widths[11], 8, f"{r.advance:.0f}", border=1, align='R')
        pdf.cell(col_widths[12], 8, f"{r.uniform:.0f}", border=1, align='R')
        pdf.cell(col_widths[13], 8, f"{r.cd:.0f}", border=1, align='R')
        pdf.cell(col_widths[14], 8, f"{r.hostel:.0f}", border=1, align='R')
        pdf.cell(col_widths[15], 8, f"{r.misc:.0f}", border=1, align='R')
        pdf.cell(col_widths[16], 8, f"{r.net_salary:.0f}", border=1, align='R')
        pdf.ln()

    pdf_bytes = pdf.output(dest='S').encode('latin-1')
    pdf_output = io.BytesIO(pdf_bytes)

    return send_file(pdf_output, as_attachment=True,
                     download_name=f"Salary_Report_{month}_{year}.pdf",
                     mimetype='application/pdf')


# -------------------------
# FIXER PAGE
# -------------------------
@routes.route('/fixer', methods=['GET', 'POST'])
@login_required
def fixer():
    # Access control
    if not (current_user.is_hr or current_user.is_superuser):
        flash("Access denied: HR only.", "error")
        return redirect(url_for('routes.dashboard'))

    # Fetch dropdown data
    departments = [d[0] for d in db.session.query(Staff.department).distinct()]
    designations = [d[0] for d in db.session.query(Staff.designation).distinct()]
    staff_list = Staff.query.order_by(Staff.name.asc()).all()

    # POST logic
    if request.method == 'POST':
        staff_id = request.form.get('staff_id')
        increment_value = float(request.form.get('increment_value') or 0)
        effective_date = request.form.get('effective_date')

        if not staff_id or increment_value <= 0:
            flash("Please fill all required fields properly.", "error")
            return redirect(url_for('routes.fixer'))

        # Find the staff record
        staff = Staff.query.filter_by(staff_id=staff_id).first()
        if not staff:
            flash("Staff record not found.", "error")
            return redirect(url_for('routes.fixer'))

        # Apply the increment
        old_salary = staff.base_salary
        staff.base_salary = old_salary + increment_value

        # Log increment history
        new_record = IncrementHistory(
            increment_type='Base Pay Increment',
            target=staff.name,
            mode='Manual',
            value=increment_value,
            effective_month=effective_date
        )
        db.session.add(new_record)

        try:
            db.session.commit()
            flash(f"✅ Increment of ₹{increment_value:,.2f} applied successfully to {staff.name} "
                  f"(ID {staff_id}). Base Pay updated from ₹{old_salary:,.2f} → ₹{staff.base_salary:,.2f}.",
                  "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Database error: {e}", "error")

        return redirect(url_for('routes.fixer'))

    # GET logic
    return render_template('fixer.html',
                           title='Salary Fixer',
                           departments=departments,
                           designations=designations,
                           staff_list=staff_list)

# -------------------------
# PROFESSIONAL TAX PAGE
# -------------------------
@routes.route('/professional-tax', methods=['GET', 'POST'])
@login_required
def professional_tax():
    if not (current_user.is_accounts or current_user.is_superuser):
        flash("Access denied: Accounts only.", "error")
        return redirect(url_for('routes.dashboard'))

    if request.method == 'POST':
        range_from = float(request.form['range_from'])
        range_to = float(request.form['range_to'])
        tax_amount = float(request.form['tax_amount'])

        new_slab = ProfessionalTax(range_from=range_from, range_to=range_to, tax_amount=tax_amount)
        db.session.add(new_slab)
        db.session.commit()

        flash(f"New tax slab added for ₹{int(range_from)}–₹{int(range_to)}.", "success")
        return redirect(url_for('routes.professional_tax'))

    tax_slabs = ProfessionalTax.query.order_by(ProfessionalTax.range_from.asc()).all()
    return render_template('professional_tax.html', title='Professional Tax', tax_slabs=tax_slabs)

@routes.route('/professional-tax/edit/<int:tax_id>', methods=['POST'])
@login_required
def edit_tax(tax_id):
    if not (current_user.is_accounts or current_user.is_superuser):
        flash("Access denied: Accounts only.", "error")
        return redirect(url_for('routes.professional_tax'))

    slab = ProfessionalTax.query.get_or_404(tax_id)

    try:
        slab.range_from = float(request.form['range_from'])
        slab.range_to = float(request.form['range_to'])
        slab.tax_amount = float(request.form['tax_amount'])
        db.session.commit()

        flash("Tax slab updated successfully!", "success")
        return redirect(url_for('routes.professional_tax'))

    except Exception as e:
        db.session.rollback()
        flash(f"Error updating slab: {e}", "error")
        return redirect(url_for('routes.professional_tax'))

@routes.route('/professional-tax/delete/<int:tax_id>')
@login_required
def delete_tax(tax_id):
    if not current_user.is_admin and not current_user.is_accounts:
        flash("Access denied: Admins only.", "error")
        return redirect(url_for('routes.dashboard'))

    slab = ProfessionalTax.query.get(tax_id)
    if slab:
        db.session.delete(slab)
        db.session.commit()
        flash("Tax slab deleted successfully.", "success")
    else:
        flash("Tax slab not found.", "error")

    return redirect(url_for('routes.professional_tax'))
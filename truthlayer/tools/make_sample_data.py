"""Generate a deliberately messy Harborview test dataset (same shape as the brief).

Every planted problem is listed in PLANTED so tests can assert the engine catches it.
"""
import csv, os
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

OUT = os.path.join(os.path.dirname(__file__), "..", "sample_data")

HR = [
    # employee_id, first, last, title, facility, phone, license, lic_exp, hire
    ["E201", "Sofia", "Reyes", "Registered Nurse", "Harborview Bayside", "718-555-0201", "RN-551203", "2027-05-31", "2020-03-02"],
    ["E202", "Marcus", "Bell", "Certified Nursing Assistant", "Harborview Riverdale", "347-555-0202", "CNA-771045", "2026-12-31", "2022-09-12"],
    ["E203", "Priya", "Natarajan", "RN", "Harborview Riverdale", "(718) 555-0203", "RN-552310", "2026-11-15", "2019-06-17"],
    ["E204", "James", "O'Connor", "Licensed Practical Nurse", "Bayside", "718.555.0204", "LPN-330872", "2027-08-31", "2021-01-11"],
    ["E205", "Aisha", "Mohammed", "Certified Nursing Asst.", "Harborview Bayside", "347-555-0205", "CNA-771200", "2026-09-30", "4/3/2021"],
    ["E206", "Daniel", "Kim", "Registered Nurse", "HV Riverdale", "718-555-0206", "RN-553901", "2028-01-31", "2023-05-08"],
    ["E207", "Grace", "Thompson", "Licensed Practical Nurse", "Harborview Riverdale", "347-555-0207", "LPN-331455", "2027-03-31", "2018-10-01"],
    ["E208", "Luis", "Hernandez", "Certified Nursing Assistant", "Harborview Bayside", "718-555-0208", "CNA-771388", "2027-07-31", "2024-04-15"],
    ["E209", "Emily", "Chen", "Registered Nurse", "Harborview Bayside", "347-555-0209", "RN-554022", "2027-10-31", "2022-02-14"],
    ["E210", "Robert", "Johnson", "Administrator", "Harborview Bayside", "718-555-0210", "", "", "2024-02-30"],
    ["E211", "Kate", "Murphy", "CNA", "Harborview Riverdale", "347-555-0211", "CNA-771502", "2027-04-30", "2023-08-21"],
    ["E212", "Katherine", "Murphy", "Certified Nursing Assistant", "Harborview Riverdale", "347-555-0211", "CNA-771502", "2027-04-30", "2023-08-21"],
]

W1 = ("2026-09-07", "2026-09-13")
W2 = ("2026-09-14", "2026-09-20")
PAYROLL = [
    # name, job_code, facility_code, week, hours
    ("REYES, SOFIA", "RN", "BYS", W1, 36), ("REYES, SOFIA", "RN", "BYS", W2, 36),
    ("BELL, MARCUS", "CNA", "RVD", W1, 40), ("BELL, MARCUS", "CNA", "RVD", W2, 40),
    ("BELL, MARCUS", "CNA", "RVD", W2, 40),                                   # duplicate payment
    ("NATARAJAN, PRIYA", "RN", "RVD", W1, 36), ("NATARAJAN, PRIYA", "RN", "RVD", W2, 44),  # paid 44, scheduled 36
    ("O'CONNOR, JAMES", "LPN", "BYS", W1, 40), ("OCONNOR, JAMES", "LPN", "BYS", W2, 40),
    ("MOHAMMED, AISHA", "CNA", "BYS", W1, 40), ("MOHAMMED, AISHA", "CNA", "BYS", W2, 40),
    ("KIM, DANIEL", "RN", "BYS", W1, 88), ("KIM, DANIEL", "RN", "BYS", W2, 36),        # facility conflict + 88h
    ("THOMPSON, GRACE", "RN", "RVD", W1, 32), ("THOMPSON, GRACE", "RN", "RVD", W2, 32), # job code RN vs LPN
    ("HERNANDEZ, LUIS", "CNA", "BYS", W1, 32),                                         # no W2 pay but scheduled
    ("CHEN, EMILY", "RN", "BYS", W1, 24), ("CHEN, EMILY", "RN", "BYS", W2, 28),
    ("JOHNSON, ROBERT", "ADM", "BYS", W1, 40), ("JOHNSON, ROBERT", "ADM", "BYS", W2, 40),
    ("MURPHY, KATHERINE", "CNA", "RVD", W1, 24), ("MURPHY, KATHERINE", "CNA", "RVD", W2, 24),
    ("DIAZ, CARMEN", "CNA", "RVD", W2, 16),                                            # not in HR
    ("RAYES, SOFIA", "RN", "BYS", ("2026-09-21", "2026-09-27"), 24),                   # typo -> fuzzy match
]

LIC = [
    ["RN-551203", "REYES, SOFIA", "RN", "2027-05-31", "2026-09-01"],
    ["CNA-771045", "BELL, MARCUS", "CNA", "2026-12-31", "2026-09-01"],
    ["RN 552310", "NATARAJAN, PRIYA", "RN", "11/15/2026", "2026-09-01"],
    ["LPN-330872", "O'CONNOR, JAMES", "LPN", "2027-02-28", "2026-08-15"],      # HR says 2027-08-31
    ["CNA-771200", "MOHAMMED, AISHA", "CNA", "2026-09-15", "2026-09-02"],      # expired mid-week; HR says 09-30
    ["RN-553901", "KIM, DANIEL", "RN", "2028-01-31", "2026-09-01"],
    ["LPN-331455", "WALSH, GRACE", "LPN", "2027-03-31", "2026-09-01"],         # name differs (married name?)
    ["RN-554022", "CHEN, EMILY", "RN", "2027-10-31", "2025-06-10"],            # stale verification
    ["CNA-771502", "MURPHY, KATHERINE", "CNA", "2027-04-30", "2026-09-01"],
    ["CNA-779999", "DIAZ, CARMEN", "CNA", "2027-06-30", "2026-09-01"],         # not linked to HR
]

DAYS = ["Mon 09/14", "Tue 09/15", "Wed 09/16", "Thu 09/17", "Fri 09/18", "Sat 09/19", "Sun 09/20"]
SCHED = {
    "Harborview Bayside": [
        ["Sofia Reyes", "RN", "7a-3p", "7a-3p", "OFF", "7a-7p", "OFF", "7a-3p", "OFF"],
        ["Emily Chen", "RN", "7a-3p", "OFF", "7a-3p", "OFF", "7a-3p", "OFF", "7a-11a"],
        ["Daniel Kim", "RN", "OFF", "7a-7p", "OFF", "7a-7p", "OFF", "7a-7p", "OFF"],
        ["James O'Connor", "LPN", "7a-3p", "7a-3p", "7a-3p", "7a-3p", "7a-3p", "OFF", "OFF"],
        ["Aisha Mohammed", "CNA", "3p-11p", "3p-11p", "3p-11p", "3p-11p", "3p-11p", "OFF", "OFF"],
        ["Luis Hernandez", "CNA", "11p-7a", "11p-7a", "OFF", "OFF", "11p-7a", "11p-7a", "OFF"],
    ],
    "Harborview Riverdale": [
        ["Priya Natarajan", "RN", "7a-7p", "OFF", "7a-7p", "OFF", "OFF", "7a-7p", "OFF"],
        ["Grace Thompson", "LPN", "3p-11p", "3p-11p", "OFF", "3p-11p", "3p-11p", "OFF", "OFF"],
        ["Marc Bell", "CNA", "3p-11p", "OFF", "3p-11p", "3p-11p", "3p-11p", "3p-11p", "OFF"],
        ["Kate Murphy", "CNA", "7a-3p", "OFF", "7a-3p", "OFF", "7a-3p", "OFF", "OFF"],
        ["Carmen Diaz", "CNA", "OFF", "OFF", "OFF", "OFF", "OFF", "3p-11p", "3p-11p"],
    ],
}

def main():
    os.makedirs(OUT, exist_ok=True)
    with open(f"{OUT}/hr_roster.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["employee_id", "first_name", "last_name", "job_title", "facility", "phone", "license_number", "license_expiration", "hire_date"])
        w.writerows(HR)
    with open(f"{OUT}/payroll.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["payroll_id", "employee_name", "job_code", "facility_code", "period_start", "period_end", "hours_paid"])
        for i, (n, j, fc, wk, h) in enumerate(PAYROLL):
            w.writerow([f"P-{3001 + i}", n, j, fc, wk[0], wk[1], h])
    with open(f"{OUT}/licenses.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["license_number", "name_on_license", "license_type", "expiration_date", "last_verified"])
        w.writerows(LIC)

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(f"{OUT}/staff_schedule.pdf", pagesize=landscape(letter))
    story = []
    for i, (fac, rows) in enumerate(SCHED.items()):
        story.append(Paragraph(f"{fac} — Weekly Staff Schedule", styles["Title"]))
        story.append(Paragraph("Week of 09/14 – 09/20", styles["Normal"]))
        story.append(Spacer(1, 12))
        t = Table([["Staff", "Role"] + DAYS] + rows)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                               ("FONTSIZE", (0, 0), (-1, -1), 9)]))
        story.append(t)
        story.append(Spacer(1, 18))
        story.append(Paragraph("Shifts: 7a-3p, 3p-11p, 11p-7a are 8 hours. 7a-7p is 12 hours.", styles["Normal"]))
        if i == 0:
            story.append(PageBreak())
    doc.build(story)
    print("wrote", os.listdir(OUT))

if __name__ == "__main__":
    main()

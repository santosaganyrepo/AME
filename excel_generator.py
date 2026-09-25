
from pathlib import Path
import json
from datetime import datetime

# Try importing both libraries
try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    XLSX_AVAILABLE = True
except ImportError:
    XLSX_AVAILABLE = False
    print("⚠️ openpyxl not installed. Install with: pip install openpyxl")

try:
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableRow, TableCell
    from odf.text import P
    from odf.style import Style, TextProperties, TableColumnProperties, TableCellProperties
    from odf.number import NumberStyle, Number
    ODS_AVAILABLE = True
except ImportError:
    ODS_AVAILABLE = False
    print("⚠️ odfpy not installed. Install with: pip install odfpy")


class ExcelGenerator:
    """Generates Excel spreadsheets from student data"""
    
    def __init__(self, base_upload_folder: Path):
        self.base_upload_folder = base_upload_folder
    
    def find_metadata_file(self, year: str, term: str, class_name: str, stream: str, subject: str, exam_type: str) -> Path:
        """
        Finds the students_metadata.json file based on exam session parameters.
        Folder structure (set by app.py): UPLOAD_FOLDER/year/term/class_stream/subject/exam_type
        """
        exam_path = self.base_upload_folder / year / term / f"{class_name}_{stream}" / subject / exam_type
        metadata_file = exam_path / "students_metadata.json"

        if not metadata_file.exists():
            raise FileNotFoundError(
                f"No data found for this exam session.\n"
                f"Looked in: {exam_path}\n"
                f"Please verify year, term, class, stream, subject and exam type are correct."
            )

        return metadata_file
    
    def load_student_data(self, metadata_file: Path) -> list:
        """
        Loads and formats student data from JSON.
        Handles the {"exam_info": {}, "students": [...]} structure used by app.py.
        Returns a sorted list of student records.
        """
        with open(metadata_file, 'r', encoding='utf-8') as f:
            raw = json.load(f)

        # Support both new list format {"students": [...]} and old dict format
        students_raw = raw.get("students", raw)
        if isinstance(students_raw, dict):
            students_raw = list(students_raw.values())

        students_list = []
        for student in students_raw:
            if not isinstance(student, dict):
                continue
            overridden = bool(student.get('overridden'))
            students_list.append({
                'id':       student.get('student_id') or student.get('id', 'Unknown'),
                'name':     student.get('student_name', 'Unknown'),
                'pages':    student.get('total_pages', 0),
                # 'status' here reflects marking status (Marked / Not Marked).
                # Whether a mark was manually overridden is tracked
                # separately below so it can be shown as its own column
                # without overloading the marking-status column.
                'status':   student.get('status', 'Not Marked'),
                # total_score is the single source of truth used everywhere
                # (session stats, student report, this spreadsheet) — a
                # manual override already lives in this field, so exporting
                # it here automatically reflects any teacher correction.
                'total_score': float(student.get('total_score', 0.0)),
                'feedback': student.get('overall_feedback', 'No feedback available'),
                'overridden': overridden,
                'ai_score': float(student.get('ai_score')) if overridden and student.get('ai_score') is not None else None,
            })

        students_list.sort(key=lambda x: str(x['id']))
        return students_list
    
    # Column definitions: key -> (header label, default width). 'pages' is
    # always included since it's not one of the user-toggleable checkboxes
    # in the UI (id/name/score/feedback/status) but stays useful context.
    COLUMN_DEFS = [
        ('id',       'Student ID',      15),
        ('name',     'Student Name',    25),
        ('pages',    'Pages',           10),
        ('status',   'Status',          15),
        ('score',    'Total Score (%)', 16),
        ('feedback', 'AI Feedback',     60),
    ]

    @staticmethod
    def _resolve_columns(columns: dict = None) -> list:
        """
        Returns the ordered list of column keys to include. `columns` is the
        {id, name, score, feedback, status} boolean dict sent by the
        "Columns to Include" checklist in the Generate Spreadsheet modal.
        'pages' has no matching checkbox so it's always shown. If `columns`
        is omitted entirely (e.g. an older client), every column is shown —
        this keeps the export working exactly as before for callers that
        don't yet send a selection.
        """
        if not columns:
            return [key for key, _, _ in ExcelGenerator.COLUMN_DEFS]
        selected = ['pages']
        for key, _, _ in ExcelGenerator.COLUMN_DEFS:
            if key == 'pages':
                continue
            if columns.get(key, True):
                selected.append(key)
        # Preserve the canonical left-to-right order regardless of dict order.
        order = [k for k, _, _ in ExcelGenerator.COLUMN_DEFS]
        return [k for k in order if k in selected]

    def generate_xlsx(self, students: list, exam_info: dict, output_path: Path,
                      columns: dict = None) -> bool:
        """
        Generates XLSX file using openpyxl. `columns` filters which columns
        are written — see `_resolve_columns`. A manually-overridden mark is
        shown with an "(edited)" suffix and a small note appended to that
        student's AI Feedback cell, so the override is visible even when
        the Status column isn't included.
        """
        if not XLSX_AVAILABLE:
            raise ImportError("openpyxl is not installed. Install with: pip install openpyxl")

        col_keys = self._resolve_columns(columns)
        col_map  = {k: (label, width) for k, label, width in self.COLUMN_DEFS}
        ncols    = len(col_keys)

        wb = Workbook()
        ws = wb.active
        ws.title = "Student Marks"

        # Title row
        last_col_letter = ws.cell(row=1, column=max(ncols, 1)).column_letter
        ws.merge_cells(f'A1:{last_col_letter}1')
        title_cell = ws['A1']
        title_cell.value = f"EXAM RESULTS - {exam_info['subject']} ({exam_info['exam_type']})"
        title_cell.font = Font(size=16, bold=True, color="FFFFFF")
        title_cell.fill = PatternFill(start_color="667eea", end_color="667eea", fill_type="solid")
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 30

        # Exam info rows
        ws['A2'] = "Year:"
        ws['B2'] = exam_info['year']
        ws['D2'] = "Term:"
        ws['E2'] = exam_info['term']

        ws['A3'] = "Class:"
        ws['B3'] = f"{exam_info['class']} {exam_info['stream']}"
        ws['D3'] = "Subject:"
        ws['E3'] = exam_info['subject']

        ws['A4'] = "Generated:"
        ws['B4'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for row in [2, 3, 4]:
            ws[f'A{row}'].font = Font(bold=True)
            ws[f'D{row}'].font = Font(bold=True)

        # Header row
        header_row = 6
        for col, key in enumerate(col_keys, start=1):
            label, width = col_map[key]
            cell = ws.cell(row=header_row, column=col)
            cell.value = label
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(start_color="764ba2", end_color="764ba2", fill_type="solid")
            cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.column_dimensions[cell.column_letter].width = width

        # Data rows
        for r_idx, student in enumerate(students, start=header_row + 1):
            for c_idx, key in enumerate(col_keys, start=1):
                cell = ws.cell(row=r_idx, column=c_idx)

                if key == 'id':
                    cell.value = student['id']

                elif key == 'name':
                    cell.value = student['name']

                elif key == 'pages':
                    cell.value = student['pages']

                elif key == 'status':
                    cell.value = student['status']
                    if student['status'] == 'Marked':
                        cell.fill = PatternFill(start_color="d4edda", end_color="d4edda", fill_type="solid")
                        cell.font = Font(color="155724", bold=True)

                elif key == 'score':
                    score = student['total_score']
                    cell.value = score
                    cell.number_format = '0.0'
                    if score >= 75:
                        cell.fill = PatternFill(start_color="d4edda", end_color="d4edda", fill_type="solid")
                        cell.font = Font(color="28a745", bold=True)
                    elif score >= 50:
                        cell.fill = PatternFill(start_color="fff3cd", end_color="fff3cd", fill_type="solid")
                        cell.font = Font(color="856404", bold=True)
                    else:
                        cell.fill = PatternFill(start_color="f8d7da", end_color="f8d7da", fill_type="solid")
                        cell.font = Font(color="721c24", bold=True)
                    if student.get('overridden'):
                        cell.value = f"{score} (edited)"
                        cell.font = Font(color=cell.font.color.rgb if cell.font.color else "000000",
                                         bold=True, italic=True)

                elif key == 'feedback':
                    fb = student['feedback']
                    if student.get('overridden') and student.get('ai_score') is not None:
                        fb = f"[Manually overridden — original AI mark: {student['ai_score']}%] {fb}"
                    cell.value = fb

        # Borders
        thin_border = Border(
            left=Side(style='thin'), right=Side(style='thin'),
            top=Side(style='thin'), bottom=Side(style='thin')
        )
        for row in ws.iter_rows(min_row=header_row, max_row=header_row + len(students),
                                min_col=1, max_col=max(ncols, 1)):
            for cell in row:
                cell.border = thin_border

        wb.save(output_path)
        return True
    
    def generate_ods(self, students: list, exam_info: dict, output_path: Path,
                     columns: dict = None) -> bool:
        """
        Generates ODS file using odfpy. `columns` filters which columns are
        written, same convention as `generate_xlsx` — see `_resolve_columns`.
        """
        if not ODS_AVAILABLE:
            raise ImportError("odfpy is not installed. Install with: pip install odfpy")

        col_keys = self._resolve_columns(columns)
        col_map  = {k: label for k, label, _ in self.COLUMN_DEFS}

        doc = OpenDocumentSpreadsheet()
        table = Table(name="Student Marks")

        # Title row
        title_row = TableRow()
        title_cell = TableCell(valuetype="string")
        title_cell.addElement(P(text=f"EXAM RESULTS - {exam_info['subject']} ({exam_info['exam_type']})"))
        title_row.addElement(title_cell)
        table.addElement(title_row)

        # Info rows
        info_rows = [
            ["Year:", exam_info['year'], "", "Term:", exam_info['term']],
            ["Class:", f"{exam_info['class']} {exam_info['stream']}", "", "Subject:", exam_info['subject']],
            ["Generated:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "", "", ""]
        ]
        for info_data in info_rows:
            row = TableRow()
            for value in info_data:
                cell = TableCell(valuetype="string")
                cell.addElement(P(text=str(value)))
                row.addElement(cell)
            table.addElement(row)

        # Empty row
        table.addElement(TableRow())

        # Header row
        header_row = TableRow()
        for key in col_keys:
            cell = TableCell(valuetype="string")
            cell.addElement(P(text=col_map[key]))
            header_row.addElement(cell)
        table.addElement(header_row)

        # Data rows
        for student in students:
            row = TableRow()
            for key in col_keys:
                if key == 'id':
                    cell = TableCell(valuetype="string")
                    cell.addElement(P(text=str(student['id'])))

                elif key == 'name':
                    cell = TableCell(valuetype="string")
                    cell.addElement(P(text=student['name']))

                elif key == 'pages':
                    cell = TableCell(valuetype="float", value=student['pages'])
                    cell.addElement(P(text=str(student['pages'])))

                elif key == 'status':
                    cell = TableCell(valuetype="string")
                    cell.addElement(P(text=student['status']))

                elif key == 'score':
                    score = student['total_score']
                    cell = TableCell(valuetype="float", value=score)
                    label = f"{score} (edited)" if student.get('overridden') else str(score)
                    cell.addElement(P(text=label))

                elif key == 'feedback':
                    fb = student['feedback']
                    if student.get('overridden') and student.get('ai_score') is not None:
                        fb = f"[Manually overridden — original AI mark: {student['ai_score']}%] {fb}"
                    cell = TableCell(valuetype="string")
                    cell.addElement(P(text=fb))

                else:
                    cell = TableCell(valuetype="string")

                row.addElement(cell)
            table.addElement(row)

        doc.spreadsheet.addElement(table)
        doc.save(output_path)
        return True
    
    def generate_spreadsheet(self, year: str, term: str, class_name: str, stream: str, 
                           subject: str, exam_type: str, file_format: str, output_folder: Path,
                           columns: dict = None) -> Path:
        """
        Main method to generate spreadsheet.
        
        Args:
            year, term, class_name, stream, subject, exam_type: Exam session parameters
            file_format: 'xlsx' or 'ods'
            output_folder: Where to save the file
            columns: optional {id, name, score, feedback, status} booleans from
                     the "Columns to Include" checklist — filters which columns
                     are written. Omit/None to include every column.
        
        Returns:
            Path to generated file
        """
        # Find metadata
        metadata_file = self.find_metadata_file(year, term, class_name, stream, subject, exam_type)
        
        # Load student data
        students = self.load_student_data(metadata_file)
        
        if len(students) == 0:
            raise ValueError("No students found in this exam session")
        
        # Prepare exam info
        exam_info = {
            'year': year,
            'term': term,
            'class': class_name,
            'stream': stream,
            'subject': subject,
            'exam_type': exam_type
        }
        
        # Generate filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{subject}_{class_name}{stream}_{exam_type}_{timestamp}.{file_format}"
        output_path = output_folder / filename
        
        # Generate based on format
        if file_format.lower() == 'xlsx':
            self.generate_xlsx(students, exam_info, output_path, columns=columns)
        elif file_format.lower() == 'ods':
            self.generate_ods(students, exam_info, output_path, columns=columns)
        else:
            raise ValueError(f"Unsupported format: {file_format}. Use 'xlsx' or 'ods'")
        
        return output_path


# Utility function for Flask integration
def generate_exam_spreadsheet(base_folder: Path, year: str, term: str, class_name: str, 
                              stream: str, subject: str, exam_type: str, file_format: str,
                              output_folder: Path, columns: dict = None) -> Path:
    """
    Wrapper function for easy Flask integration.
    """
    generator = ExcelGenerator(base_folder)
    return generator.generate_spreadsheet(year, term, class_name, stream, subject, exam_type,
                                          file_format, output_folder, columns=columns)

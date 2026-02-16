"""
Excel Generator Module
Generates XLSX and ODS spreadsheets from students_metadata.json
"""

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
        """
        exam_path = self.base_upload_folder / year / term / class_name / stream / subject / exam_type
        metadata_file = exam_path / "students_metadata.json"
        
        if not metadata_file.exists():
            raise FileNotFoundError(f"No data found for this exam session: {exam_path}")
        
        return metadata_file
    
    def load_student_data(self, metadata_file: Path) -> list:
        """
        Loads and formats student data from JSON.
        Returns list of student records.
        """
        with open(metadata_file, 'r') as f:
            students_dict = json.load(f)
        
        # Convert to list and sort by student_id
        students_list = []
        for student_id, student_data in students_dict.items():
            students_list.append({
                'id': student_data.get('student_id', student_id),
                'name': student_data.get('student_name', 'Unknown'),
                'pages': student_data.get('total_pages', 0),
                'status': student_data.get('status', 'Not Marked'),
                'total_score': student_data.get('total_score', 0.0),
                'feedback': student_data.get('overall_feedback', 'No feedback available')
            })
        
        # Sort by ID
        students_list.sort(key=lambda x: str(x['id']))
        
        return students_list
    
    def generate_xlsx(self, students: list, exam_info: dict, output_path: Path) -> bool:
        """
        Generates XLSX file using openpyxl.
        """
        if not XLSX_AVAILABLE:
            raise ImportError("openpyxl is not installed. Install with: pip install openpyxl")
        
        wb = Workbook()
        ws = wb.active
        ws.title = "Student Marks"
        
        # Title row
        ws.merge_cells('A1:F1')
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
        
        # Make info rows bold
        for row in [2, 3, 4]:
            ws[f'A{row}'].font = Font(bold=True)
            ws[f'D{row}'].font = Font(bold=True)
        
        # Header row
        headers = ['Student ID', 'Student Name', 'Pages', 'Status', 'Total Score (%)', 'AI Feedback']
        header_row = 6
        
        for col, header in enumerate(headers, start=1):
            cell = ws.cell(row=header_row, column=col)
            cell.value = header
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(start_color="764ba2", end_color="764ba2", fill_type="solid")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        
        # Data rows
        for idx, student in enumerate(students, start=header_row + 1):
            ws.cell(row=idx, column=1, value=student['id'])
            ws.cell(row=idx, column=2, value=student['name'])
            ws.cell(row=idx, column=3, value=student['pages'])
            
            status_cell = ws.cell(row=idx, column=4, value=student['status'])
            if student['status'] == 'Marked':
                status_cell.fill = PatternFill(start_color="d4edda", end_color="d4edda", fill_type="solid")
                status_cell.font = Font(color="155724", bold=True)
            
            score_cell = ws.cell(row=idx, column=5, value=student['total_score'])
            score_cell.number_format = '0.0'
            
            # Color code scores
            score = student['total_score']
            if score >= 75:
                score_cell.fill = PatternFill(start_color="d4edda", end_color="d4edda", fill_type="solid")
                score_cell.font = Font(color="28a745", bold=True)
            elif score >= 50:
                score_cell.fill = PatternFill(start_color="fff3cd", end_color="fff3cd", fill_type="solid")
                score_cell.font = Font(color="856404", bold=True)
            else:
                score_cell.fill = PatternFill(start_color="f8d7da", end_color="f8d7da", fill_type="solid")
                score_cell.font = Font(color="721c24", bold=True)
            
            ws.cell(row=idx, column=6, value=student['feedback'])
        
        # Adjust column widths
        ws.column_dimensions['A'].width = 15
        ws.column_dimensions['B'].width = 25
        ws.column_dimensions['C'].width = 10
        ws.column_dimensions['D'].width = 15
        ws.column_dimensions['E'].width = 15
        ws.column_dimensions['F'].width = 60
        
        # Add borders
        thin_border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        
        for row in ws.iter_rows(min_row=header_row, max_row=header_row + len(students), 
                                min_col=1, max_col=6):
            for cell in row:
                cell.border = thin_border
        
        # Save
        wb.save(output_path)
        return True
    
    def generate_ods(self, students: list, exam_info: dict, output_path: Path) -> bool:
        """
        Generates ODS file using odfpy.
        """
        if not ODS_AVAILABLE:
            raise ImportError("odfpy is not installed. Install with: pip install odfpy")
        
        doc = OpenDocumentSpreadsheet()
        
        # Create table
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
        headers = ['Student ID', 'Student Name', 'Pages', 'Status', 'Total Score (%)', 'AI Feedback']
        header_row = TableRow()
        for header in headers:
            cell = TableCell(valuetype="string")
            cell.addElement(P(text=header))
            header_row.addElement(cell)
        table.addElement(header_row)
        
        # Data rows
        for student in students:
            row = TableRow()
            
            # ID
            cell = TableCell(valuetype="string")
            cell.addElement(P(text=str(student['id'])))
            row.addElement(cell)
            
            # Name
            cell = TableCell(valuetype="string")
            cell.addElement(P(text=student['name']))
            row.addElement(cell)
            
            # Pages
            cell = TableCell(valuetype="float", value=student['pages'])
            cell.addElement(P(text=str(student['pages'])))
            row.addElement(cell)
            
            # Status
            cell = TableCell(valuetype="string")
            cell.addElement(P(text=student['status']))
            row.addElement(cell)
            
            # Score
            cell = TableCell(valuetype="float", value=student['total_score'])
            cell.addElement(P(text=str(student['total_score'])))
            row.addElement(cell)
            
            # Feedback
            cell = TableCell(valuetype="string")
            cell.addElement(P(text=student['feedback']))
            row.addElement(cell)
            
            table.addElement(row)
        
        doc.spreadsheet.addElement(table)
        doc.save(output_path)
        return True
    
    def generate_spreadsheet(self, year: str, term: str, class_name: str, stream: str, 
                           subject: str, exam_type: str, file_format: str, output_folder: Path) -> Path:
        """
        Main method to generate spreadsheet.
        
        Args:
            year, term, class_name, stream, subject, exam_type: Exam session parameters
            file_format: 'xlsx' or 'ods'
            output_folder: Where to save the file
        
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
            self.generate_xlsx(students, exam_info, output_path)
        elif file_format.lower() == 'ods':
            self.generate_ods(students, exam_info, output_path)
        else:
            raise ValueError(f"Unsupported format: {file_format}. Use 'xlsx' or 'ods'")
        
        return output_path


# Utility function for Flask integration
def generate_exam_spreadsheet(base_folder: Path, year: str, term: str, class_name: str, 
                              stream: str, subject: str, exam_type: str, file_format: str,
                              output_folder: Path) -> Path:
    """
    Wrapper function for easy Flask integration.
    """
    generator = ExcelGenerator(base_folder)
    return generator.generate_spreadsheet(year, term, class_name, stream, subject, exam_type, file_format, output_folder)
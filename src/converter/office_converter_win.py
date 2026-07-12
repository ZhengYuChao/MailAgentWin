"""
Office 文档转换器 (Windows 版)
使用本机 Office COM 把 docx/xlsx/pptx 转 PDF。
要求: 本机已装 Microsoft Office (Word/Excel/PowerPoint)。
"""
import os
import logging
import pythoncom
import win32com.client
from pathlib import Path
from typing import Optional, List

log = logging.getLogger(__name__)

WD_FORMAT_PDF = 17
XL_TYPE_PDF = 0
PP_FORMAT_PDF = 32

def convert_to_pdf(src_path: str, output_dir: str) -> Optional[str]:
    """将 docx/pptx/xlsx 转换为 PDF"""
    src_path = os.path.abspath(src_path)
    ext = os.path.splitext(src_path)[1].lower()
    dst_path = os.path.join(output_dir, os.path.splitext(os.path.basename(src_path))[0] + ".pdf")
    
    pythoncom.CoInitialize()
    try:
        if ext in (".doc", ".docx"):
            return _word_to_pdf(src_path, dst_path)
        if ext in (".xls", ".xlsx"):
            return _excel_to_pdf(src_path, dst_path)
        if ext in (".ppt", ".pptx"):
            return _ppt_to_pdf(src_path, dst_path)
        return None
    except Exception as e:
        log.warning(f"Office to PDF conversion failed for {src_path}: {e}")
        return None
    finally:
        pythoncom.CoUninitialize()

def _word_to_pdf(src, dst):
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0  # wdAlertsNone
    word.ScreenUpdating = False
    try:
        doc = word.Documents.Open(src, ReadOnly=True, Visible=False)
        doc.SaveAs(dst, FileFormat=WD_FORMAT_PDF)
        doc.Close(SaveChanges=False)
        return dst
    finally:
        word.Quit()

def _excel_to_pdf(src, dst):
    xl = win32com.client.DispatchEx("Excel.Application")
    xl.Visible = False
    xl.DisplayAlerts = False
    xl.ScreenUpdating = False
    try:
        wb = xl.Workbooks.Open(src, ReadOnly=True)
        wb.ExportAsFixedFormat(XL_TYPE_PDF, dst)
        wb.Close(SaveChanges=False)
        return dst
    finally:
        xl.Quit()

def _ppt_to_pdf(src, dst):
    pp = win32com.client.DispatchEx("PowerPoint.Application")
    pp.DisplayAlerts = 7  # ppAlertsNone
    try:
        # PPT 不支持完全隐藏，必须用 WithWindow=False
        deck = pp.Presentations.Open(src, ReadOnly=True, WithWindow=False)
        deck.SaveAs(dst, PP_FORMAT_PDF)
        deck.Close()
        return dst
    finally:
        pp.Quit()

def convert_office_attachment(input_path: str, output_dir: str) -> List[str]:
    """统一入口"""
    pdf_path = convert_to_pdf(input_path, output_dir)
    return [pdf_path] if pdf_path else []

def is_convertible(filename: str) -> bool:
    """判断文件是否支持转换"""
    ext = Path(filename).suffix.lower()
    return ext in ('.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt')

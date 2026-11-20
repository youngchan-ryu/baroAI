"""Bounded, untrusted school catalog workbook parsing; never logs workbook contents."""

import math
import posixpath
import re
from datetime import date, datetime
from io import BytesIO
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment

# The travel sheet can contain 1,000,000 rows plus a header. Bounds also leave
# room for catalog rows and shared-string/style XML in a legitimate workbook.
MAX_UPLOAD_BYTES = 32_000_000
MAX_EXPANDED_BYTES = 512_000_000
MAX_TOTAL_ROWS = 1_010_000
MAX_TOTAL_CELLS = 3_100_000
MAX_CELL_TEXT = 2000
MAX_SHARED_STRINGS_BYTES = 16_000_000
MAX_STYLES_BYTES = 4_000_000
MAX_MANIFEST_BYTES = 1_000_000
MAX_METADATA_BYTES = 16_000_000
SHEETS = {
    "departments": ("code", "name"),
    "fields": ("code", "name"),
    "categories": ("code", "name", "field_code"),
    "locations": (
        "code",
        "name",
        "building_code",
        "floor",
        "space_code",
        "region",
        "latitude",
        "longitude",
        "aliases",
        "coordinate_system",
    ),
    "mappings": ("category_code", "location_code", "department_code"),
    "dataset": (
        "dataset_id",
        "version",
        "source",
        "source_version",
        "confirmation_reference",
        "confirmed_by",
        "confirmed_at",
    ),
    "travel_times": ("from_location_code", "to_location_code", "minutes"),
}


def template_bytes():
    book = Workbook()
    book.remove(book.active)
    for name, columns in SHEETS.items():
        sheet = book.create_sheet(name)
        sheet.append(columns)
        sheet.freeze_panes = "A2"
        sheet.cell(1, 1).comment = Comment(
            "코드는 영문 대문자·숫자와 : _ - 를 사용해 30자 이내로 입력해 주세요. "
            "다른 시트의 코드 참조는 등록된 코드와 일치해야 해요. "
            "UNKNOWN은 예약되어 있어요. 별칭은 줄바꿈으로 구분해요. "
            "좌표계는 확인된 경우에만 WGS84로 입력해요. "
            "이동시간을 작성한다면 모든 위치 쌍을 적고 같은 위치는 0으로 입력해요.",
            "바로AI",
        )
    result = BytesIO()
    book.save(result)
    book.close()
    return result.getvalue()


def read_workbook(content: bytes):
    """Return rows with source row numbers, or raise ValueError for invalid format."""
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("invalid_file_size")
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if (
                sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES
                or len(entries) > 2000
                or any(item.flag_bits & 1 for item in entries)
            ):
                raise ValueError("invalid_archive_size")
            names = [item.filename.lower() for item in entries]
            for entry in entries:
                name = entry.filename.lower()
                if name == "[content_types].xml":
                    limit = MAX_MANIFEST_BYTES
                elif name == "xl/styles.xml":
                    limit = MAX_STYLES_BYTES
                elif re.fullmatch(r"xl/worksheets/sheet[0-9]+\.xml", name):
                    limit = MAX_EXPANDED_BYTES
                else:
                    limit = MAX_METADATA_BYTES
                if entry.file_size > limit:
                    raise ValueError("invalid_archive_size")
                if (
                    name == "xl/sharedstrings.xml" and entry.file_size > MAX_SHARED_STRINGS_BYTES
                ) or (name == "xl/styles.xml" and entry.file_size > MAX_STYLES_BYTES):
                    raise ValueError("invalid_archive_size")
            if len(names) != len(set(names)) or any(
                name.startswith("xl/externallinks/")
                or "vbaproject" in name
                or name.endswith(".bin")
                for name in names
            ):
                raise ValueError("unsupported_workbook_content")
            manifest = fromstring(archive.read("[Content_Types].xml"))
            shared_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
            )
            workbook_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
            )
            for node in manifest:
                content_type = node.get("ContentType", "")
                part = node.get("PartName")
                if content_type in {
                    "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
                    "application/vnd.ms-excel.template.macroEnabled.main+xml",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
                }:
                    raise ValueError("unsupported_workbook_content")
                if content_type == shared_type and part != "/xl/sharedStrings.xml":
                    raise ValueError("unsupported_workbook_content")
                if content_type == workbook_type and part not in {None, "/xl/workbook.xml"}:
                    raise ValueError("unsupported_workbook_content")
            workbook_relationships = None
            for entry in entries:
                if entry.filename.lower().endswith(".rels"):
                    if entry.file_size > 1_000_000:
                        raise ValueError("invalid_archive_size")
                    root = fromstring(archive.read(entry))
                    if any(node.get("TargetMode", "").lower() == "external" for node in root):
                        raise ValueError("unsupported_workbook_content")
                    if entry.filename == "xl/_rels/workbook.xml.rels":
                        workbook_relationships = root
            if workbook_relationships is None:
                raise ValueError("unsupported_workbook_content")
            worksheet_type = (
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
            )
            relationships = {}
            for node in workbook_relationships:
                relation_id = node.get("Id")
                if not relation_id or relation_id in relationships:
                    raise ValueError("unsupported_workbook_content")
                if "chartsheet" in node.get("Type", "").lower():
                    raise ValueError("unsupported_workbook_content")
                relationships[relation_id] = node
            workbook_root = fromstring(archive.read("xl/workbook.xml"))
            sheet_tag = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"
            relation_attr = (
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            for node in workbook_root.iter():
                if node.tag.split("}")[-1] == "sheet" and node.tag != sheet_tag:
                    raise ValueError("unsupported_workbook_content")
            for sheet in workbook_root.iter(sheet_tag):
                relation = relationships.get(sheet.get(relation_attr))
                if relation is None or relation.get("Type") != worksheet_type:
                    raise ValueError("unsupported_workbook_content")
                target = relation.get("Target", "")
                if "%" in target or "?" in target or "#" in target:
                    raise ValueError("unsupported_workbook_content")
                path = posixpath.normpath(
                    target.lstrip("/") if target.startswith("/") else "xl/" + target
                )
                if (
                    not re.fullmatch(r"xl/worksheets/sheet[0-9]+\.xml", path)
                    or path not in archive.namelist()
                ):
                    raise ValueError("unsupported_workbook_content")
        book = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
    except (BadZipFile, OSError, KeyError, ValueError, DefusedXmlException, ParseError):
        raise ValueError("invalid_workbook") from None
    try:
        required = set(SHEETS) - {"travel_times"}
        if not required <= set(book.sheetnames) or not set(book.sheetnames) <= set(SHEETS):
            raise ValueError("invalid_sheets")
        rows, total_rows, total_cells = {}, 0, 0
        for sheet_name, columns in SHEETS.items():
            if sheet_name not in book.sheetnames:
                rows[sheet_name] = []
                continue
            sheet = book[sheet_name]
            # Do not trust XLSX-provided dimensions when iterating read-only XML.
            sheet.reset_dimensions()
            result = []
            saw_header = False
            for row_number, cells in enumerate(sheet.iter_rows(), 1):
                total_rows += 1
                total_cells += len(cells)
                if total_rows > MAX_TOTAL_ROWS or total_cells > MAX_TOTAL_CELLS:
                    raise ValueError("workbook_limit_exceeded")
                if len(cells) > len(columns):
                    raise ValueError("invalid_columns")
                values = []
                for cell in cells:
                    if cell.data_type in {"f", "e"}:
                        raise ValueError("unsupported_cell")
                    value = cell.value
                    if isinstance(value, str) and len(value) > MAX_CELL_TEXT:
                        raise ValueError("cell_too_long")
                    if isinstance(value, (date, datetime)):
                        if sheet_name != "dataset" or columns[len(values)] != "confirmed_at":
                            raise ValueError("unsupported_cell")
                        if isinstance(value, datetime) and value.time().isoformat() != "00:00:00":
                            raise ValueError("unsupported_cell")
                        value = value.isoformat()[:10]
                    if value is not None and not isinstance(value, (str, int, float, bool)):
                        raise ValueError("unsupported_cell")
                    if isinstance(value, float) and not math.isfinite(value):
                        raise ValueError("unsupported_cell")
                    values.append(value.strip() if isinstance(value, str) else value)
                values.extend([None] * (len(columns) - len(values)))
                if row_number == 1:
                    saw_header = True
                    legacy_locations = (
                        sheet_name == "locations" and tuple(values) == (*columns[:-1], None)
                    )
                    if tuple(values) != columns and not legacy_locations:
                        raise ValueError("invalid_columns")
                elif any(value is not None and value != "" for value in values):
                    if sheet_name == "travel_times":
                        result.append((row_number, tuple(values)))
                    else:
                        result.append((row_number, dict(zip(columns, values, strict=True))))
            if not saw_header:
                raise ValueError("invalid_columns")
            rows[sheet_name] = result
        return rows
    except (
        ValueError,
        TypeError,
        IndexError,
        DefusedXmlException,
        ParseError,
        BadZipFile,
        OSError,
    ):
        raise ValueError("invalid_workbook") from None
    finally:
        book.close()

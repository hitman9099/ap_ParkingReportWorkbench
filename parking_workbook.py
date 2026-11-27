"""Read-only validation for the platform's vehicle record exports."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel
from openpyxl.utils.exceptions import InvalidFileException


TIME_HEADERS = {"入场": "入场时间", "出场": "出场时间"}


class ExportValidationError(ValueError):
    """The workbook does not match the submitted query."""


@dataclass(frozen=True)
class ExportValidation:
    row_count: int
    min_timestamp: datetime | None
    max_timestamp: datetime | None


def _read_timestamp(value: object, label: str, *, epoch: datetime | None = None) -> datetime:
    try:
        if epoch is not None and type(value) in (int, float):
            timestamp = from_excel(value, epoch=epoch)
        elif isinstance(value, datetime):
            timestamp = value
        else:
            timestamp = datetime.fromisoformat(str(value).strip())
    except (ValueError, OverflowError):
        raise ExportValidationError(f"{label}缺失或格式无效。") from None

    if not isinstance(timestamp, datetime) or timestamp.tzinfo is not None:
        raise ExportValidationError(f"{label}须为不带时区的本地日期时间。")
    return timestamp


def validate_export(
    path: str | Path,
    *,
    project: str,
    start: datetime | str,
    end: datetime | str,
    status: str | None,
    expected_rows: int,
    entry_channel: str | None = None,
) -> ExportValidation:
    """Verify the query metadata and each record's timestamp for its recorded status."""
    project = project.strip()
    if not project:
        raise ExportValidationError("须提供完整停车场名称。")
    if status is not None and status not in TIME_HEADERS:
        raise ExportValidationError("状态须为入场、出场或不筛选。")
    if entry_channel is not None:
        entry_channel = entry_channel.strip()
        if not entry_channel:
            raise ExportValidationError("须提供完整入场通道名称。")
    if type(expected_rows) is not int or expected_rows < 0:
        raise ExportValidationError("查询条数须为非负整数。")
    start_time = _read_timestamp(start, "查询开始时间")
    end_time = _read_timestamp(end, "查询结束时间")
    if start_time > end_time:
        raise ExportValidationError("查询开始时间晚于结束时间。")

    try:
        with Path(path).open("rb") as source, closing(
            load_workbook(source, read_only=True, data_only=True)
        ) as workbook:
            sheet = workbook.active
            if sheet is None:
                raise ExportValidationError("导出文件缺少车辆记录工作表。")
            # The platform can mark a populated worksheet as A1:A1 in its XML.
            sheet.reset_dimensions()
            rows = sheet.iter_rows(values_only=True)
            title = next(rows, ())
            scope = next(rows, ())
            next(rows, ())  # Print timestamp.
            headers = next(rows, ())
            if not headers:
                raise ExportValidationError("导出文件缺少第 4 行表头。")

            title_text = str(title[0]).strip() if title else ""
            prefix = "车辆进出记录"
            title_start, _, title_end = title_text.removeprefix(prefix).partition("至")
            if not title_text.startswith(prefix) or not title_end:
                raise ExportValidationError("导出标题缺少查询时间范围。")
            actual_start = _read_timestamp(title_start, "导出标题开始时间")
            actual_end = _read_timestamp(title_end, "导出标题结束时间")
            if (actual_start, actual_end) != (start_time, end_time):
                raise ExportValidationError(
                    f"导出标题时间范围 {actual_start} 至 {actual_end} 与查询不符。"
                )
            if not scope or str(scope[0]).strip() != project:
                raise ExportValidationError("导出标题下方的停车场名称与查询不符。")

            headers = tuple(str(value).strip() for value in headers)
            time_headers = tuple(TIME_HEADERS.values()) if status is None else (TIME_HEADERS[status],)
            required_headers = ("所属车场", "状态", *time_headers)
            if entry_channel is not None:
                required_headers += ("入场通道",)
            for header in required_headers:
                if headers.count(header) != 1:
                    raise ExportValidationError(f"第 4 行须包含一个“{header}”列。")
            columns = {header: headers.index(header) for header in required_headers}
            project_column = columns["所属车场"]
            status_column = columns["状态"]
            channel_column = columns.get("入场通道")
            last_identity_column = max(project_column, status_column)

            row_count = 0
            earliest = latest = None
            for row_number, values in enumerate(rows, start=5):
                if all(value in (None, "") for value in values):
                    continue
                if len(values) <= last_identity_column:
                    raise ExportValidationError(f"第 {row_number} 行缺少必要列。")
                if str(values[project_column]).strip() != project:
                    raise ExportValidationError(f"第 {row_number} 行的停车场与查询不符。")
                row_status = str(values[status_column]).strip()
                if status is not None and row_status != status:
                    raise ExportValidationError(f"第 {row_number} 行的状态与查询不符。")
                if row_status not in TIME_HEADERS:
                    raise ExportValidationError(f"第 {row_number} 行的状态不是入场或出场。")
                if channel_column is not None and (
                    channel_column >= len(values) or str(values[channel_column]).strip() != entry_channel
                ):
                    raise ExportValidationError(f"第 {row_number} 行的入场通道与查询不符。")
                time_header = TIME_HEADERS[row_status]
                time_column = columns[time_header]
                timestamp = _read_timestamp(
                    values[time_column] if time_column < len(values) else None,
                    f"第 {row_number} 行的{time_header}",
                    epoch=workbook.epoch,
                )
                if not start_time <= timestamp <= end_time:
                    raise ExportValidationError(f"第 {row_number} 行的{time_header}超出查询时间范围。")
                row_count += 1
                earliest = timestamp if earliest is None else min(earliest, timestamp)
                latest = timestamp if latest is None else max(latest, timestamp)
    except (OSError, BadZipFile, InvalidFileException, ParseError):
        raise ExportValidationError("无法读取车辆进出记录 Excel 文件。") from None

    if row_count != expected_rows:
        raise ExportValidationError(f"导出共 {row_count} 条，查询显示 {expected_rows} 条。")
    return ExportValidation(row_count, earliest, latest)

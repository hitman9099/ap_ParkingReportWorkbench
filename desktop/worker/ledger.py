"""Read the supplied parking ledger, reconcile exported records and write a reviewed copy."""

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import isfinite
from pathlib import Path
from posixpath import join, normpath
import re
from tempfile import TemporaryDirectory
from unicodedata import normalize
from zipfile import ZipFile

from lxml import etree
from openpyxl import load_workbook
from openpyxl.formula import Tokenizer
from openpyxl.formula.tokenizer import TokenizerError
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.utils.datetime import to_excel
from openpyxl.workbook.properties import CalcProperties


REVIEW_SHEETS = ("车场查找与导出", "最终汇总")
NAME_HEADER = "地块名称"
HOURS_HEADER = "月总停车时长(小时)"
HOURS_FORMAT = "#,##0.00"
COUNT_FORMAT = "#,##0"
TITLE_FONT = Font(name="微软雅黑", color="18364B", bold=True, size=16)
HEADER_FONT = Font(name="微软雅黑", color="FFFFFF", bold=True)
HEADER_FILL = PatternFill("solid", fgColor="18364B")
WARNING_FILL = PatternFill("solid", fgColor="FBE8CD")
GOOD_FILL = PatternFill("solid", fgColor="DAF0E4")
OK_CHANNELS = {"已核对并汇总", "查询为零"}
TOTAL_RE = re.compile(r"(?:合计|总计|小计|总数)(?:[：:]?.*)?$")
DURATION_RE = re.compile(r"(?:(\d+)天)?(?:(\d+)小时)?(?:(\d+)分钟)?")
AUDIT_COLUMNS = (
    "名单车场", "查找方式", "实际搜索词", "平台项目名称", "项目编码", "查询入场通道",
    "车场查找结果", "通道处理结果", "平台查询条数", "报表条数", "核验条数", "原始报表", "备注",
)
DETAIL_COLUMNS = (
    "名单车场", "平台项目编码", "查询入场通道", "来源报表", "所属车场", "状态",
    "车牌号", "入场通道", "入场时间", "出场通道", "出场时间", "停车时长", "订单号",
)
XLS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
UNKNOWN = object()
COORD = r"\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}"


@dataclass(frozen=True)
class LedgerSite:
    row: int
    name: str


@dataclass(frozen=True)
class LedgerSpec:
    sheet_name: str
    header_row: int
    name_column: int
    duration_column: int
    sites: tuple[LedgerSite, ...]
    total_rows: tuple[int, ...]

    @property
    def names(self):
        return [site.name for site in self.sites]


@dataclass(frozen=True)
class OrderRecord:
    sheet: str
    row: int
    source: str
    code: str
    state: str
    plate: str
    entry_gate: str
    exit_gate: str
    entered: datetime | None
    exited: datetime | None
    minutes: int | None
    counted: bool

    @property
    def anchor(self):
        return f"{self.sheet}!{self.row}"


@dataclass
class ReportReview:
    audit_rows: list
    counts: Counter
    by_site: dict
    issues: list
    duplicate_rows: list


def _text(value):
    return str(value).strip() if value is not None else ""


def _header(value):
    return re.sub(r"\s+", "", normalize("NFKC", _text(value)))


def _locate_sheet(workbook):
    candidates = []
    for sheet in workbook.worksheets:
        if sheet.title in REVIEW_SHEETS or sheet.sheet_state != "visible":
            continue
        for row, values in enumerate(sheet.iter_rows(min_row=1, max_row=min(sheet.max_row or 100, 100), values_only=True), 1):
            headers = [_header(value) for value in values]
            if NAME_HEADER in headers and HOURS_HEADER in headers:
                if headers.count(NAME_HEADER) != 1 or headers.count(HOURS_HEADER) != 1:
                    raise ValueError(f"工作表“{sheet.title}”包含重复的地块名称或月总停车时长表头。")
                candidates.append((sheet, row, headers.index(NAME_HEADER)+1, headers.index(HOURS_HEADER)+1))
                break
    if len(candidates) != 1:
        raise ValueError("台账须包含唯一一张同时具有“地块名称”和“月总停车时长（小时）”表头的可见工作表。")
    return candidates[0]


def _spec(workbook):
    sheet, header_row, name_column, duration_column = _locate_sheet(workbook)
    sites = []
    totals = []
    names = set()
    for row, values in enumerate(sheet.iter_rows(min_row=header_row+1, max_col=name_column, values_only=True), header_row+1):
        if any(TOTAL_RE.fullmatch(_text(value)) for value in values):
            totals.append(row)
            continue
        value = values[-1]
        name = _text(value)
        if not name or name == NAME_HEADER or name.startswith(("说明：", "备注：", "口径说明：")):
            continue
        if not isinstance(value, str) or name.startswith(("=", "#")):
            raise ValueError(f"台账第 {row} 行地块名称须为文字。")
        canonical = normalize("NFKC", name)
        if canonical in names:
            raise ValueError(f"台账第 {row} 行地块名称重复，请合并或改用唯一名称。")
        names.add(canonical)
        sites.append(LedgerSite(row, name))
    if not sites:
        raise ValueError("台账地块名称列中没有停车场。")
    return LedgerSpec(sheet.title, header_row, name_column, duration_column, tuple(sites), tuple(totals))


def read_ledger(path):
    """Find the authoritative roster in a single Excel input sheet."""
    path = Path(path)
    if path.suffix.lower() != ".xlsx" or not path.is_file():
        raise ValueError("请选择有效的 .xlsx 台账文件。")
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        return _spec(workbook)
    finally:
        workbook.close()


def _timestamp(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(_text(value)).replace(tzinfo=None)
    except ValueError:
        return None


def _minutes(value):
    if isinstance(value, timedelta):
        whole, extra = divmod(value.total_seconds(), 60)
        return int(whole) if whole >= 0 and not extra else None
    if isinstance(value, time) and value.second == value.microsecond == 0:
        return value.hour * 60 + value.minute
    text = re.sub(r"\s+", "", _text(value))
    match = DURATION_RE.fullmatch(text) if text else None
    if match is None:
        return None
    days, hours, minutes = (int(component or 0) for component in match.groups())
    return days * 1440 + hours * 60 + minutes


def _integer(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value >= 0 and int(value) == value else None


def _positions(values, required, sheet):
    headers = [_text(value) for value in values]
    for title in required:
        if headers.count(title) != 1:
            raise ValueError(f"报表“{sheet}”缺少唯一的“{title}”字段。")
    return [headers.index(title) for title in required]


def _items(row, positions):
    return [row[i] if i < len(row) else None for i in positions]


def _audit(input_book, start, end):
    try:
        sheet = input_book["车场与通道核对"]
    except KeyError:
        raise ValueError("报表缺少“车场与通道核对”工作表。") from None
    sheet.reset_dimensions()
    rows = iter(sheet.values)
    next(rows, None)
    period = next(rows, ())
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", _text(period[0]) if period else "")
    if dates != [start.isoformat(), end.isoformat()]:
        raise ValueError("所选日期范围与已核验报表中的日期不一致。")
    next(rows, None)
    next(rows, None)
    positions = _positions(next(rows, ()), AUDIT_COLUMNS, sheet.title)
    audit_rows = []
    expected = {}
    names_to_code = {}
    code_to_name = {}
    for row in rows:
        site, method, query, park, code, lane, park_state, state, queried, reported, verified, report, note = _items(row, positions)
        site, park, code, lane, report, state = map(_text, (site, park, code, lane, report, state))
        if not site:
            continue
        for actual, index, key in ((code, names_to_code, site), (site, code_to_name, code)):
            if code and actual != index.setdefault(key, actual):
                raise ValueError("车场与通道报表包含冲突的名单车场和项目编码。")
        queried, reported, verified = map(_integer, (queried, reported, verified))
        if state in OK_CHANNELS:
            if not code or not park or not lane or queried is None or verified != queried or (reported is not None and reported != verified):
                raise ValueError(f"{site}的入场通道审计条数或项目标识不一致。")
            if state == "查询为零" and verified:
                raise ValueError(f"{site}的零记录通道含有报表明细。")
            key = (site, code, lane, report)
            if key in expected:
                raise ValueError(f"{site}存在重复的通道报表。")
            expected[key] = (park, verified)
        audit_rows.append((site, method, query, park, code, lane, park_state, state, queried, reported, verified, report, note))
    return audit_rows, expected


def _analyze_report(path, start, end):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        audit, expected = _audit(workbook, start, end)
        counts = Counter()
        by_site = defaultdict(Counter)
        actual_counts = Counter()
        studies = {}
        duplicates = defaultdict(list)
        issues = []
        for sheet in workbook.worksheets:
            if not sheet.title.startswith("车辆明细"):
                continue
            sheet.reset_dimensions()
            rows = iter(sheet.values)
            indices = _positions(next(rows, ()), DETAIL_COLUMNS, sheet.title)
            for line, row in enumerate(rows, 2):
                source, code, requested, filename, park, state, plate, entry_gate, entered, exit_gate, exited, duration, order = _items(row, indices)
                source, code, requested, filename, park, state, plate, entry_gate, exit_gate, order = map(_text, (
                    source, code, requested, filename, park, state, plate, entry_gate, exit_gate, order,
                ))
                if not source and not any(row):
                    continue
                key = (source, code, requested, filename)
                if key not in expected or expected[key][0] != park or entry_gate != requested:
                    raise ValueError(f"{sheet.title}第 {line} 行与已核验的车场、查询通道或来源报表不符。")
                entered, exited = _timestamp(entered), _timestamp(exited)
                event = {"入场": entered, "出场": exited}.get(state)
                if event is None or not start <= event.date() <= end:
                    raise ValueError(f"{sheet.title}第 {line} 行的事件状态或发生日期超出核验范围。")
                actual_counts[key] += 1
                counts["records"] += 1
                stats = by_site[source]
                stats["records"] += 1
                reason = []
                minutes = _minutes(duration)
                counted = state == "出场" and minutes is not None
                if state == "入场":
                    counts["entry_records"] += 1
                    if exited is not None or _text(duration):
                        reason.append("入场状态附出场时间或停车时长")
                else:
                    counts["exit_records"] += 1
                    if not counted:
                        counts["excluded_exits"] += 1
                        reason.append("停车时长无法识别" if _text(duration) else "缺少停车时长")
                    if entered is None:
                        reason.append("缺少有效入场时间")
                    elif entered > exited:
                        reason.append("入场晚于出场")
                    elif counted and not 0 <= minutes * 60 - (exited - entered).total_seconds() < 60:
                        reason.append("报表时长与进出场时间差非整分钟口径")
                    if counted:
                        stats["minutes"] += minutes
                        stats["included"] += 1
                if reason:
                    issues.append((source, code, f"{sheet.title}!{line}", state, "、".join(reason), "已按报表计入" if counted else "未计入"))
                if order:
                    detail = OrderRecord(sheet.title, line, source, code, state, plate, entry_gate,
                                         exit_gate, entered, exited, minutes, counted)
                    if order in studies:
                        duplicates[order].append(detail)
                    else:
                        studies[order] = detail
        if any(actual_counts[key] != value[1] for key, value in expected.items()):
            raise ValueError("报表明细实际条数与通道核验条数不一致。")
        duplicate_rows = [_duplicate_group([studies[order], *records]) for order, records in duplicates.items()]
        return ReportReview(audit, counts, by_site, issues, duplicate_rows)
    finally:
        workbook.close()


def _duplicate_group(rows):
    first = rows[0]
    compatible = first.plate and first.entered and first.entry_gate and first.exit_gate and all(
        row.counted and row.exited is not None and row.entered is not None and row.exited >= row.entered
        and (row.code, row.plate, row.entered, row.entry_gate, row.exit_gate)
        == (first.code, first.plate, first.entered, first.entry_gate, first.exit_gate) for row in rows
    )
    if compatible:
        latest = max(rows, key=lambda row: row.exited)
        removed = [row for row in rows if row is not latest]
        return (first.source, first.code, "同车牌、同入场时间和通道", latest.anchor,
                "、".join(row.anchor for row in removed), sum(row.minutes for row in removed))
    reason = "车牌不同，保留待核对" if len({row.plate for row in rows if row.plate}) > 1 else "记录身份或状态待人工核对"
    return (first.source, first.code, reason, "、".join(row.anchor for row in rows), "", 0)


def _style_sheet(sheet, title, widths):
    sheet.sheet_view.showGridLines = False
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.append([title])
    sheet["A1"].font = TITLE_FONT
    sheet.row_dimensions[1].height = 28


def _append(sheet, values):
    sheet.append(values)
    for cell in sheet[sheet.max_row][:len(values)]:
        if isinstance(cell.value, str):
            cell.data_type = "s"


def _header_row(sheet, values):
    _append(sheet, values)
    sheet.row_dimensions[sheet.max_row].height = 24
    for cell in sheet[sheet.max_row][:len(values)]:
        cell.font, cell.fill = HEADER_FONT, HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)


def _section(sheet, title, headers, records):
    if records:
        sheet.append([])
        _append(sheet, [title])
        sheet.cell(sheet.max_row, 1).font = TITLE_FONT
        _header_row(sheet, headers)
        for fields in records:
            _append(sheet, fields)
            for cell in sheet[sheet.max_row][:len(fields)]:
                cell.alignment = Alignment(vertical="center", wrap_text=True)


def _ledger_issues(spec, original_values, source_sheet):
    sheet = original_values[spec.sheet_name]
    headers = [_header(cell.value) for cell in source_sheet[spec.header_row]]
    cost = next((i for i, value in enumerate(headers, 1) if value.startswith("建设费")), None)
    returns = next((i for i, value in enumerate(headers, 1) if value.startswith("投资回收期")), None)
    if cost is None or returns is None:
        return []
    issues = []
    for site in spec.sites:
        value = sheet.cell(site.row, returns)
        if value.data_type == "e":
            source_cost = sheet.cell(site.row, cost)
            formula = source_sheet.cell(site.row, returns).value
            cost_pattern = rf"\$?{get_column_letter(cost)}\$?{site.row}\s*\*"
            if value.value == "#VALUE!" and isinstance(source_cost.value, str) and isinstance(formula, str) and re.search(cost_pattern, formula, re.I):
                note = "建设费为非空文本，公式执行乘除运算时报错"
            else:
                note = "检查台账原有公式和引用值"
            issues.append((site.name, value.coordinate, value.value, source_cost.coordinate, source_cost.value, note))
    return issues


def _render_reviews(workbook, spec, review, start, end, amounts, missing, existing_errors):
    for title in REVIEW_SHEETS:
        if title in workbook.sheetnames:
            del workbook[title]
    audit = workbook.create_sheet(REVIEW_SHEETS[0])
    _style_sheet(audit, "车场与入场通道核对", (33, 15, 30, 35, 15, 23, 18, 21, 15, 15, 15, 55, 60))
    _append(audit, (f"查询区间 {start.isoformat()} 至 {end.isoformat()}，含首尾两天",))
    _header_row(audit, AUDIT_COLUMNS)
    for values in review.audit_rows:
        _append(audit, values)
        audit.cell(audit.max_row, 8).fill = GOOD_FILL if values[7] in OK_CHANNELS else WARNING_FILL
    audit.freeze_panes = "C4"
    audit.auto_filter.ref = f"A3:M{audit.max_row}"

    summary = workbook.create_sheet(REVIEW_SHEETS[1])
    _style_sheet(summary, "停车时长导出汇总", (35, 18, 27, 31, 31, 38))
    _append(summary, (f"查询区间 {start.isoformat()} 至 {end.isoformat()}，含首尾两天。出入状态未选。",))
    _header_row(summary, ("核对项目", "本次结果"))
    total = sum(amounts.values())
    proposed = sum(row[-1] for row in review.duplicate_rows if row[0] in amounts)
    verified_channels = sum(row[7] in OK_CHANNELS for row in review.audit_rows)
    matched = len({row[0] for row in review.audit_rows if row[6] == "已匹配"})
    keyword = len({row[0] for row in review.audit_rows if _text(row[1]) in {"关键词", "关键字"}})
    summary_values = (
        ("台账地块数", len(spec.sites)), ("已更新地块数", len(amounts)), ("未覆盖或待核对地块数", len(missing)),
        ("已匹配地块数", matched), ("关键词匹配地块数", keyword), ("通过核验的入场通道数", verified_channels),
        ("报表核验行数", review.counts["records"]), ("入场记录数", review.counts["entry_records"]),
        ("出场记录数", review.counts["exit_records"]), ("缺少或无法解析时长的出场数", review.counts["excluded_exits"]),
        ("本次写入月总停车时长（小时）", total / 60), ("重复订单核对组数", len(review.duplicate_rows)),
        ("同一次停放拟扣减（分钟）", proposed), ("拟扣减后时长（小时）", (total-proposed)/60),
    )
    for label, value in summary_values:
        _append(summary, (label, value))
        summary.cell(summary.max_row, 2).number_format = HOURS_FORMAT if label.endswith("（小时）") else COUNT_FORMAT
    _append(summary, ("时长按可解析的出场记录累计。入场状态及无有效时长的出场行计入报表行数。",))
    _append(summary, ("重复订单扣减为复核建议，写入台账的时长保留原报表口径。",))
    if missing:
        _append(summary, ("本次写入时长仅包含已更新地块。原台账合计可能包含待核对地块已有的数值。",))
    _section(summary, "待核对地块", ("地块名称", "台账行号", "问题"), missing)
    _section(summary, "异常明细", ("名单车场", "项目编码", "来源行位置", "出入状态", "问题", "计入情况"), review.issues)
    _section(summary, "重复订单核对", ("名单车场", "项目编码", "判断依据", "复核或拟保留位置", "拟扣减位置", "拟扣减分钟"), review.duplicate_rows)
    _section(summary, "原台账公式问题", ("地块名称", "公式单元格", "显示结果", "建设费位置", "建设费原值", "待核对原因"), existing_errors)
    summary.freeze_panes = "A4"
    return {"hours": total / 60, "updated_parks": len(amounts), "unresolved_parks": len(missing),
            "records": review.counts["records"], "entry_records": review.counts["entry_records"],
            "exit_records": review.counts["exit_records"], "excluded_exits": review.counts["excluded_exits"],
            "duplicate_groups": len(review.duplicate_rows), "proposed_minutes": proposed,
            "proposed_hours": (total-proposed)/60, "issue_records": len(review.issues),
            "matched": matched, "keyword": keyword, "verified_channels": verified_channels}


def _formula_references(value, sheet_name, sheet_names):
    if not isinstance(value, str) or re.search(r"\b(?:INDIRECT|OFFSET)\(", value, re.I):
        return None
    try:
        tokens = Tokenizer(value).items
        ranges = []
        for token in tokens:
            if token.type != "OPERAND" or token.subtype != "RANGE":
                continue
            if "!" in token.value:
                name, address = token.value.rsplit("!", 1)
                name = name.strip("'").replace("''", "'")
                sheet = sheet_names.get(name.casefold())
            else:
                sheet, address = sheet_name, token.value
            if sheet is None or (":" not in address and not re.fullmatch(COORD, address, re.I)):
                return None
            ranges.append((sheet, range_boundaries(address)))
        return ranges
    except (TokenizerError, ValueError):
        return None


def _affected_formulas(formulas, changed, sheet_names):
    reverse = defaultdict(set)
    points = defaultdict(list)
    for name, row, column in set(formulas) | changed:
        points[name].append((row, column))
    for cells in points.values():
        cells.sort()
    affected = set(changed)
    for key, formula in formulas.items():
        references = _formula_references(formula.value, key[0], sheet_names)
        if references is None:
            affected.add(key)
            continue
        for name, (left, top, right, bottom) in references:
            if name in REVIEW_SHEETS:
                affected.add(key)
                continue
            positions = points[name]
            start = bisect_left(positions, (top or 1, 1))
            end = bisect_right(positions, (bottom or 1_048_576, 16_384))
            for index in range(start, end):
                row, col = positions[index]
                if (left or 1) <= col <= (right or 16_384):
                    reverse[(name, row, col)].add(key)
    pending = list(affected)
    while pending:
        new = reverse[pending.pop()] - affected
        affected.update(new)
        pending.extend(new)
    return affected


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _calculate_ledger_formula(formula, value_at):
    if not isinstance(formula, str):
        return UNKNOWN
    formula = re.sub(r"\s+|\$", "", formula).upper()
    match = re.fullmatch(r"=SUM\(([A-Z]+[0-9]+:[A-Z]+[0-9]+)\)", formula)
    if match:
        left, top, right, bottom = range_boundaries(match[1])
        if (right-left+1) * (bottom-top+1) > 50_000:
            return UNKNOWN
        values = [value_at(row, col) for row in range(top, bottom+1) for col in range(left, right+1)]
        if any(v is UNKNOWN or (isinstance(v, str) and v.startswith("#")) for v in values):
            return UNKNOWN
        return sum(value for value in values if _number(value))
    match = re.fullmatch(r'=IFERROR\(([A-Z]+[0-9]+)/\(([A-Z]+[0-9]+)\*([0-9]+(?:\.[0-9]+)?)\),""\)', formula)
    if match:
        values = [value_at(row, col) for col, row, _, _ in (range_boundaries(match[i]) for i in (1, 2))]
        if any(value is UNKNOWN for value in values):
            return UNKNOWN
        dividend, divisor = (0 if value is None else value for value in values)
        if _number(dividend) and _number(divisor) and divisor and float(match[3]):
            return dividend / (divisor * float(match[3]))
        return ""
    return UNKNOWN


def _sheet_paths(book):
    xml = etree.fromstring(book.read("xl/workbook.xml"))
    relations = etree.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    targets = {node.get("Id"): node.get("Target") for node in relations}
    return {node.get("name"): normpath(target.lstrip("/") if target.startswith("/") else join("xl", target))
            for node in xml.find(XLS+"sheets") if (target := targets.get(node.get(REL+"id")))}


def _restore_caches(saved, ready, workbook, old_values, changed):
    formulas = {(sheet.title, cell.row, cell.column): cell
                for sheet in workbook.worksheets if sheet.title not in REVIEW_SHEETS
                for row in sheet.iter_rows() for cell in row if cell.data_type == "f"}
    affected = _affected_formulas(formulas, changed, {name.casefold(): name for name in workbook.sheetnames})
    calculated = {}
    cache_values = defaultdict(dict)
    for (name, row, col), formula in formulas.items():
        if (name, row, col) not in affected:
            old = old_values[name].cell(row, col)
            calculated[(name,row,col)] = old.value
            if old.value is not None:
                cache_values[name][formula.coordinate] = (old.data_type, old.value)

    def value_at(name, row, col):
        key = (name, row, col)
        if key in calculated:
            return calculated[key]
        if key in formulas:
            return UNKNOWN
        return workbook[name].cell(row, col).value

    for (name,row,col), formula in formulas.items():
        if (name,row,col) in affected:
            value = _calculate_ledger_formula(formula.value, lambda r,c: value_at(name,r,c))
            if value is not UNKNOWN:
                calculated[(name,row,col)] = value
                cache_values[name][formula.coordinate] = ("n" if _number(value) else "str", value)

    with ZipFile(saved) as authored, ZipFile(ready, "w") as final:
        lookup = _sheet_paths(authored)
        caches = {lookup[name]: cells for name,cells in cache_values.items()}
        for member in authored.infolist():
            data = authored.read(member)
            if member.filename in caches:
                sheet = etree.fromstring(data)
                available = caches[member.filename]
                for cell in sheet.iter(XLS+"c"):
                    item = available.get(cell.get("r"))
                    if item is None or cell.find(XLS+"f") is None:
                        continue
                    kind, value = item
                    kind = {"s":"str", "d":"n"}.get(kind, kind)
                    if isinstance(value,(date,time,timedelta)):
                        value = to_excel(value, workbook.epoch)
                    if isinstance(value,bool):
                        value = int(value)
                    node = cell.find(XLS+"v")
                    if node is None:
                        node = etree.SubElement(cell, XLS+"v")
                    node.text = str(value)
                    cell.set("t", kind)
                data = etree.tostring(sheet, encoding="UTF-8")
            final.writestr(member, data)


def write_ledger(input_path, channel_report_path, output_path, start, end, *, aliases=None):
    """Write report hours and diagnostics into a separate, atomically published ledger copy."""
    source, report, output = (Path(path).resolve() for path in (input_path, channel_report_path, output_path))
    if len({source, report, output}) != 3 or output.suffix.lower() != ".xlsx":
        raise ValueError("输出台账须为独立的 .xlsx 文件。")
    start, end = (value if isinstance(value, date) else date.fromisoformat(value) for value in (start, end))
    if start > end:
        raise ValueError("起始日期应早于或等于结束日期。")
    review = _analyze_report(report, start, end)
    workbook = load_workbook(source, data_only=False)
    original_values = load_workbook(source, data_only=True)
    try:
        spec = _spec(original_values)
        sheet = workbook[spec.sheet_name]
        existing_errors = _ledger_issues(spec, original_values, sheet)
        audit_by_site = defaultdict(list)
        for row in review.audit_rows:
            audit_by_site[row[0]].append(row)
        aliases = aliases or {}
        requested = [site.name if site.name in audit_by_site else aliases.get(site.name, site.name) for site in spec.sites]
        if len(requested) != len(set(requested)):
            raise ValueError("台账的多个地块对应同一报表名称，请复核项目映射。")
        if set(audit_by_site) - set(requested):
            raise ValueError("报表含当前台账以外的地块，请核对输入文件。")
        missing, amounts, targets, changed = [], {}, {}, set()
        for site, source_name in zip(spec.sites, requested, strict=True):
            channels = audit_by_site[source_name]
            target = sheet.cell(site.row, spec.duration_column)
            target.number_format = HOURS_FORMAT
            if target.data_type == "f":
                missing.append((site.name, site.row, "时长单元格已有公式，保留待核对"))
            elif not channels or any(row[7] not in OK_CHANNELS for row in channels):
                missing.append((site.name, site.row, "录入待核对，通道报表未全部通过核验"))
            else:
                minutes = review.by_site[source_name]["minutes"]
                if target.value != minutes / 60:
                    changed.add((spec.sheet_name, site.row, spec.duration_column))
                target.value = minutes / 60
                targets[target.coordinate] = target.value
                amounts[source_name] = minutes
        for line in spec.total_rows:
            sheet.cell(line, spec.duration_column).number_format = HOURS_FORMAT
        column_letter = get_column_letter(spec.duration_column)
        subtotal = re.compile(rf"=SUM\(\$?{column_letter}\$?\d+:\$?{column_letter}\$?\d+\)", re.IGNORECASE)
        for line in range(spec.header_row + 1, sheet.max_row + 1):
            duration = sheet.cell(line, spec.duration_column)
            if duration.data_type == "f" and isinstance(duration.value, str) and subtotal.fullmatch(duration.value):
                duration.number_format = HOURS_FORMAT
        result = _render_reviews(workbook, spec, review, start, end, amounts, missing, existing_errors)
        workbook.calculation = CalcProperties(calcMode="auto", fullCalcOnLoad=True, forceFullCalc=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=output.parent, prefix="parking-ledger-") as staging:
            saved = Path(staging) / "authored.xlsx"
            ready = Path(staging) / "ledger.xlsx"
            workbook.save(saved)
            _restore_caches(saved, ready, workbook, original_values, changed)
            _verify_written(ready, spec, targets)
            ready.replace(output)
        return result
    finally:
        original_values.close()
        workbook.close()


def _verify_written(path, spec, targets):
    with ZipFile(path) as package:
        if package.testzip() is not None:
            raise ValueError("保存的台账未通过 Excel 文件完整性验证。")
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        sheet = workbook[spec.sheet_name]
        if not set(REVIEW_SHEETS).issubset(workbook.sheetnames):
            raise ValueError("保存的台账缺少查找与汇总工作表。")
        for row in sheet.iter_rows(min_row=spec.header_row+1, max_col=spec.duration_column):
            cell = row[-1]
            if hasattr(cell, "coordinate") and cell.coordinate in targets:
                expected = targets.pop(cell.coordinate)
                if not isfinite(cell.value) or abs(cell.value - expected) > 1e-9 or cell.number_format != HOURS_FORMAT:
                    raise ValueError(f"台账写入复核失败 {cell.coordinate}。")
        if targets:
            raise ValueError("保存的台账缺少预期的时长单元格。")
    finally:
        workbook.close()

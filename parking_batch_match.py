"""Match a parking list against an account catalog without using the network."""

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from unicodedata import normalize


DISTRICT_PREFIXES = ("北京市通州区", "通州区", "北京市")
NUMBERED_PARK_NOTE = re.compile(r"(.+\d+号停车场)\(([^()]+)\)")
PARK_SCOPE_NOTE = re.compile(r"[0-9东西南北]|[一二三四五六七八九十甲乙丙丁]+[号期区栋座]|内侧|外侧|入口|出口|地上|地下")


@dataclass(frozen=True)
class NameKeys:
    exact: str
    without_suffix: str
    keyword: str
    note: str


def _keys(name):
    compact = re.sub(r"\s+", "", normalize("NFKC", name))
    base = compact
    note = ""
    annotation = NUMBERED_PARK_NOTE.fullmatch(compact)
    if annotation and not PARK_SCOPE_NOTE.search(annotation[2]):
        base, note = annotation.groups()
    without_suffix = base.removesuffix("停车场")
    keyword = without_suffix
    for prefix in DISTRICT_PREFIXES:
        shortened = keyword.removeprefix(prefix)
        if shortened != keyword and shortened:
            keyword = shortened
            break
    return NameKeys(compact.casefold(), without_suffix, keyword, note.casefold())


def _text(value):
    return str(value).strip() if value is not None else ""


def match_catalog(names, catalog):
    """Return an ordered manifest for a catalog of name/code mappings or pairs.

    Pass None when the account catalog has not yet been checked. A supplied
    catalog is assumed to cover the account scope available to the caller.
    """
    by_exact = defaultdict(list)
    by_keyword = defaultdict(list)
    seen_entries = set()
    for item in catalog or ():
        if isinstance(item, Mapping):
            name = _text(item.get("name", item.get("project_name")))
            code = _text(item.get("code", item.get("project_code")))
        else:
            name, code = map(_text, item)
        if not name:
            continue
        keys = _keys(name)
        identity = (keys.exact, code)
        if identity in seen_entries:
            continue
        seen_entries.add(identity)
        by_exact[keys.exact].append((name, code, keys))
        if keys.keyword:
            by_keyword[keys.keyword.casefold()].append((name, code, keys))

    manifest = []
    claims = defaultdict(list)
    for source in names:
        name = _text(source)
        keys = _keys(name)
        row = {
            "source_name": name,
            "matched_name": "",
            "project_code": "",
            "match_method": "",
            "query": keys.keyword or name,
            "state": "missing",
            "note": "目录未找到可唯一确认的停车场。",
        }
        manifest.append(row)
        if not name:
            row["note"] = "名单缺少停车场名称。"
            continue
        if catalog is None:
            row.update(state="unattempted", note="尚未读取账号项目目录，搜索词供后续查询。")
            continue

        candidates = by_exact.get(keys.exact, [])
        if candidates:
            row["query"] = name
        else:
            candidates = [
                entry for entry in by_keyword.get(keys.keyword.casefold(), [])
                if not keys.note or not entry[2].note or keys.note == entry[2].note
            ]
        if len(candidates) > 1:
            row.update(state="ambiguous", note=f"目录有 {len(candidates)} 个候选项目，需核对项目编码。")
            continue
        if not candidates:
            continue

        matched_name, code, matched_keys = candidates[0]
        if name == matched_name:
            method, priority = "exact", 0
        elif keys.exact == matched_keys.exact:
            method, priority = "normalized", 1
        elif keys.without_suffix.casefold() == matched_keys.without_suffix.casefold():
            method, priority = "suffix", 2
            row["query"] = keys.without_suffix
        else:
            method, priority = "keyword", 2
        row.update(matched_name=matched_name, project_code=code, match_method=method, state="matched", note="")
        identity = ("code", code) if code else ("name", matched_keys.exact)
        claims[identity].append((priority, row))

    for competing in claims.values():
        if len(competing) == 1:
            continue
        best = min(priority for priority, _ in competing)
        shared_best = sum(priority == best for priority, _ in competing) > 1
        for priority, row in competing:
            if shared_best or priority != best:
                row.update(
                    matched_name="", project_code="", match_method="", state="ambiguous",
                    note="名单中多个名称指向同一平台项目，需确认对应关系。",
                )
    return manifest

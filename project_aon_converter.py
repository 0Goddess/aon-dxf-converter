#!/usr/bin/env python3
"""Convert Microsoft Project XML into auditable AON data and editable DXF drawings.

The converter deliberately treats Microsoft Project's calculated dates and slack
as authoritative.  It builds a graph only for validation and drawing layout.
"""

from __future__ import annotations

import argparse
import collections
import html
import json
import math
import re
import sys
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable


NS_URI = "http://schemas.microsoft.com/project"
NS = {"p": NS_URI}
RELATION_TYPES = {0: "FF", 1: "FS", 2: "SF", 3: "SS"}
CONSTRAINT_TYPES = {
    0: "越早越好(ASAP)",
    1: "越晚越好(ALAP)",
    2: "必須開始於(MSO)",
    3: "必須完成於(MFO)",
    4: "不得早於開始(SNET)",
    5: "不得晚於開始(SNLT)",
    6: "不得早於完成(FNET)",
    7: "不得晚於完成(FNLT)",
}
DAY_NAMES = {1: "星期日", 2: "星期一", 3: "星期二", 4: "星期三", 5: "星期四", 6: "星期五", 7: "星期六"}
ZONE_COLORS = {
    "共同／前置": "#E2E8F0",
    "A1區": "#DBEAFE",
    "A2區": "#DCFCE7",
    "B區": "#FEF3C7",
    "其他共同工程": "#F3E8FF",
    "驗收／送電": "#FCE7F3",
}


def child_text(node: ET.Element, name: str, default: str = "") -> str:
    namespaced_path = "/".join(f"p:{part}" for part in name.split("/"))
    value = node.findtext(namespaced_path, default=default, namespaces=NS)
    return default if value is None else value


def child_int(node: ET.Element, name: str, default: int = 0) -> int:
    try:
        return int(child_text(node, name, ""))
    except (TypeError, ValueError):
        return default


def iso_duration_hours(raw: str) -> float | None:
    if not raw:
        return None
    match = re.fullmatch(
        r"P(?:(?P<days>-?\d+(?:\.\d+)?)D)?"
        r"(?:T(?:(?P<hours>-?\d+(?:\.\d+)?)H)?"
        r"(?:(?P<minutes>-?\d+(?:\.\d+)?)M)?"
        r"(?:(?P<seconds>-?\d+(?:\.\d+)?)S)?)?",
        raw,
    )
    if not match:
        return None
    parts = {key: float(value or 0) for key, value in match.groupdict().items()}
    return parts["days"] * 24 + parts["hours"] + parts["minutes"] / 60 + parts["seconds"] / 3600


def tenths_minute_to_days(raw: str, minutes_per_day: int) -> float | None:
    if raw == "":
        return None
    try:
        return int(raw) / 10 / minutes_per_day
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def parse_datetime(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def date_only(raw: str) -> str:
    value = parse_datetime(raw)
    return value.strftime("%Y-%m-%d") if value else ""


def compact_number(value: float | None) -> str:
    if value is None:
        return "-"
    if math.isclose(value, round(value), abs_tol=1e-9):
        return str(int(round(value)))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def zone_from_outline(outline: str, summary_names: dict[str, str]) -> str:
    """Return a project-specific lane name instead of assuming A1/A2/B WBS codes.

    The second outline level normally represents the owner's phase, building,
    or area.  Its summary name is therefore a useful lane label for any Project
    file.  Shallow/irregular schedules fall back to their nearest summary and
    finally to one neutral lane.
    """
    pieces = [piece for piece in outline.split(".") if piece]
    candidates: list[str] = []
    if len(pieces) >= 2:
        candidates.append(".".join(pieces[:2]))
    candidates.extend(".".join(pieces[:length]) for length in range(len(pieces) - 1, 0, -1))
    for candidate in candidates:
        name = " ".join(summary_names.get(candidate, "").split())
        if name:
            return name
    return "全工程"


def display_width(text: str) -> int:
    return sum(2 if ord(ch) > 127 else 1 for ch in text)


def wrap_display(text: str, limit: int, max_lines: int = 2) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    current_width = 0
    for ch in text:
        width = 2 if ord(ch) > 127 else 1
        if current and current_width + width > limit:
            lines.append(current)
            current = ch
            current_width = width
            if len(lines) == max_lines:
                break
        else:
            current += ch
            current_width += width
    if len(lines) < max_lines and current:
        lines.append(current)
    consumed = sum(len(line) for line in lines)
    if consumed < len(text) and lines:
        last = lines[-1]
        lines[-1] = (last[:-1] if last else "") + "…"
    return lines[:max_lines]


@dataclass
class Link:
    index: int
    pred_uid: int
    succ_uid: int
    relation: str
    lag_days: float | None
    lag_format: int
    cross_project: bool
    critical: bool = False

    @property
    def label(self) -> str:
        lag = self.lag_days or 0
        if math.isclose(lag, 0):
            return self.relation
        sign = "+" if lag > 0 else "-"
        return f"{self.relation}{sign}{compact_number(abs(lag))}d"


@dataclass
class Task:
    uid: int
    task_id: int
    name: str
    wbs: str
    outline_number: str
    outline_level: int
    parent_summary: str
    summary: bool
    milestone: bool
    manual: bool
    critical: bool
    duration_days: float | None
    remaining_days: float | None
    percent_complete: int
    physical_percent_complete: int
    start: str
    finish: str
    early_start: str
    early_finish: str
    late_start: str
    late_finish: str
    actual_start: str
    actual_finish: str
    free_slack_days: float | None
    total_slack_days: float | None
    constraint_type_code: int
    constraint_date: str
    calendar_uid: int
    is_null: bool
    external: bool
    zone: str
    predecessor_uids: list[int] = field(default_factory=list)
    successor_uids: list[int] = field(default_factory=list)
    predecessor_labels: list[str] = field(default_factory=list)
    successor_labels: list[str] = field(default_factory=list)
    rank: int = 0
    row: int = 0
    boundary: bool = False

    @property
    def constraint_type(self) -> str:
        return CONSTRAINT_TYPES.get(self.constraint_type_code, f"代碼{self.constraint_type_code}")


@dataclass
class ProjectModel:
    source_path: Path
    project: dict
    tasks_all: dict[int, Task]
    tasks: dict[int, Task]
    links: list[Link]
    summaries: list[dict]
    calendars: list[dict]
    calendar_exceptions: list[dict]
    issues: list[dict]
    minutes_per_day: int
    critical_limit_days: float


def parse_project(path: Path) -> ProjectModel:
    root = ET.parse(path).getroot()
    minutes_per_day = child_int(root, "MinutesPerDay", 480)
    critical_limit_days = child_int(root, "CriticalSlackLimit", 0) / 10 / minutes_per_day
    project_fields = [
        "Name", "Title", "Author", "CreationDate", "LastSaved", "ScheduleFromStart",
        "StartDate", "FinishDate", "CurrentDate", "StatusDate", "CriticalSlackLimit",
        "CalendarUID", "MinutesPerDay", "MinutesPerWeek", "DaysPerMonth",
        "MultipleCriticalPaths", "NewTasksAreManual", "HonorConstraints",
    ]
    project = {name: child_text(root, name) for name in project_fields}

    calendars: list[dict] = []
    calendar_exceptions: list[dict] = []
    for calendar in root.findall("p:Calendars/p:Calendar", NS):
        uid = child_int(calendar, "UID", -1)
        name = child_text(calendar, "Name")
        weekly = []
        exception_count = 0
        for weekday in calendar.findall("p:WeekDays/p:WeekDay", NS):
            day_type = child_int(weekday, "DayType", -1)
            working = bool(child_int(weekday, "DayWorking", 0))
            times = []
            for wt in weekday.findall("p:WorkingTimes/p:WorkingTime", NS):
                times.append(f"{child_text(wt, 'FromTime')}–{child_text(wt, 'ToTime')}")
            if day_type == 0:
                exception_count += 1
                calendar_exceptions.append(
                    {
                        "calendar_uid": uid,
                        "calendar_name": name,
                        "from": date_only(child_text(weekday, "TimePeriod/FromDate")),
                        "to": date_only(child_text(weekday, "TimePeriod/ToDate")),
                        "working": "是" if working else "否",
                        "working_times": "、".join(times),
                    }
                )
            elif day_type in DAY_NAMES:
                weekly.append(
                    {
                        "day_type": day_type,
                        "day_name": DAY_NAMES[day_type],
                        "working": "是" if working else "否",
                        "working_times": "、".join(times),
                    }
                )
        calendars.append(
            {
                "uid": uid,
                "name": name,
                "is_base": "是" if child_int(calendar, "IsBaseCalendar", 0) else "否",
                "base_uid": child_int(calendar, "BaseCalendarUID", -1),
                "working_weekdays": sum(1 for row in weekly if row["working"] == "是"),
                "exceptions": exception_count,
                "weekly": weekly,
            }
        )

    raw_tasks = root.findall("p:Tasks/p:Task", NS)
    summary_names: dict[str, str] = {}
    summaries = []
    for node in raw_tasks:
        if child_int(node, "Summary", 0):
            outline = child_text(node, "OutlineNumber")
            summary_names[outline] = child_text(node, "Name")
            summaries.append(
                {
                    "uid": child_int(node, "UID", -1),
                    "id": child_int(node, "ID", -1),
                    "outline": outline,
                    "level": child_int(node, "OutlineLevel", 0),
                    "name": child_text(node, "Name"),
                    "start": child_text(node, "Start"),
                    "finish": child_text(node, "Finish"),
                    "total_slack_days": tenths_minute_to_days(child_text(node, "TotalSlack"), minutes_per_day),
                    "critical": "是" if child_int(node, "Critical", 0) else "否",
                }
            )

    def parent_summary(outline: str) -> str:
        pieces = outline.split(".")
        for length in range(len(pieces) - 1, 0, -1):
            candidate = ".".join(pieces[:length])
            if candidate in summary_names:
                return summary_names[candidate]
        return ""

    tasks_all: dict[int, Task] = {}
    links: list[Link] = []
    link_index = 0
    for node in raw_tasks:
        uid = child_int(node, "UID", -1)
        outline = child_text(node, "OutlineNumber")
        duration_hours = iso_duration_hours(child_text(node, "Duration"))
        remaining_hours = iso_duration_hours(child_text(node, "RemainingDuration"))
        task = Task(
            uid=uid,
            task_id=child_int(node, "ID", -1),
            name=child_text(node, "Name"),
            wbs=child_text(node, "WBS"),
            outline_number=outline,
            outline_level=child_int(node, "OutlineLevel", 0),
            parent_summary=parent_summary(outline),
            summary=bool(child_int(node, "Summary", 0)),
            milestone=bool(child_int(node, "Milestone", 0)) or (duration_hours is not None and math.isclose(duration_hours, 0)),
            manual=bool(child_int(node, "Manual", 0)),
            critical=bool(child_int(node, "Critical", 0)),
            duration_days=(duration_hours * 60 / minutes_per_day) if duration_hours is not None else None,
            remaining_days=(remaining_hours * 60 / minutes_per_day) if remaining_hours is not None else None,
            percent_complete=child_int(node, "PercentComplete", 0),
            physical_percent_complete=child_int(node, "PhysicalPercentComplete", 0),
            start=child_text(node, "Start"),
            finish=child_text(node, "Finish"),
            early_start=child_text(node, "EarlyStart"),
            early_finish=child_text(node, "EarlyFinish"),
            late_start=child_text(node, "LateStart"),
            late_finish=child_text(node, "LateFinish"),
            actual_start=child_text(node, "ActualStart"),
            actual_finish=child_text(node, "ActualFinish"),
            free_slack_days=tenths_minute_to_days(child_text(node, "FreeSlack"), minutes_per_day),
            total_slack_days=tenths_minute_to_days(child_text(node, "TotalSlack"), minutes_per_day),
            constraint_type_code=child_int(node, "ConstraintType", 0),
            constraint_date=child_text(node, "ConstraintDate"),
            calendar_uid=child_int(node, "CalendarUID", -1),
            is_null=bool(child_int(node, "IsNull", 0)),
            external=bool(child_int(node, "ExternalTask", 0)),
            zone=zone_from_outline(outline, summary_names),
        )
        tasks_all[uid] = task
        for pred_node in node.findall("p:PredecessorLink", NS):
            link_index += 1
            relation_code = child_int(pred_node, "Type", 1)
            links.append(
                Link(
                    index=link_index,
                    pred_uid=child_int(pred_node, "PredecessorUID", -1),
                    succ_uid=uid,
                    relation=RELATION_TYPES.get(relation_code, f"TYPE{relation_code}"),
                    lag_days=tenths_minute_to_days(child_text(pred_node, "LinkLag"), minutes_per_day),
                    lag_format=child_int(pred_node, "LagFormat", -1),
                    cross_project=bool(child_int(pred_node, "CrossProject", 0)),
                )
            )

    tasks = {
        uid: task for uid, task in tasks_all.items()
        if uid != 0 and not task.summary and not task.is_null
    }
    valid_links = [link for link in links if link.pred_uid in tasks and link.succ_uid in tasks]
    for link in valid_links:
        pred = tasks[link.pred_uid]
        succ = tasks[link.succ_uid]
        link.critical = bool(pred.critical and succ.critical)
        succ.predecessor_uids.append(pred.uid)
        pred.successor_uids.append(succ.uid)
        succ.predecessor_labels.append(f"{pred.task_id}{link.label}")
        pred.successor_labels.append(f"{succ.task_id}{link.label}")

    assign_topological_ranks(tasks, valid_links)
    issues = build_issues(project, tasks, valid_links, calendars, critical_limit_days)
    return ProjectModel(
        source_path=path,
        project=project,
        tasks_all=tasks_all,
        tasks=tasks,
        links=valid_links,
        summaries=summaries,
        calendars=calendars,
        calendar_exceptions=calendar_exceptions,
        issues=issues,
        minutes_per_day=minutes_per_day,
        critical_limit_days=critical_limit_days,
    )


def assign_topological_ranks(tasks: dict[int, Task], links: list[Link]) -> None:
    predecessors: dict[int, list[int]] = {uid: [] for uid in tasks}
    successors: dict[int, list[int]] = {uid: [] for uid in tasks}
    for link in links:
        predecessors[link.succ_uid].append(link.pred_uid)
        successors[link.pred_uid].append(link.succ_uid)
    indegree = {uid: len(values) for uid, values in predecessors.items()}
    queue = collections.deque(sorted((uid for uid, value in indegree.items() if value == 0), key=lambda uid: tasks[uid].task_id))
    ranks = {uid: 0 for uid in queue}
    visited = 0
    while queue:
        uid = queue.popleft()
        visited += 1
        tasks[uid].rank = ranks[uid]
        for succ_uid in successors[uid]:
            ranks[succ_uid] = max(ranks.get(succ_uid, 0), ranks[uid] + 1)
            indegree[succ_uid] -= 1
            if indegree[succ_uid] == 0:
                queue.append(succ_uid)
    if visited != len(tasks):
        cycle_uids = [uid for uid, value in indegree.items() if value > 0]
        raise ValueError(f"排程存在循環邏輯，涉及 UID: {cycle_uids[:20]}")


def build_issues(
    project: dict,
    tasks: dict[int, Task],
    links: list[Link],
    calendars: list[dict],
    critical_limit_days: float,
) -> list[dict]:
    issues: list[dict] = []

    def add(severity: str, category: str, task: Task | None, detail: str, recommendation: str) -> None:
        issues.append(
            {
                "severity": severity,
                "category": category,
                "task_id": task.task_id if task else None,
                "uid": task.uid if task else None,
                "wbs": task.wbs if task else "",
                "task_name": task.name if task else "",
                "detail": detail,
                "recommendation": recommendation,
            }
        )

    start_date = parse_datetime(project.get("StartDate", ""))
    current_date = parse_datetime(project.get("CurrentDate", ""))
    status_date = parse_datetime(project.get("StatusDate", ""))
    if not status_date:
        add("需確認", "資料日期", None, "Project 未設定狀態日期（Status Date）。", "若本表用於進度更新，請在 Project 設定狀態日期後重新匯出 XML。")
    if start_date and current_date and current_date > start_date and all(t.percent_complete == 0 for t in tasks.values()):
        add("需確認", "實際進度", None, f"專案目前日期晚於計畫開工日，但 {len(tasks)} 個作業的完成百分比皆為 0%。", "確認本檔是否為純計畫版；若為進度版，請更新實際開始、完成及剩餘工期。")
    for calendar in calendars:
        if calendar["working_weekdays"] == 7:
            add("需確認", "工作日曆", None, f"日曆「{calendar['name']}」設定每週 7 天皆為工作日。", "確認工期是否確實採全年無休 8 小時工作制。")
    if not any(task.milestone for task in tasks.values()):
        add("提醒", "里程碑", None, "排程中沒有 0 工期里程碑作業。", "可視管理需求增加開工、結構完成、申請使用執照及驗收等里程碑。")

    for task in sorted(tasks.values(), key=lambda item: item.task_id):
        if not task.predecessor_uids:
            severity = "重要" if task.critical else "需確認"
            add(severity, "開放起點", task, "作業沒有前置關係。", "確認是否為合法起始作業；否則補上前置關係或共同開工里程碑。")
        if not task.successor_uids:
            severity = "重要" if task.critical else "需確認"
            add(severity, "開放終點", task, "作業沒有後續關係。", "確認是否為合法完工端點；否則連至後續作業或共同完工里程碑。")
        if task.manual:
            add("需確認", "手動排程", task, "作業採手動排程，邏輯關係變更時日期可能不會完整連動。", "若無特殊理由，建議改為自動排程後重新計算。")
        if task.constraint_type_code not in (0, 1):
            detail = f"限制類型：{task.constraint_type}；限制日期：{date_only(task.constraint_date) or '未填'}。"
            recommendation = "確認限制日期具有契約或施工依據，避免限制條件遮蔽真正的邏輯要徑。"
            constraint_dt = parse_datetime(task.constraint_date)
            if start_date and constraint_dt and constraint_dt < start_date:
                detail += " 限制日期早於本專案計畫開工日。"
                recommendation = "優先確認此限制日期是否為沿用舊版排程的殘留設定。"
            add("需確認", "日期限制", task, detail, recommendation)
        slack_critical = task.total_slack_days is not None and task.total_slack_days <= critical_limit_days + 1e-9
        if slack_critical != task.critical:
            add(
                "重要",
                "要徑一致性",
                task,
                f"Project Critical={task.critical}，總浮時={compact_number(task.total_slack_days)}天。",
                "在 Project 重新計算整份排程後再匯出 XML。",
            )
    return issues


def task_issue_text(task: Task, issues: list[dict]) -> str:
    categories = [row["category"] for row in issues if row["uid"] == task.uid]
    return "、".join(dict.fromkeys(categories))


def model_to_json(model: ProjectModel) -> dict:
    task_rows = []
    for index, task in enumerate(sorted(model.tasks.values(), key=lambda item: item.task_id), start=1):
        task_rows.append(
            {
                "seq": index,
                "id": task.task_id,
                "uid": task.uid,
                "wbs": task.wbs,
                "outline_level": task.outline_level,
                "parent_summary": task.parent_summary,
                "zone": task.zone,
                "name": task.name,
                "duration_days": task.duration_days,
                "remaining_days": task.remaining_days,
                "start": task.start,
                "finish": task.finish,
                "early_start": task.early_start,
                "early_finish": task.early_finish,
                "late_start": task.late_start,
                "late_finish": task.late_finish,
                "free_slack_days": task.free_slack_days,
                "total_slack_days": task.total_slack_days,
                "project_critical": "是" if task.critical else "否",
                "slack_critical": "是" if task.total_slack_days is not None and task.total_slack_days <= model.critical_limit_days + 1e-9 else "否",
                "critical_check": "一致" if task.critical == (task.total_slack_days is not None and task.total_slack_days <= model.critical_limit_days + 1e-9) else "檢查",
                "predecessor_count": len(task.predecessor_uids),
                "successor_count": len(task.successor_uids),
                "predecessors": "、".join(task.predecessor_labels),
                "successors": "、".join(task.successor_labels),
                "manual": "是" if task.manual else "否",
                "milestone": "是" if task.milestone else "否",
                "constraint_type": task.constraint_type,
                "constraint_date": task.constraint_date,
                "percent_complete": task.percent_complete / 100,
                "actual_start": task.actual_start,
                "actual_finish": task.actual_finish,
                "open_start": "是" if not task.predecessor_uids else "否",
                "open_finish": "是" if not task.successor_uids else "否",
                "issue": task_issue_text(task, model.issues),
                "cad_rank": task.rank,
                "cad_row": task.row,
            }
        )

    link_rows = []
    for link in model.links:
        pred = model.tasks[link.pred_uid]
        succ = model.tasks[link.succ_uid]
        link_rows.append(
            {
                "index": link.index,
                "pred_uid": pred.uid,
                "pred_id": pred.task_id,
                "pred_wbs": pred.wbs,
                "pred_name": pred.name,
                "succ_uid": succ.uid,
                "succ_id": succ.task_id,
                "succ_wbs": succ.wbs,
                "succ_name": succ.name,
                "type": link.relation,
                "lag_days": link.lag_days,
                "label": link.label,
                "critical": "是" if link.critical else "否",
                "cross_project": "是" if link.cross_project else "否",
                "validation": "正常",
            }
        )

    weekly_rows = []
    for calendar in model.calendars:
        for row in calendar["weekly"]:
            weekly_rows.append(
                {
                    "calendar_uid": calendar["uid"],
                    "calendar_name": calendar["name"],
                    **row,
                }
            )
    issue_counts = collections.Counter((row["severity"], row["category"]) for row in model.issues)
    return {
        "source_file": model.source_path.name,
        "project": model.project,
        "minutes_per_day": model.minutes_per_day,
        "critical_limit_days": model.critical_limit_days,
        "tasks": task_rows,
        "links": link_rows,
        "summaries": model.summaries,
        "calendars": [
            {key: value for key, value in calendar.items() if key != "weekly"}
            for calendar in model.calendars
        ],
        "calendar_weekly": weekly_rows,
        "calendar_exceptions": model.calendar_exceptions,
        "issues": model.issues,
        "issue_counts": [
            {"severity": severity, "category": category, "count": count}
            for (severity, category), count in sorted(issue_counts.items())
        ],
        "metrics": {
            "tasks": len(model.tasks),
            "links": len(model.links),
            "critical_tasks": sum(task.critical for task in model.tasks.values()),
            "critical_links": sum(link.critical for link in model.links),
            "open_starts": sum(not task.predecessor_uids for task in model.tasks.values()),
            "open_finishes": sum(not task.successor_uids for task in model.tasks.values()),
            "manual_tasks": sum(task.manual for task in model.tasks.values()),
            "milestones": sum(task.milestone for task in model.tasks.values()),
            "constrained_tasks": sum(task.constraint_type_code not in (0, 1) for task in model.tasks.values()),
            "nonzero_lag_links": sum(not math.isclose(link.lag_days or 0, 0) for link in model.links),
            "max_rank": max(task.rank for task in model.tasks.values()),
        },
    }


@dataclass
class DrawingNode:
    task: Task
    x: float
    y: float
    boundary: bool = False


@dataclass
class DrawingLayout:
    title: str
    nodes: dict[int, DrawingNode]
    links: list[Link]
    lane_ranges: dict[str, tuple[float, float]]
    width: float
    height: float
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    time_axis: list[tuple[str, float]] = field(default_factory=list)
    time_axis_title: str = ""


def subset_graph(model: ProjectModel, zones: set[str] | None) -> tuple[dict[int, Task], list[Link], set[int]]:
    if not zones:
        return dict(model.tasks), list(model.links), set()
    core = {uid for uid, task in model.tasks.items() if task.zone in zones}
    boundary: set[int] = set()
    for link in model.links:
        if link.pred_uid in core and link.succ_uid not in core:
            boundary.add(link.succ_uid)
        if link.succ_uid in core and link.pred_uid not in core:
            boundary.add(link.pred_uid)
    selected = core | boundary
    tasks = {uid: model.tasks[uid] for uid in selected}
    links = [
        link for link in model.links
        if link.pred_uid in selected and link.succ_uid in selected
        and (link.pred_uid in core or link.succ_uid in core)
    ]
    return tasks, links, boundary


def build_layout(model: ProjectModel, title: str, zones: set[str] | None = None) -> DrawingLayout:
    tasks, links, boundary = subset_graph(model, zones)
    original_ranks = sorted({task.rank for task in tasks.values()})
    rank_map = {rank: index for index, rank in enumerate(original_ranks)}
    by_rank: dict[int, list[int]] = collections.defaultdict(list)
    for uid, task in tasks.items():
        by_rank[rank_map[task.rank]].append(uid)

    pred_map: dict[int, list[int]] = collections.defaultdict(list)
    succ_map: dict[int, list[int]] = collections.defaultdict(list)
    for link in links:
        pred_map[link.succ_uid].append(link.pred_uid)
        succ_map[link.pred_uid].append(link.succ_uid)

    zone_order = sorted(
        {task.zone for task in tasks.values()},
        key=lambda zone: min(task.task_id for task in tasks.values() if task.zone == zone),
    )
    zone_index = {zone: index for index, zone in enumerate(zone_order)}
    for rank in by_rank:
        by_rank[rank].sort(key=lambda uid: (zone_index.get(tasks[uid].zone, 99), tasks[uid].task_id))

    positions: dict[int, float] = {}
    for rank in sorted(by_rank):
        for index, uid in enumerate(by_rank[rank]):
            positions[uid] = index
    for _ in range(6):
        for rank in sorted(by_rank):
            if rank == 0:
                continue
            by_rank[rank].sort(
                key=lambda uid: (
                    zone_index.get(tasks[uid].zone, 99),
                    sum(positions[pred] for pred in pred_map[uid] if pred in positions) / max(1, sum(pred in positions for pred in pred_map[uid])),
                    tasks[uid].task_id,
                )
            )
            for index, uid in enumerate(by_rank[rank]):
                positions[uid] = index
        for rank in sorted(by_rank, reverse=True):
            if rank == max(by_rank):
                continue
            by_rank[rank].sort(
                key=lambda uid: (
                    zone_index.get(tasks[uid].zone, 99),
                    sum(positions[succ] for succ in succ_map[uid] if succ in positions) / max(1, sum(succ in positions for succ in succ_map[uid])),
                    tasks[uid].task_id,
                )
            )
            for index, uid in enumerate(by_rank[rank]):
                positions[uid] = index

    node_width = 120.0
    node_height = 50.0
    x_spacing = 155.0
    y_spacing = 68.0
    lane_gap = 34.0
    lane_ranges: dict[str, tuple[float, float]] = {}
    lane_start_rows: dict[str, float] = {}

    active_zones = zone_order
    if zones:
        active_zones = ["分區網圖"]
        lane_start_rows["分區網圖"] = 0
    else:
        cursor = 0.0
        for zone in active_zones:
            max_count = max(sum(tasks[uid].zone == zone for uid in uids) for uids in by_rank.values())
            lane_start_rows[zone] = cursor
            lane_top = -cursor * y_spacing
            cursor += max(1, max_count)
            lane_bottom = -(cursor - 1) * y_spacing - node_height
            lane_ranges[zone] = (lane_top, lane_bottom)
            cursor += lane_gap / y_spacing

    nodes: dict[int, DrawingNode] = {}
    for rank in sorted(by_rank):
        if zones:
            ordered = by_rank[rank]
            for row_index, uid in enumerate(ordered):
                task = tasks[uid]
                task.row = row_index
                nodes[uid] = DrawingNode(task, rank * x_spacing, -row_index * y_spacing, uid in boundary)
        else:
            zone_counters = collections.Counter()
            for uid in by_rank[rank]:
                task = tasks[uid]
                row_index = lane_start_rows[task.zone] + zone_counters[task.zone]
                zone_counters[task.zone] += 1
                task.row = int(round(row_index))
                nodes[uid] = DrawingNode(task, rank * x_spacing, -row_index * y_spacing, uid in boundary)

    min_x = min(node.x for node in nodes.values()) - 190
    max_x = max(node.x for node in nodes.values()) + node_width + 40
    min_y = min(node.y for node in nodes.values()) - node_height - 65
    max_y = max(node.y for node in nodes.values()) + 125
    return DrawingLayout(
        title=title,
        nodes=nodes,
        links=links,
        lane_ranges=lane_ranges,
        width=max_x - min_x,
        height=max_y - min_y,
        min_x=min_x,
        max_x=max_x,
        min_y=min_y,
        max_y=max_y,
    )


class DxfWriter:
    LAYERS = {
        "0": (7, "CONTINUOUS"),
        "AON_TITLE": (5, "CONTINUOUS"),
        "AON_LANE": (9, "DASHED"),
        "AON_NODE": (7, "CONTINUOUS"),
        "AON_CRITICAL": (1, "CONTINUOUS"),
        "AON_WARNING": (30, "CONTINUOUS"),
        "AON_BOUNDARY": (9, "DASHED"),
        "AON_TEXT": (7, "CONTINUOUS"),
        "AON_LINK_FS": (8, "CONTINUOUS"),
        "AON_LINK_SS": (4, "CONTINUOUS"),
        "AON_LINK_FF": (3, "CONTINUOUS"),
        "AON_LINK_SF": (6, "CONTINUOUS"),
        "AON_LINK_CRITICAL": (1, "CONTINUOUS"),
    }

    def __init__(self, layout: DrawingLayout):
        self.layout = layout
        self.entities: list[tuple[int, str | int | float]] = []

    def add(self, code: int, value: str | int | float) -> None:
        self.entities.append((code, value))

    def line(self, layer: str, x1: float, y1: float, x2: float, y2: float) -> None:
        for code, value in [
            (0, "LINE"), (8, layer),
            (10, x1), (20, y1), (30, 0), (11, x2), (21, y2), (31, 0),
        ]:
            self.add(code, value)

    def mtext(self, layer: str, x: float, y: float, width: float, height: float, text: str, attachment: int = 1) -> None:
        # Use basic TEXT entities instead of MTEXT for maximum DXF importer
        # compatibility.  Multiple logical lines are emitted separately.
        for line_index, line_text in enumerate(text.splitlines() or [""]):
            safe = line_text.replace("\\", "\\\\")
            baseline_y = y - line_index * height * 1.35
            for code, value in [
                (0, "TEXT"), (8, layer),
                (10, x), (20, baseline_y), (30, 0), (40, height), (1, safe),
                (50, 0.0), (41, 0.82), (7, "AON_TC"), (72, 0),
                (11, x), (21, baseline_y), (31, 0), (73, 0),
            ]:
                self.add(code, value)

    def solid(self, layer: str, points: list[tuple[float, float]]) -> None:
        p = points + [points[-1]] * (4 - len(points))
        self.add(0, "SOLID")
        self.add(8, layer)
        for index, (x, y) in enumerate(p[:4]):
            self.add(10 + index, x)
            self.add(20 + index, y)
            self.add(30 + index, 0)

    def rectangle(self, layer: str, x: float, y: float, width: float, height: float) -> None:
        self.line(layer, x, y, x + width, y)
        self.line(layer, x + width, y, x + width, y - height)
        self.line(layer, x + width, y - height, x, y - height)
        self.line(layer, x, y - height, x, y)

    def draw_node(self, node: DrawingNode) -> None:
        task = node.task
        x, y = node.x, node.y
        width, height = 120.0, 50.0
        issue = not task.predecessor_uids or not task.successor_uids or task.manual
        layer = "AON_BOUNDARY" if node.boundary else "AON_CRITICAL" if task.critical else "AON_WARNING" if issue else "AON_NODE"
        if task.milestone:
            cx, cy = x + width / 2, y - height / 2
            points = [(cx, y), (x + width, cy), (cx, y - height), (x, cy), (cx, y)]
            for first, second in zip(points, points[1:]):
                self.line(layer, first[0], first[1], second[0], second[1])
        else:
            self.rectangle(layer, x, y, width, height)
            self.line(layer, x, y - 9, x + width, y - 9)
            self.line(layer, x, y - 29, x + width, y - 29)
            self.line(layer, x, y - 39.5, x + width, y - 39.5)
            for split in (40, 80):
                self.line(layer, x + split, y - 29, x + split, y - height)
        prefix = "[外部] " if node.boundary else ""
        self.mtext("AON_TEXT", x + 2, y - 1.5, width - 4, 2.2, f"{prefix}ID {task.task_id}  |  WBS {task.wbs}")
        name_lines = wrap_display(task.name, 38, 2)
        self.mtext("AON_TEXT", x + 2, y - 11, width - 4, 2.8, "\n".join(name_lines))
        self.mtext("AON_TEXT", x + 1.5, y - 31, 37, 2.0, f"ES {date_only(task.early_start)}")
        self.mtext("AON_TEXT", x + 41.5, y - 31, 37, 2.0, f"D {compact_number(task.duration_days)}d")
        self.mtext("AON_TEXT", x + 81.5, y - 31, 37, 2.0, f"EF {date_only(task.early_finish)}")
        self.mtext("AON_TEXT", x + 1.5, y - 41.5, 37, 2.0, f"LS {date_only(task.late_start)}")
        self.mtext("AON_TEXT", x + 41.5, y - 41.5, 37, 2.0, f"TF {compact_number(task.total_slack_days)}d / FF {compact_number(task.free_slack_days)}d")
        self.mtext("AON_TEXT", x + 81.5, y - 41.5, 37, 2.0, f"LF {date_only(task.late_finish)}")

    def draw_arrow(self, link: Link, ordinal: int) -> None:
        pred = self.layout.nodes[link.pred_uid]
        succ = self.layout.nodes[link.succ_uid]
        width, height = 120.0, 50.0
        relation = link.relation
        layer = "AON_LINK_CRITICAL" if link.critical else f"AON_LINK_{relation}"
        if relation in ("FS", "FF"):
            sx = pred.x + width
        else:
            sx = pred.x
        sy = pred.y - height / 2
        if relation in ("FS", "SS"):
            tx = succ.x
            arrow_direction = 1
        else:
            tx = succ.x + width
            arrow_direction = -1
        ty = succ.y - height / 2

        track_offset = 7.0 + (ordinal % 5) * 1.4
        if relation == "FS":
            track_y = pred.y + track_offset
            bend1 = sx + 7.0 + (ordinal % 4) * 1.5
            bend2 = tx - 7.0 - (ordinal % 4) * 1.5
            points = [(sx, sy), (bend1, sy), (bend1, track_y), (bend2, track_y), (bend2, ty), (tx, ty)]
        elif relation == "SS":
            channel = min(sx, tx) - 10.0 - (ordinal % 5) * 2.0
            points = [(sx, sy), (channel, sy), (channel, ty), (tx, ty)]
        elif relation == "FF":
            channel = max(sx, tx) + 10.0 + (ordinal % 5) * 2.0
            points = [(sx, sy), (channel, sy), (channel, ty), (tx, ty)]
        else:
            track_y = pred.y + track_offset
            points = [(sx, sy), (sx - 8, sy), (sx - 8, track_y), (tx + 8, track_y), (tx + 8, ty), (tx, ty)]
        for first, second in zip(points, points[1:]):
            self.line(layer, first[0], first[1], second[0], second[1])
        size = 3.2
        if arrow_direction == 1:
            arrow = [(tx, ty), (tx - size, ty + size * 0.65), (tx - size, ty - size * 0.65)]
        else:
            arrow = [(tx, ty), (tx + size, ty + size * 0.65), (tx + size, ty - size * 0.65)]
        self.solid(layer, arrow)
        if link.label != "FS":
            label_x = (points[-2][0] + tx) / 2
            label_y = ty + 3.5
            self.mtext(layer, label_x, label_y, 24, 2.0, link.label)

    def build(self) -> None:
        self.mtext("AON_TITLE", self.layout.min_x + 5, self.layout.max_y - 5, self.layout.width - 10, 6.0, self.layout.title)
        self.mtext(
            "AON_TEXT",
            self.layout.min_x + 5,
            self.layout.max_y - 18,
            self.layout.width - 10,
            2.6,
            "節點：ID/WBS、作業名稱、ES/D/EF、LS/TF/FF/LF；紅色為 Project 要徑；橘色為開放端點或手動排程。",
        )
        for zone, (top, bottom) in self.layout.lane_ranges.items():
            self.mtext("AON_TITLE", self.layout.min_x + 5, top - 4, 175, 3.4, zone)
            self.line("AON_LANE", self.layout.min_x + 2, top + 10, self.layout.max_x - 2, top + 10)
            self.line("AON_LANE", self.layout.min_x + 2, bottom - 10, self.layout.max_x - 2, bottom - 10)
        for ordinal, link in enumerate(self.layout.links):
            self.draw_arrow(link, ordinal)
        for node in self.layout.nodes.values():
            self.draw_node(node)

    def save(self, path: Path) -> None:
        self.build()
        pairs: list[tuple[int, str | int | float]] = []
        view_center_x = (self.layout.min_x + self.layout.max_x) / 2
        view_center_y = (self.layout.min_y + self.layout.max_y) / 2
        view_size = max(self.layout.height, self.layout.width / 1.65) * 1.08
        pairs.extend([(0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1009"), (9, "$DWGCODEPAGE"), (3, "ANSI_950")])
        pairs.extend([(9, "$EXTMIN"), (10, self.layout.min_x), (20, self.layout.min_y), (30, 0)])
        pairs.extend([(9, "$EXTMAX"), (10, self.layout.max_x), (20, self.layout.max_y), (30, 0)])
        pairs.extend([(9, "$LIMMIN"), (10, self.layout.min_x), (20, self.layout.min_y)])
        pairs.extend([(9, "$LIMMAX"), (10, self.layout.max_x), (20, self.layout.max_y)])
        pairs.extend([(9, "$VIEWCTR"), (10, view_center_x), (20, view_center_y)])
        pairs.extend([(9, "$VIEWSIZE"), (40, view_size), (9, "$TILEMODE"), (70, 1), (9, "$LIMCHECK"), (70, 0), (0, "ENDSEC")])
        pairs.extend([(0, "SECTION"), (2, "TABLES")])
        pairs.extend([(0, "TABLE"), (2, "LTYPE"), (70, 2)])
        pairs.extend([(0, "LTYPE"), (2, "CONTINUOUS"), (70, 0), (3, "Solid line"), (72, 65), (73, 0), (40, 0.0)])
        pairs.extend([(0, "LTYPE"), (2, "DASHED"), (70, 0), (3, "Dashed"), (72, 65), (73, 2), (40, 6.0), (49, 3.0), (74, 0), (49, -3.0), (74, 0)])
        pairs.append((0, "ENDTAB"))
        pairs.extend([(0, "TABLE"), (2, "LAYER"), (70, len(self.LAYERS))])
        for name, (color, linetype) in self.LAYERS.items():
            pairs.extend([(0, "LAYER"), (2, name), (70, 0), (62, color), (6, linetype)])
        pairs.append((0, "ENDTAB"))
        pairs.extend([(0, "TABLE"), (2, "STYLE"), (70, 2)])
        pairs.extend([(0, "STYLE"), (2, "STANDARD"), (70, 0), (40, 0.0), (41, 1.0), (50, 0.0), (71, 0), (42, 2.5), (3, "txt"), (4, "")])
        pairs.extend([(0, "STYLE"), (2, "AON_TC"), (70, 0), (40, 0.0), (41, 1.0), (50, 0.0), (71, 0), (42, 2.5), (3, "msjh.ttc"), (4, "")])
        pairs.extend([(0, "ENDTAB"), (0, "ENDSEC")])
        pairs.extend([(0, "SECTION"), (2, "BLOCKS"), (0, "ENDSEC")])
        pairs.extend([(0, "SECTION"), (2, "ENTITIES")])
        pairs.extend(self.entities)
        pairs.extend([(0, "ENDSEC"), (0, "EOF")])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for code, value in pairs:
                if isinstance(value, float):
                    rendered = f"{value:.6f}".rstrip("0").rstrip(".")
                else:
                    rendered = str(value)
                    # R12 is ASCII/codepage based.  Unicode escape sequences
                    # keep Chinese text portable across AutoCAD codepages.
                    rendered = "".join(
                        char if 32 <= ord(char) <= 126 else f"\\U+{ord(char):04X}"
                        for char in rendered
                    )
                handle.write(f"{code}\n{rendered}\n")


def layout_to_svg(layout: DrawingLayout, path: Path) -> None:
    scale = 1.0
    margin = 20.0
    width = layout.width * scale + 2 * margin
    height = layout.height * scale + 2 * margin

    def sx(x: float) -> float:
        return (x - layout.min_x) * scale + margin

    def sy(y: float) -> float:
        return (layout.max_y - y) * scale + margin

    def line(x1: float, y1: float, x2: float, y2: float, color: str, dash: str = "") -> str:
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        return f'<line x1="{sx(x1):.1f}" y1="{sy(y1):.1f}" x2="{sx(x2):.1f}" y2="{sy(y2):.1f}" stroke="{color}" stroke-width="0.8"{dash_attr}/>'

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:"Microsoft JhengHei","Noto Sans CJK TC",Arial,sans-serif;fill:#111827}.small{font-size:2.1px}.tiny{font-size:1.8px}.title{font-size:6px;font-weight:700}.lane{font-size:3.4px;font-weight:700}</style>',
        f'<text x="{sx(layout.min_x + 5):.1f}" y="{sy(layout.max_y - 5):.1f}" class="title">{html.escape(layout.title)}</text>',
    ]
    for zone, (top, bottom) in layout.lane_ranges.items():
        parts.append(line(layout.min_x + 2, top + 10, layout.max_x - 2, top + 10, "#94A3B8", "5 4"))
        parts.append(line(layout.min_x + 2, bottom - 10, layout.max_x - 2, bottom - 10, "#94A3B8", "5 4"))
        parts.append(f'<text x="{sx(layout.min_x + 5):.1f}" y="{sy(top - 4):.1f}" class="lane">{html.escape(zone)}</text>')
    for link in layout.links:
        pred = layout.nodes[link.pred_uid]
        succ = layout.nodes[link.succ_uid]
        color = "#DC2626" if link.critical else {"FS": "#64748B", "SS": "#0891B2", "FF": "#16A34A", "SF": "#C026D3"}.get(link.relation, "#64748B")
        x1 = pred.x + (120 if link.relation in ("FS", "FF") else 0)
        y1 = pred.y - 25
        x2 = succ.x + (0 if link.relation in ("FS", "SS") else 120)
        y2 = succ.y - 25
        parts.append(line(x1, y1, x2, y2, color))
    for node in layout.nodes.values():
        task = node.task
        x, y = sx(node.x), sy(node.y)
        critical = task.critical
        issue = not task.predecessor_uids or not task.successor_uids or task.manual
        stroke = "#DC2626" if critical else "#F59E0B" if issue else "#334155"
        fill = "#F8FAFC" if node.boundary else ZONE_COLORS.get(task.zone, "#FFFFFF")
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="120" height="50" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')
        parts.append(line(node.x, node.y - 9, node.x + 120, node.y - 9, stroke))
        parts.append(line(node.x, node.y - 29, node.x + 120, node.y - 29, stroke))
        parts.append(line(node.x, node.y - 39.5, node.x + 120, node.y - 39.5, stroke))
        for split in (40, 80):
            parts.append(line(node.x + split, node.y - 29, node.x + split, node.y - 50, stroke))
        parts.append(f'<text x="{x+2:.1f}" y="{y+6.5:.1f}" class="small">ID {task.task_id} | WBS {html.escape(task.wbs)}</text>')
        for line_no, name_line in enumerate(wrap_display(task.name, 38, 2)):
            parts.append(f'<text x="{x+2:.1f}" y="{y+15+line_no*4:.1f}" class="small">{html.escape(name_line)}</text>')
        row1 = [f"ES {date_only(task.early_start)}", f"D {compact_number(task.duration_days)}d", f"EF {date_only(task.early_finish)}"]
        row2 = [f"LS {date_only(task.late_start)}", f"TF {compact_number(task.total_slack_days)}d", f"LF {date_only(task.late_finish)}"]
        for col, value in enumerate(row1):
            parts.append(f'<text x="{x+1.5+col*40:.1f}" y="{y+36:.1f}" class="tiny">{html.escape(value)}</text>')
        for col, value in enumerate(row2):
            parts.append(f'<text x="{x+1.5+col*40:.1f}" y="{y+47:.1f}" class="tiny">{html.escape(value)}</text>')
    parts.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")


def validate_dxf(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return {
        "file": path.name,
        "bytes": path.stat().st_size,
        "has_header": "\nSECTION\n2\nHEADER\n" in "\n" + text,
        "has_entities": "\nSECTION\n2\nENTITIES\n" in "\n" + text,
        "has_eof": text.rstrip().endswith("0\nEOF"),
        "line_entities": text.count("\nLINE\n"),
        "text_entities": text.count("\nTEXT\n"),
        "mtext_entities": text.count("\nMTEXT\n"),
        "solid_entities": text.count("\nSOLID\n"),
        "replacement_chars": text.count("�"),
    }


def make_outputs(model: ProjectModel, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    graph_specs = [
        ("AON_全工程", "富邦人壽新竹湖口開發新建工程｜AON全工程網圖", None),
        ("AON_A1區", "A1區｜AON分區網圖（含跨區邊界作業）", {"A1區"}),
        ("AON_A2區", "A2區｜AON分區網圖（含跨區邊界作業）", {"A2區"}),
        ("AON_B區", "B區｜AON分區網圖（含跨區邊界作業）", {"B區"}),
        ("AON_共同及驗收", "共同、前置及驗收｜AON分區網圖（含跨區邊界作業）", {"共同／前置", "其他共同工程", "驗收／送電"}),
    ]
    validations = []
    output_files = []
    prepared_layouts = []
    for stem, title, zones in graph_specs:
        prepared_layouts.append((stem, build_layout(model, title, zones)))
        if zones is None:
            # Preserve the overall-network row assignment in the audit workbook.
            overall_rows = {uid: node.task.row for uid, node in prepared_layouts[-1][1].nodes.items()}

    for uid, row in overall_rows.items():
        model.tasks[uid].row = row
    data = model_to_json(model)
    data_path = output_dir / "aon_project_data.json"
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    for stem, layout in prepared_layouts:
        dxf_path = output_dir / f"{stem}.dxf"
        svg_path = output_dir / f"{stem}.svg"
        DxfWriter(layout).save(dxf_path)
        layout_to_svg(layout, svg_path)
        validations.append(validate_dxf(dxf_path))
        output_files.extend([dxf_path.name, svg_path.name])
    validation_path = output_dir / "dxf_validation.json"
    validation_path.write_text(json.dumps(validations, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "data": data_path.name,
        "validation": validation_path.name,
        "files": output_files,
        "dxf_validation": validations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path, help="Microsoft Project XML file")
    parser.add_argument("--output", type=Path, required=True, help="Output directory")
    args = parser.parse_args()
    model = parse_project(args.xml)
    result = make_outputs(model, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

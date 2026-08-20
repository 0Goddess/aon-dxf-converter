#!/usr/bin/env python3
"""Windows GUI and command-line wrapper for the Project XML to AON DXF converter."""

from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import re
import sys
import tempfile
import threading
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import ezdxf

from outline_dxf_unicode_text import convert as outline_unicode_text
from project_aon_converter import build_layout, parse_project
from project_aon_ezdxf import (
    EzdxfAonWriter,
    NODE_HEIGHT,
    NODE_WIDTH,
    X_SPACING,
    Y_SPACING,
    enlarge_layout,
)


APP_NAME = "Project XML 轉 AON DXF"
APP_VERSION = "2.0.9"
OUTPUT_SUFFIX = "_AON全區_AutoCAD2023.dxf"


def resource_path(relative: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / relative


FONT_FILE = resource_path("assets/NotoSansTC-Regular.ttf")


@dataclass
class ConversionResult:
    output_file: str
    file_size_bytes: int
    tasks: int
    links: int
    critical_tasks: int
    direct_links: int
    two_turn_links: int
    four_turn_links: int
    audit_errors: int


def safe_stem(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned or "Project"


def output_path_for(xml_path: Path, output_directory: Path) -> Path:
    return output_directory / f"{safe_stem(xml_path.stem)}{OUTPUT_SUFFIX}"


def insert_route_column_gap(layout, link_index: int, gap: float = X_SPACING) -> bool:
    """Insert one complete blank time column immediately before a blocked target."""
    link = next((item for item in layout.links if item.index == link_index), None)
    if link is None:
        return False
    pred = layout.nodes[link.pred_uid]
    succ = layout.nodes[link.succ_uid]
    if succ.x <= pred.x:
        return False

    threshold = succ.x
    moved = False
    for node in layout.nodes.values():
        if node.x >= threshold - 1e-6:
            node.x += gap
            moved = True
    if not moved:
        return False

    layout.time_axis = [
        (label, center + gap if center >= threshold - 1e-6 else center)
        for label, center in layout.time_axis
    ]
    layout.time_boundaries = [
        boundary + gap if boundary >= threshold - 1e-6 else boundary
        for boundary in layout.time_boundaries
    ]
    layout.max_x += gap
    layout.width += gap
    stats = getattr(layout, "optimization_stats", None)
    if isinstance(stats, dict):
        stats["route_gap_columns"] = stats.get("route_gap_columns", 0) + 1
        stats.setdefault("route_gap_links", []).append(link_index)
    return True


def insert_route_row_gap(layout, link_index: int, gap: float = Y_SPACING) -> bool:
    """Insert one complete blank row toward the blocked target."""
    link = next((item for item in layout.links if item.index == link_index), None)
    if link is None:
        return False
    pred = layout.nodes[link.pred_uid]
    succ = layout.nodes[link.succ_uid]
    same_row = abs(succ.y - pred.y) < 1e-6
    if same_row:
        row_y = pred.y
        span_left = min(pred.x, succ.x) - X_SPACING * 0.25
        span_right = max(pred.x, succ.x) + X_SPACING * 0.25

        def side_nodes(upward: bool):
            return [
                node
                for node in layout.nodes.values()
                if (node.y > row_y + 1e-6 if upward else node.y < row_y - 1e-6)
            ]

        def side_score(upward: bool) -> tuple[int, int, int]:
            nodes = side_nodes(upward)
            critical_moves = sum(bool(node.task.critical) for node in nodes)
            corridor_occupancy = sum(
                span_left <= node.x <= span_right for node in nodes
            )
            return critical_moves, corridor_occupancy, len(nodes)

        upward_score = side_score(True)
        downward_score = side_score(False)
        upward = upward_score <= downward_score
        movable_nodes = side_nodes(upward)
        if not movable_nodes:
            # An empty side already provides an open outer corridor. Moving
            # the occupied side would not enlarge that corridor and could
            # disturb a critical row, so report no effective vertical change.
            return False
        threshold = row_y
    else:
        upward = succ.y > pred.y
        threshold = succ.y

    moved = False
    for node in layout.nodes.values():
        if upward and (
            node.y > threshold + 1e-6
            if same_row
            else node.y >= threshold - 1e-6
        ):
            node.y += gap
            node.task.row += 1
            moved = True
        elif not upward and (
            node.y < threshold - 1e-6
            if same_row
            else node.y <= threshold + 1e-6
        ):
            node.y -= gap
            node.task.row -= 1
            moved = True
    if not moved:
        return False

    layout.lane_ranges = {}
    zones = sorted({node.task.zone for node in layout.nodes.values()})
    for zone in zones:
        zone_nodes = [
            node
            for node in layout.nodes.values()
            if node.task.zone == zone and not node.task.critical
        ]
        if zone_nodes:
            layout.lane_ranges[zone] = (
                max(node.y for node in zone_nodes),
                min(node.y for node in zone_nodes) - NODE_HEIGHT,
            )
    layout.min_y = min(node.y for node in layout.nodes.values()) - NODE_HEIGHT - 110.0
    layout.max_y = max(node.y for node in layout.nodes.values()) + 250.0
    layout.height = layout.max_y - layout.min_y
    stats = getattr(layout, "optimization_stats", None)
    if isinstance(stats, dict):
        stats["route_gap_rows"] = stats.get("route_gap_rows", 0) + 1
        stats.setdefault("route_gap_row_links", []).append(link_index)
    return True


def estimate_route_gap_batch(layout, link_index: int) -> tuple[int, int]:
    """Estimate a useful multi-column/multi-row expansion for one blocked link."""
    link = next((item for item in layout.links if item.index == link_index), None)
    if link is None:
        return 2, 2
    pred = layout.nodes[link.pred_uid]
    succ = layout.nodes[link.succ_uid]
    span_left = min(pred.x, succ.x) - X_SPACING * 0.25
    span_right = max(pred.x, succ.x) + X_SPACING * 0.25
    span_nodes = [
        node
        for uid, node in layout.nodes.items()
        if uid not in {link.pred_uid, link.succ_uid}
        and span_left <= node.x <= span_right
    ]

    if abs(succ.y - pred.y) < 1e-6:
        row_y = pred.y
        upper_rows = {
            round(node.y / Y_SPACING)
            for node in span_nodes
            if node.y > row_y + 1e-6 and not node.task.critical
        }
        lower_rows = {
            round(node.y / Y_SPACING)
            for node in span_nodes
            if node.y < row_y - 1e-6 and not node.task.critical
        }
        usable_counts = [len(rows) for rows in (upper_rows, lower_rows) if rows]
        vertical = min(usable_counts) + 1 if usable_counts else 2
    else:
        low_y = min(pred.y, succ.y)
        high_y = max(pred.y, succ.y)
        occupied_rows = {
            round(node.y / Y_SPACING)
            for node in span_nodes
            if low_y - Y_SPACING <= node.y <= high_y + Y_SPACING
        }
        vertical = len(occupied_rows) + 1

    same_source_links = sum(
        1 for item in layout.links if item.pred_uid == link.pred_uid
    )
    horizontal = 2 if same_source_links <= 3 else 3
    return max(2, horizontal), max(2, vertical)


def expanded_layout_copy(
    layout,
    link_index: int,
    horizontal_count: int,
    vertical_count: int,
):
    """Apply a batch expansion to a disposable layout copy."""
    candidate = copy.deepcopy(layout)
    horizontal_done = 0
    vertical_done = 0
    for _ in range(horizontal_count):
        if not insert_route_column_gap(candidate, link_index):
            break
        horizontal_done += 1
    for _ in range(vertical_count):
        if not insert_route_row_gap(candidate, link_index):
            break
        vertical_done += 1
    return candidate, horizontal_done, vertical_done


def analyze_and_expand_route(
    layout,
    link_index: int,
    update: Callable[[int, str], None],
):
    """Test a bounded set of geometry-based batches on disposable copies."""
    seed_horizontal, seed_vertical = estimate_route_gap_batch(layout, link_index)
    update(
        43,
        f"關係 {link_index} H1、V1 後仍無合法通道，分析工作框與通道占用…",
    )
    candidates = (
        (seed_horizontal, seed_vertical),
        (seed_horizontal, 0),
        (0, seed_vertical),
    )
    tested: set[tuple[int, int]] = set()
    for requested_horizontal, requested_vertical in candidates:
        if (requested_horizontal, requested_vertical) in tested:
            continue
        tested.add((requested_horizontal, requested_vertical))
        candidate, horizontal_done, vertical_done = expanded_layout_copy(
            layout,
            link_index,
            requested_horizontal,
            requested_vertical,
        )
        if horizontal_done == 0 and vertical_done == 0:
            continue
        update(
            43,
            f"關係 {link_index} 分析候選：水平 +{horizontal_done} 格、"
            f"垂直 +{vertical_done} 格，驗證完整排線…",
        )
        try:
            writer = EzdxfAonWriter(candidate)
            return candidate, writer, horizontal_done, vertical_done, None
        except RuntimeError as error:
            match = re.fullmatch(r"No clear final route for link (\d+)", str(error))
            if match is None:
                raise
            next_blocked_link = int(match.group(1))
            if next_blocked_link != link_index:
                # The original blockage is cleared. Keep this batch and let
                # the outer loop analyze the newly exposed relationship.
                return (
                    candidate,
                    None,
                    horizontal_done,
                    vertical_done,
                    next_blocked_link,
                )
    return layout, None, 0, 0, link_index


def relocate_blocked_branch(layout, link_index: int) -> tuple[bool, int]:
    """Move a blocked non-critical branch to the best open row in one pass.

    V2.0.7 stopped after testing only the nearest collision-free row.  That row
    can still leave the relationship's vertical corridor inside the same dense
    fan.  Evaluate all bounded, collision-free rows first and prefer the row
    with the fewest workboxes in the relationship corridor; distance is only a
    tie breaker.  Critical tasks are never part of the movable component.
    """
    link = next((item for item in layout.links if item.index == link_index), None)
    if link is None:
        return False, 0

    successors: dict[str, list[str]] = {}
    predecessors: dict[str, list[str]] = {}
    for item in layout.links:
        successors.setdefault(item.pred_uid, []).append(item.succ_uid)
        predecessors.setdefault(item.succ_uid, []).append(item.pred_uid)

    if not layout.nodes[link.succ_uid].task.critical:
        anchor_uid = link.succ_uid
        graph = successors
    elif not layout.nodes[link.pred_uid].task.critical:
        anchor_uid = link.pred_uid
        graph = predecessors
    else:
        return False, 0

    component: set[str] = set()
    stack = [anchor_uid]
    while stack:
        uid = stack.pop()
        if uid in component or layout.nodes[uid].task.critical:
            continue
        component.add(uid)
        stack.extend(graph.get(uid, ()))
    if not component:
        return False, 0

    anchor = layout.nodes[anchor_uid]
    if anchor.y > 1e-6:
        directions = (1, -1)
    elif anchor.y < -1e-6:
        directions = (-1, 1)
    else:
        upper_count = sum(node.y > 1e-6 for node in layout.nodes.values())
        lower_count = sum(node.y < -1e-6 for node in layout.nodes.values())
        directions = (1, -1) if upper_count <= lower_count else (-1, 1)

    fixed = [
        node for uid, node in layout.nodes.items() if uid not in component
    ]
    occupied_rows = {node.task.row for node in layout.nodes.values()}
    max_steps = max(2, len(occupied_rows) + 1)

    candidates: list[tuple[tuple[int, int, int], int, int]] = []
    for direction_rank, direction in enumerate(directions):
        for steps in range(1, max_steps + 1):
            delta_y = direction * steps * Y_SPACING
            collision = False
            for uid in component:
                moved = layout.nodes[uid]
                new_y = moved.y + delta_y
                if any(
                    abs(moved.x - other.x) < NODE_WIDTH - 1e-6
                    and abs(new_y - other.y) < NODE_HEIGHT + 1e-6
                    for other in fixed
                ):
                    collision = True
                    break
            if collision:
                continue

            pred = layout.nodes[link.pred_uid]
            succ = layout.nodes[link.succ_uid]
            pred_y = pred.y + (delta_y if link.pred_uid in component else 0.0)
            succ_y = succ.y + (delta_y if link.succ_uid in component else 0.0)
            left = min(pred.x, succ.x) - NODE_WIDTH * 0.15
            right = max(pred.x, succ.x) + NODE_WIDTH * 0.15
            low = min(pred_y, succ_y) - NODE_HEIGHT * 0.35
            high = max(pred_y, succ_y) + NODE_HEIGHT * 0.35
            corridor_blockers = sum(
                1
                for other in fixed
                if left < other.x + NODE_WIDTH / 2.0
                and right > other.x - NODE_WIDTH / 2.0
                and low < other.y + NODE_HEIGHT / 2.0
                and high > other.y - NODE_HEIGHT / 2.0
            )
            # Prefer a genuinely open corridor, then the smallest move.  The
            # original side preference is retained only as the final tie.
            candidates.append(
                ((corridor_blockers, steps, direction_rank), direction, steps)
            )

    if not candidates:
        return False, 0
    _, direction, steps = min(candidates, key=lambda item: item[0])
    delta_y = direction * steps * Y_SPACING
    for uid in component:
        node = layout.nodes[uid]
        node.y += delta_y
        node.task.row += direction * steps
    layout.lane_ranges = {}
    layout.min_y = min(node.y for node in layout.nodes.values()) - NODE_HEIGHT - 110.0
    layout.max_y = max(node.y for node in layout.nodes.values()) + 250.0
    layout.height = layout.max_y - layout.min_y
    stats = getattr(layout, "optimization_stats", None)
    if isinstance(stats, dict):
        stats["route_branch_relocations"] = (
            stats.get("route_branch_relocations", 0) + 1
        )
        stats.setdefault("route_branch_relocation_links", []).append(link_index)
    return True, direction * steps


def convert_project(
    xml_path: Path,
    output_directory: Path,
    *,
    overwrite: bool = False,
    time_scale: str = "auto",
    zone_level: str = "auto",
    progress: Callable[[int, str], None] | None = None,
) -> ConversionResult:
    def update(percent: int, message: str) -> None:
        if progress:
            progress(percent, message)

    xml_path = xml_path.resolve()
    output_directory = output_directory.resolve()
    if not xml_path.is_file():
        raise FileNotFoundError(f"找不到 Project XML：{xml_path}")
    if xml_path.suffix.lower() != ".xml":
        raise ValueError("輸入檔必須是 Microsoft Project 匯出的 XML 檔。")
    if not FONT_FILE.is_file():
        raise FileNotFoundError("程式缺少內建中文字型，請重新解壓縮完整程式包。")

    output_directory.mkdir(parents=True, exist_ok=True)
    final_path = output_path_for(xml_path, output_directory)
    if final_path.exists() and not overwrite:
        raise FileExistsError(f"輸出檔已存在：{final_path}")

    update(8, "讀取 Project XML…")
    model = parse_project(xml_path, zone_level=zone_level)
    if not model.tasks:
        raise ValueError("XML 中沒有可繪製的非摘要作業。")

    project_name = (
        model.project.get("Title")
        or model.project.get("Name")
        or xml_path.stem
    )
    update(25, f"建立 AON 網路與版面（{len(model.tasks)} 個作業）…")
    layout = enlarge_layout(
        build_layout(model, f"{project_name}｜AON全工程網圖", None),
        time_scale=time_scale,
    )

    with tempfile.TemporaryDirectory(prefix=".aon_build_", dir=output_directory) as temp_dir:
        temp_root = Path(temp_dir)
        source_dxf = temp_root / "AON_SOURCE_R2010.dxf"
        final_temp = temp_root / "AON_FINAL_AUTOCAD2023.dxf"

        update(43, f"繪製節點與 {len(layout.links)} 條關係線…")
        route_gap_attempts: dict[int, dict[str, int]] = {}
        while True:
            try:
                writer = EzdxfAonWriter(layout)
                break
            except RuntimeError as error:
                match = re.fullmatch(r"No clear final route for link (\d+)", str(error))
                if match is None:
                    raise
                blocked_link = int(match.group(1))
                attempts = route_gap_attempts.setdefault(
                    blocked_link,
                    {
                        "horizontal": 0,
                        "vertical": 0,
                        "horizontal_probed": 0,
                        "vertical_probed": 0,
                        "relayout_attempts": 0,
                    },
                )
                expanded = False
                for dimension, inserter in (
                    ("horizontal", insert_route_column_gap),
                    ("vertical", insert_route_row_gap),
                ):
                    probe_key = f"{dimension}_probed"
                    if attempts[probe_key]:
                        continue
                    attempts[probe_key] = 1
                    if not inserter(layout, blocked_link):
                        continue
                    attempts[dimension] += 1
                    label = (
                        "水平空間不足，插入 1 格空白欄"
                        if dimension == "horizontal"
                        else "垂直通道不足，插入 1 格空白列"
                    )
                    update(43, f"關係 {blocked_link} {label}後重新排線…")
                    expanded = True
                    break
                if expanded:
                    continue

                if attempts["horizontal_probed"] and attempts["vertical_probed"]:
                    if attempts["relayout_attempts"] >= 6:
                        raise RuntimeError(
                            f"No clear final route for link {blocked_link}; "
                            "bounded spacing analysis and local branch relocation failed"
                        )
                    (
                        layout,
                        analyzed_writer,
                        added_horizontal,
                        added_vertical,
                        next_blocked_link,
                    ) = analyze_and_expand_route(layout, blocked_link, update)
                    attempts["horizontal"] += added_horizontal
                    attempts["vertical"] += added_vertical
                    if analyzed_writer is not None:
                        update(
                            43,
                            f"關係 {blocked_link} 分析完成，一次套用水平 "
                            f"+{added_horizontal} 格、垂直 +{added_vertical} 格；"
                            "完整排線成功。",
                        )
                        writer = analyzed_writer
                        break
                    if next_blocked_link == blocked_link:
                        if attempts["relayout_attempts"] < 6:
                            attempts["relayout_attempts"] += 1
                            relocated, row_delta = relocate_blocked_branch(
                                layout, blocked_link
                            )
                            if relocated:
                                update(
                                    43,
                                    f"關係 {blocked_link} 有限擴格仍無解，"
                                    f"判定為局部排版問題；將所屬非要徑分支"
                                    f"一次選擇較開放通道並移動 {abs(row_delta)} 列"
                                    f"（候選 {attempts['relayout_attempts']}/6）後重新排線…",
                                )
                                continue
                        raise RuntimeError(
                            f"No clear final route for link {blocked_link}; "
                            "bounded spacing analysis and local branch relocation failed"
                        )
                    update(
                        43,
                        f"關係 {blocked_link} 已解除；一次套用水平 "
                        f"+{added_horizontal} 格、垂直 +{added_vertical} 格，"
                        f"接續分析關係 {next_blocked_link}…",
                    )
                    continue

                raise RuntimeError(
                    f"No clear final route for link {blocked_link}; "
                    "horizontal and vertical workbox spacing cannot be expanded"
                )
        source_info = writer.save(source_dxf)
        if source_info["audit_errors"]:
            raise RuntimeError("AON 原始 DXF 格式稽核失敗。")

        update(68, "將中文字轉為 CAD 向量線條…")
        outline_unicode_text(source_dxf, final_temp, FONT_FILE)

        update(91, "執行 AutoCAD 相容性與關係資料稽核…")
        document = ezdxf.readfile(final_temp)
        auditor = document.audit()
        if auditor.errors:
            raise RuntimeError(f"AutoCAD DXF 稽核發現 {len(auditor.errors)} 個錯誤。")

        modelspace = document.modelspace()
        link_entities = [
            entity
            for entity in modelspace.query("LWPOLYLINE")
            if entity.has_xdata("AON_LINK")
        ]
        if len(link_entities) != len(layout.links):
            raise RuntimeError(
                f"關係線物件數不符：預期 {len(layout.links)}，實際 {len(link_entities)}。"
            )
        for entity in modelspace.query("TEXT MTEXT ATTRIB ATTDEF"):
            content = getattr(entity.dxf, "text", "") or getattr(entity, "text", "")
            if any(ord(character) >= 128 for character in content):
                raise RuntimeError("仍有未向量化的中文字，已停止輸出。")

        final_temp.replace(final_path)

    route_stats = writer.route_stats
    update(100, "轉換完成。")
    return ConversionResult(
        output_file=str(final_path),
        file_size_bytes=final_path.stat().st_size,
        tasks=len(model.tasks),
        links=len(layout.links),
        critical_tasks=sum(task.critical for task in model.tasks.values()),
        direct_links=int(route_stats.get("direct_links", 0)),
        two_turn_links=int(route_stats.get("two_turn_links", 0)),
        four_turn_links=int(route_stats.get("four_turn_links", 0)),
        audit_errors=0,
    )


def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class ConverterWindow:
        def __init__(self) -> None:
            self.root = tk.Tk()
            self.root.title(f"{APP_NAME} {APP_VERSION}")
            self.root.geometry("820x590")
            self.root.minsize(720, 520)
            self.events: queue.Queue[tuple[str, object]] = queue.Queue()
            self.worker: threading.Thread | None = None

            style = ttk.Style(self.root)
            if "vista" in style.theme_names():
                style.theme_use("vista")

            self.xml_value = tk.StringVar()
            self.output_value = tk.StringVar()
            self.open_folder_value = tk.BooleanVar(value=True)
            self.time_scale_value = tk.StringVar(value="自動")
            self.zone_level_value = tk.StringVar(value="自動")
            self.status_value = tk.StringVar(value="請選擇 Microsoft Project XML。")
            self.progress_value = tk.IntVar(value=0)

            container = ttk.Frame(self.root, padding=18)
            container.pack(fill="both", expand=True)

            ttk.Label(container, text="Project XML 轉 AON 全區 DXF", font=("Microsoft JhengHei UI", 17, "bold")).pack(anchor="w")
            ttk.Label(
                container,
                text="輸出 AutoCAD 2023 可開啟的 DXF；中文字向量化，關係線為單一物件。",
            ).pack(anchor="w", pady=(3, 17))

            input_group = ttk.LabelFrame(container, text="1. 選擇 Project XML", padding=10)
            input_group.pack(fill="x")
            ttk.Entry(input_group, textvariable=self.xml_value).pack(side="left", fill="x", expand=True)
            ttk.Button(input_group, text="瀏覽…", command=self.choose_xml, width=11).pack(side="left", padx=(8, 0))

            output_group = ttk.LabelFrame(container, text="2. 輸出位置（預設與 XML 同資料夾）", padding=10)
            output_group.pack(fill="x", pady=(12, 0))
            ttk.Entry(output_group, textvariable=self.output_value).pack(side="left", fill="x", expand=True)
            ttk.Button(output_group, text="瀏覽…", command=self.choose_output, width=11).pack(side="left", padx=(8, 0))

            options = ttk.Frame(container)
            options.pack(fill="x", pady=(10, 0))
            ttk.Checkbutton(options, text="完成後開啟輸出資料夾", variable=self.open_folder_value).pack(side="left")
            ttk.Label(options, text="時間分隔：").pack(side="left", padx=(18, 4))
            ttk.Combobox(
                options,
                textvariable=self.time_scale_value,
                values=("自動", "週", "月", "季", "年", "關閉時間軸"),
                state="readonly",
                width=11,
            ).pack(side="left")
            ttk.Label(options, text="ES／EF／LS／LF／TF／FF 以 XML 計算結果為準").pack(side="right")

            zone_options = ttk.Frame(container)
            zone_options.pack(fill="x", pady=(8, 0))
            ttk.Label(zone_options, text="分區方式：").pack(side="left")
            ttk.Combobox(
                zone_options,
                textvariable=self.zone_level_value,
                values=("自動", "不分區", "WBS 第1層", "WBS 第2層", "WBS 第3層"),
                state="readonly",
                width=14,
            ).pack(side="left", padx=(4, 0))
            ttk.Label(zone_options, text="使用所選層級的摘要作業名稱作為分區").pack(side="left", padx=(10, 0))

            action = ttk.Frame(container)
            action.pack(fill="x", pady=(14, 0))
            self.convert_button = ttk.Button(action, text="開始轉換", command=self.start_conversion, width=18)
            self.convert_button.pack(side="right")
            ttk.Label(action, textvariable=self.status_value).pack(side="left")

            ttk.Progressbar(container, variable=self.progress_value, maximum=100).pack(fill="x", pady=(10, 8))

            log_group = ttk.LabelFrame(container, text="轉換紀錄", padding=8)
            log_group.pack(fill="both", expand=True)
            self.log = tk.Text(log_group, height=10, wrap="word", state="disabled", font=("Consolas", 9))
            scrollbar = ttk.Scrollbar(log_group, orient="vertical", command=self.log.yview)
            self.log.configure(yscrollcommand=scrollbar.set)
            self.log.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            self.root.after(100, self.poll_events)
            self.root.protocol("WM_DELETE_WINDOW", self.close)

        def choose_xml(self) -> None:
            selected = filedialog.askopenfilename(
                title="選擇 Microsoft Project XML",
                filetypes=[("Project XML", "*.xml"), ("所有檔案", "*.*")],
            )
            if not selected:
                return
            self.xml_value.set(selected)
            self.output_value.set(str(Path(selected).parent))
            self.status_value.set("已選擇 XML，可開始轉換。")

        def choose_output(self) -> None:
            selected = filedialog.askdirectory(title="選擇輸出資料夾")
            if selected:
                self.output_value.set(selected)

        def append_log(self, message: str) -> None:
            self.log.configure(state="normal")
            self.log.insert("end", message.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

        def start_conversion(self) -> None:
            if self.worker and self.worker.is_alive():
                return
            xml_path = Path(self.xml_value.get().strip())
            output_directory = Path(self.output_value.get().strip()) if self.output_value.get().strip() else xml_path.parent
            if not xml_path.is_file():
                messagebox.showwarning(APP_NAME, "請先選擇有效的 Project XML。")
                return
            final_path = output_path_for(xml_path, output_directory)
            overwrite = False
            if final_path.exists():
                overwrite = messagebox.askyesno(APP_NAME, f"輸出檔已存在，是否覆寫？\n\n{final_path}")
                if not overwrite:
                    return

            self.progress_value.set(0)
            self.convert_button.configure(state="disabled")
            self.append_log(f"輸入：{xml_path}")
            self.append_log(f"輸出：{final_path}")
            scale_map = {"自動": "auto", "週": "week", "月": "month", "季": "quarter", "年": "year", "關閉時間軸": "none"}
            selected_scale = scale_map[self.time_scale_value.get()]
            zone_map = {"自動": "auto", "不分區": "none", "WBS 第1層": "1", "WBS 第2層": "2", "WBS 第3層": "3"}
            selected_zone_level = zone_map[self.zone_level_value.get()]
            self.append_log(f"時間分隔：{self.time_scale_value.get()}")
            self.append_log(f"分區方式：{self.zone_level_value.get()}")
            self.status_value.set("轉換中…")

            def progress(percent: int, message: str) -> None:
                self.events.put(("progress", (percent, message)))

            def work() -> None:
                try:
                    result = convert_project(
                        xml_path,
                        output_directory,
                        overwrite=overwrite,
                        time_scale=selected_scale,
                        zone_level=selected_zone_level,
                        progress=progress,
                    )
                    self.events.put(("done", result))
                except Exception as error:
                    self.events.put(("error", (error, traceback.format_exc())))

            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

        def poll_events(self) -> None:
            try:
                while True:
                    event, payload = self.events.get_nowait()
                    if event == "progress":
                        percent, message = payload
                        self.progress_value.set(percent)
                        self.status_value.set(message)
                        self.append_log(f"[{percent:3d}%] {message}")
                    elif event == "done":
                        result: ConversionResult = payload
                        self.convert_button.configure(state="normal")
                        self.status_value.set("轉換完成。")
                        self.append_log(
                            f"完成：{result.tasks} 個作業、{result.links} 條關係、"
                            f"兩轉折 {result.two_turn_links} 條、外繞 {result.four_turn_links} 條。"
                        )
                        self.append_log(f"DXF：{result.output_file}")
                        messagebox.showinfo(
                            APP_NAME,
                            f"轉換完成\n\n作業：{result.tasks}\n關係：{result.links}\n"
                            f"格式稽核錯誤：{result.audit_errors}\n\n{result.output_file}",
                        )
                        if self.open_folder_value.get() and os.name == "nt":
                            os.startfile(str(Path(result.output_file).parent))  # type: ignore[attr-defined]
                    elif event == "error":
                        error, details = payload
                        self.convert_button.configure(state="normal")
                        self.progress_value.set(0)
                        self.status_value.set("轉換失敗。")
                        self.append_log(details)
                        messagebox.showerror(APP_NAME, f"轉換失敗：\n\n{error}")
            except queue.Empty:
                pass
            self.root.after(100, self.poll_events)

        def close(self) -> None:
            if self.worker and self.worker.is_alive():
                if not messagebox.askyesno(APP_NAME, "轉換仍在進行，確定要關閉程式嗎？"):
                    return
            self.root.destroy()

        def run(self) -> None:
            self.root.mainloop()

    ConverterWindow().run()


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--convert", type=Path, metavar="PROJECT_XML", help="以命令列轉換指定 XML")
    parser.add_argument("--output", type=Path, metavar="DIRECTORY", help="輸出資料夾")
    parser.add_argument("--overwrite", action="store_true", help="覆寫既有 DXF")
    parser.add_argument(
        "--time-scale",
        choices=("auto", "week", "month", "quarter", "year", "none"),
        default="auto",
        help="時間軸分隔",
    )
    parser.add_argument(
        "--zone-level",
        choices=("auto", "none", "1", "2", "3"),
        default="auto",
        help="分區使用的 WBS 摘要層級",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    args = parser.parse_args()

    if args.convert:
        output = args.output or args.convert.resolve().parent
        try:
            result = convert_project(
                args.convert,
                output,
                overwrite=args.overwrite,
                time_scale=args.time_scale,
                zone_level=args.zone_level,
                progress=lambda percent, message: print(f"[{percent:3d}%] {message}", flush=True),
            )
        except Exception as error:
            print(f"錯誤：{error}", file=sys.stderr)
            return 1
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0

    run_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

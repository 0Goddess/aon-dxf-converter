#!/usr/bin/env python3
"""Generate AutoCAD-compatible AON drawings with the ezdxf library."""

from __future__ import annotations

import argparse
import collections
import json
import math
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, "/tmp/ezdxf_runtime")

import ezdxf  # noqa: E402
from ezdxf.enums import TextEntityAlignment  # noqa: E402

from project_aon_converter import (  # noqa: E402
    DrawingLayout,
    DrawingNode,
    Link,
    build_layout,
    compact_number,
    date_only,
    parse_project,
    wrap_display,
)


LAYER_CONFIG = {
    "AON_TITLE": {"color": 5},
    "AON_LANE": {"color": 9, "linetype": "DASHED"},
    "AON_NODE": {"color": 7},
    "AON_CRITICAL": {"color": 1},
    "AON_WARNING": {"color": 30},
    "AON_BOUNDARY": {"color": 9, "linetype": "DASHED"},
    "AON_TEXT": {"color": 7},
    "AON_LINK_FS": {"color": 8},
    "AON_LINK_SS": {"color": 4},
    "AON_LINK_FF": {"color": 3},
    "AON_LINK_SF": {"color": 6},
    "AON_LINK_CRITICAL": {"color": 1},
}


NODE_WIDTH = 150.0
NODE_HEIGHT = 64.0
X_SPACING = 285.0
Y_SPACING = 165.0
ARROW_LENGTH = 4.8
ARROW_HALF_HEIGHT = 3.1
MIN_VERTICAL_CHANNEL_SPACING = 10.0
MIN_HORIZONTAL_CHANNEL_SPACING = 6.0


def enlarge_layout(layout: DrawingLayout) -> DrawingLayout:
    """Align a maximum set of FS links into horizontal dependency chains."""
    ranks = sorted({node.task.rank for node in layout.nodes.values()})
    rank_map = {rank: index for index, rank in enumerate(ranks)}
    zone_order = sorted(
        {node.task.zone for node in layout.nodes.values()},
        key=lambda zone: min(
            node.task.task_id for node in layout.nodes.values() if node.task.zone == zone
        ),
    )
    major_zones = set(zone_order)

    adjacency: dict[int, list[int]] = collections.defaultdict(list)
    for link in layout.links:
        if link.relation != "FS":
            continue
        pred_zone = layout.nodes[link.pred_uid].task.zone
        succ_zone = layout.nodes[link.succ_uid].task.zone
        # Preserve the XML-derived phase/area identity between dependency chains.
        if pred_zone in major_zones and succ_zone in major_zones and pred_zone != succ_zone:
            continue
        adjacency[link.pred_uid].append(link.succ_uid)

    for pred_uid, successors in adjacency.items():
        pred = layout.nodes[pred_uid].task
        successors.sort(
            key=lambda uid: (
                rank_map[layout.nodes[uid].task.rank] - rank_map[pred.rank],
                pred.zone != layout.nodes[uid].task.zone,
                not (pred.critical and layout.nodes[uid].task.critical),
                abs(pred.task_id - layout.nodes[uid].task.task_id),
                uid,
            )
        )

    left_nodes = sorted(adjacency)
    match_left: dict[int, int | None] = {uid: None for uid in left_nodes}
    match_right: dict[int, int | None] = {uid: None for uid in layout.nodes}
    distance: dict[int, int] = {}

    def matching_bfs() -> bool:
        queue: collections.deque[int] = collections.deque()
        found = False
        for uid in left_nodes:
            if match_left[uid] is None:
                distance[uid] = 0
                queue.append(uid)
            else:
                distance[uid] = -1
        while queue:
            uid = queue.popleft()
            for successor in adjacency[uid]:
                paired = match_right[successor]
                if paired is None:
                    found = True
                elif distance[paired] < 0:
                    distance[paired] = distance[uid] + 1
                    queue.append(paired)
        return found

    def matching_dfs(uid: int) -> bool:
        for successor in adjacency[uid]:
            paired = match_right[successor]
            if paired is None or (
                distance.get(paired, -1) == distance[uid] + 1 and matching_dfs(paired)
            ):
                match_left[uid] = successor
                match_right[successor] = uid
                return True
        distance[uid] = -1
        return False

    matching_size = 0
    while matching_bfs():
        for uid in left_nodes:
            if match_left[uid] is None and matching_dfs(uid):
                matching_size += 1

    selected_successor = {uid: successor for uid, successor in match_left.items() if successor is not None}
    selected_incoming = set(selected_successor.values())
    chains: list[list[int]] = []
    for start_uid in layout.nodes:
        if start_uid in selected_incoming:
            continue
        chain: list[int] = []
        uid = start_uid
        while True:
            chain.append(uid)
            if uid not in selected_successor:
                break
            uid = selected_successor[uid]
        chains.append(chain)

    def chain_group(chain: list[int]) -> str:
        zones = [layout.nodes[uid].task.zone for uid in chain]
        for zone in zone_order:
            if zone in zones:
                return zone
        return zone_order[0]

    row_assignment: dict[int, int] = {}
    cursor = 0
    group_row_counts: dict[str, int] = {}
    for group in zone_order:
        group_chains = [chain for chain in chains if chain_group(chain) == group]
        group_chains.sort(
            key=lambda chain: (
                min(rank_map[layout.nodes[uid].task.rank] for uid in chain),
                -len(chain),
                layout.nodes[chain[0]].task.task_id,
            )
        )
        occupied_by_row: list[set[int]] = []
        for chain in group_chains:
            chain_ranks = sorted(rank_map[layout.nodes[uid].task.rank] for uid in chain)
            reserved_ranks: set[int] = set()
            if len(chain_ranks) == 1:
                reserved_ranks.add(chain_ranks[0])
            else:
                for start_rank, end_rank in zip(chain_ranks, chain_ranks[1:]):
                    reserved_ranks.update(range(start_rank, end_rank + 1))
            row = next(
                (index for index, occupied in enumerate(occupied_by_row) if reserved_ranks.isdisjoint(occupied)),
                None,
            )
            if row is None:
                row = len(occupied_by_row)
                occupied_by_row.append(set())
            occupied_by_row[row].update(reserved_ranks)
            for uid in chain:
                row_assignment[uid] = cursor + row
        group_row_counts[group] = len(occupied_by_row)
        if occupied_by_row:
            cursor += len(occupied_by_row) + 1

    for uid, node in layout.nodes.items():
        node.x = rank_map[node.task.rank] * X_SPACING
        node.y = -row_assignment[uid] * Y_SPACING
        node.task.row = row_assignment[uid]

    layout.lane_ranges = {}
    for zone in zone_order:
        zone_nodes = [node for node in layout.nodes.values() if node.task.zone == zone]
        if zone_nodes:
            layout.lane_ranges[zone] = (
                max(node.y for node in zone_nodes),
                min(node.y for node in zone_nodes) - NODE_HEIGHT,
            )

    layout.optimization_stats = {
        "matched_fs_links": matching_size,
        "chains": len(chains),
        "rows": max(row_assignment.values()) + 1,
        "group_rows": group_row_counts,
    }

    layout.min_x = min(node.x for node in layout.nodes.values()) - 270.0
    layout.max_x = max(node.x for node in layout.nodes.values()) + NODE_WIDTH + 90.0
    layout.min_y = min(node.y for node in layout.nodes.values()) - NODE_HEIGHT - 110.0
    layout.max_y = max(node.y for node in layout.nodes.values()) + 190.0
    layout.width = layout.max_x - layout.min_x
    layout.height = layout.max_y - layout.min_y
    return layout


def text_width_units(value: str) -> float:
    # Text containing CJK characters is converted to Noto Sans outlines later;
    # pure ASCII text remains in AutoCAD's wider built-in txt.shx font.
    outline_text = any(ord(character) >= 128 for character in value)
    if not outline_text:
        # Treat txt.shx as a full-em monospaced font.  This deliberately
        # overestimates dates and float strings and guarantees cell clearance.
        return max(float(len(value)), 1.0)
    units = 0.0
    for character in value:
        if character.isspace():
            units += 0.34
        elif unicodedata.east_asian_width(character) in {"W", "F"}:
            units += 1.0
        elif character in "ilI1|.,:;'`":
            units += 0.32
        elif character in "MW@%#":
            units += 0.88
        else:
            units += 0.58
    # ezdxf scales Noto CJK outlines by cap-height; measured path widths are
    # up to about 1.52x the nominal em estimate.  Keep a small safety margin.
    return max(units * 1.55, 1.0)


def segment_intersects_rectangle(
    start: tuple[float, float],
    end: tuple[float, float],
    rectangle: tuple[float, float, float, float],
) -> bool:
    """Liang-Barsky segment/rectangle intersection test."""
    x1, y1 = start
    x2, y2 = end
    xmin, ymin, xmax, ymax = rectangle
    dx = x2 - x1
    dy = y2 - y1
    lower = 0.0
    upper = 1.0
    for p, q in ((-dx, x1 - xmin), (dx, xmax - x1), (-dy, y1 - ymin), (dy, ymax - y1)):
        if math.isclose(p, 0.0, abs_tol=1e-12):
            if q < 0.0:
                return False
            continue
        ratio = q / p
        if p < 0.0:
            if ratio > upper:
                return False
            lower = max(lower, ratio)
        else:
            if ratio < lower:
                return False
            upper = min(upper, ratio)
    return lower <= upper


def collinear_overlap(
    first: tuple[tuple[float, float], tuple[float, float]],
    second: tuple[tuple[float, float], tuple[float, float]],
    tolerance: float = 0.08,
) -> bool:
    (a, b), (c, d) = first, second
    vx, vy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(vx, vy)
    if length < tolerance:
        return False
    wx, wy = d[0] - c[0], d[1] - c[1]
    if abs(vx * wy - vy * wx) > tolerance * length * max(math.hypot(wx, wy), 1.0):
        return False
    if abs(vx * (c[1] - a[1]) - vy * (c[0] - a[0])) > tolerance * length:
        return False
    axis = 0 if abs(vx) >= abs(vy) else 1
    first_range = sorted((a[axis], b[axis]))
    second_range = sorted((c[axis], d[axis]))
    return min(first_range[1], second_range[1]) - max(first_range[0], second_range[0]) > tolerance


def assign_interval_lanes(
    items: list[tuple[int, float, float]],
    clearance: float = 2.0,
) -> tuple[dict[int, int], int]:
    """Greedy interval coloring: overlapping spans receive different lanes."""
    lane_ends: list[float] = []
    assignment: dict[int, int] = {}
    for key, start, end in sorted(items, key=lambda item: (min(item[1], item[2]), max(item[1], item[2]), item[0])):
        low, high = sorted((start, end))
        lane = next((index for index, lane_end in enumerate(lane_ends) if low > lane_end + clearance), None)
        if lane is None:
            lane = len(lane_ends)
            lane_ends.append(high)
        else:
            lane_ends[lane] = high
        assignment[key] = lane
    return assignment, max(1, len(lane_ends))


class EzdxfAonWriter:
    def __init__(self, layout: DrawingLayout):
        self.layout = layout
        self.doc = ezdxf.new("R2010", setup=True)
        self.doc.header["$INSUNITS"] = 0
        self.doc.header["$LIMMIN"] = (layout.min_x, layout.min_y)
        self.doc.header["$LIMMAX"] = (layout.max_x, layout.max_y)
        self.doc.header["$EXTMIN"] = (layout.min_x, layout.min_y, 0)
        self.doc.header["$EXTMAX"] = (layout.max_x, layout.max_y, 0)
        for name, attrs in LAYER_CONFIG.items():
            if name not in self.doc.layers:
                self.doc.layers.add(name, **attrs)
        if "AON_TC" not in self.doc.styles:
            self.doc.styles.add("AON_TC", font="msjh.ttc")
        if "AON_LATIN" not in self.doc.styles:
            self.doc.styles.add("AON_LATIN", font="arial.ttf")
        if "AON_LINK" not in self.doc.appids:
            self.doc.appids.add("AON_LINK")
        self.msp = self.doc.modelspace()
        self.link_geometry, self.route_stats = self.prepare_link_geometry()

    def line(self, layer: str, x1: float, y1: float, x2: float, y2: float) -> None:
        self.msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer})

    def text(
        self,
        layer: str,
        x: float,
        y: float,
        height: float,
        value: str,
        *,
        max_width: float | None = None,
        nominal_width_factor: float = 0.92,
    ) -> None:
        width_factor = nominal_width_factor
        if max_width is not None:
            estimated_width = text_width_units(value) * height
            width_factor = min(width_factor, max_width / estimated_width)
        width_factor = max(0.30, width_factor)
        self.msp.add_text(
            value,
            height=height,
            dxfattribs={"layer": layer, "style": "AON_TC", "width": width_factor},
        ).set_placement((x, y), align=TextEntityAlignment.LEFT)

    def rectangle(self, layer: str, x: float, y: float, width: float, height: float) -> None:
        self.msp.add_lwpolyline(
            [(x, y), (x + width, y), (x + width, y - height), (x, y - height)],
            close=True,
            dxfattribs={"layer": layer},
        )

    @staticmethod
    def endpoint_sides(link: Link) -> tuple[str, str]:
        source_side = "R" if link.relation in ("FS", "FF") else "L"
        target_side = "L" if link.relation in ("FS", "SS") else "R"
        return source_side, target_side

    def direct_path_is_clear(
        self,
        link: Link,
        start: tuple[float, float],
        end: tuple[float, float],
        accepted_segments: list[tuple[tuple[float, float], tuple[float, float]]],
    ) -> bool:
        if link.relation != "FS" or end[0] <= start[0]:
            return False
        margin = 2.0
        for uid, node in self.layout.nodes.items():
            if uid in (link.pred_uid, link.succ_uid):
                continue
            rectangle = (
                node.x - margin,
                node.y - NODE_HEIGHT - margin,
                node.x + NODE_WIDTH + margin,
                node.y + margin,
            )
            if segment_intersects_rectangle(start, end, rectangle):
                return False
        candidate = (start, end)
        return not any(collinear_overlap(candidate, existing) for existing in accepted_segments)

    def prepare_link_geometry(self) -> tuple[dict[int, dict[str, object]], dict[str, int]]:
        # Same-row FS links are the only direct links.  Their endpoints share
        # one y-coordinate, so the line and arrow enter the target horizontally.
        direct_candidate_groups: dict[float, list[tuple[int, float, float]]] = collections.defaultdict(list)
        for link in self.layout.links:
            pred = self.layout.nodes[link.pred_uid]
            succ = self.layout.nodes[link.succ_uid]
            if link.relation == "FS" and math.isclose(pred.y, succ.y, abs_tol=1e-6):
                direct_candidate_groups[round(pred.y, 6)].append(
                    (link.index, pred.x + NODE_WIDTH, succ.x - ARROW_LENGTH)
                )

        direct_level: dict[int, float] = {}
        maximum_direct_lanes = 1
        for row_y, items in direct_candidate_groups.items():
            assignment, count = assign_interval_lanes(items, clearance=3.0)
            maximum_direct_lanes = max(maximum_direct_lanes, count)
            # Keep horizontal arrows inside the operation-name compartment.
            top_offset = 10.0
            usable_height = 44.0
            for link_index, lane in assignment.items():
                direct_level[link_index] = row_y - top_offset - (lane + 1) * usable_height / (count + 1)

        direct_geometry: dict[int, dict[str, object]] = {}
        accepted_direct_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for link in self.layout.links:
            if link.index not in direct_level:
                continue
            pred = self.layout.nodes[link.pred_uid]
            succ = self.layout.nodes[link.succ_uid]
            y = direct_level[link.index]
            start = (pred.x + NODE_WIDTH, y)
            tip = (succ.x, y)
            arrow_base = (tip[0] - ARROW_LENGTH, y)
            if self.direct_path_is_clear(link, start, arrow_base, accepted_direct_segments):
                accepted_direct_segments.append((start, arrow_base))
                direct_geometry[link.index] = {
                    "start": start,
                    "tip": tip,
                    "base": arrow_base,
                    "target_side": "L",
                    "arrow_direction": 1,
                    "direct": True,
                }

        routed_links = [link for link in self.layout.links if link.index not in direct_geometry]

        # Routed endpoints get distinct side ports.  Direct-link levels are
        # treated as reserved so a routed line cannot overlap them at a node.
        reserved_levels: dict[tuple[int, str], list[float]] = collections.defaultdict(list)
        for link in self.layout.links:
            if link.index not in direct_geometry:
                continue
            y = float(direct_geometry[link.index]["tip"][1])
            reserved_levels[(link.pred_uid, "R")].append(y)
            reserved_levels[(link.succ_uid, "L")].append(y)

        endpoint_groups: dict[tuple[int, str], list[tuple[int, str, float]]] = collections.defaultdict(list)
        for link in routed_links:
            source_side, target_side = self.endpoint_sides(link)
            pred = self.layout.nodes[link.pred_uid]
            succ = self.layout.nodes[link.succ_uid]
            endpoint_groups[(link.pred_uid, source_side)].append((link.index, "pred", succ.y))
            endpoint_groups[(link.succ_uid, target_side)].append((link.index, "succ", pred.y))

        port_y: dict[tuple[int, str], float] = {}
        port_slot: dict[tuple[int, str], int] = {}
        port_count: dict[tuple[int, str], int] = {}
        for (uid, side), endpoints in endpoint_groups.items():
            ordered = sorted(endpoints, key=lambda item: (-item[2], item[0], item[1]))
            count = len(ordered)
            reserved = reserved_levels.get((uid, side), [])
            pool_count = count + len(reserved)
            node_top = self.layout.nodes[uid].y
            pool = [
                node_top - 2.0 - (index + 1) * (NODE_HEIGHT - 4.0) / (pool_count + 1)
                for index in range(pool_count)
            ]
            if reserved:
                pool.sort(key=lambda level: min(abs(level - value) for value in reserved), reverse=True)
                pool = sorted(pool[:count], reverse=True)
            for slot, ((link_index, role, _), level) in enumerate(zip(ordered, pool)):
                port_y[(link_index, role)] = level
                port_slot[(link_index, role)] = slot
                port_count[(link_index, role)] = count

        base: dict[int, dict[str, object]] = dict(direct_geometry)
        for link in routed_links:
            pred = self.layout.nodes[link.pred_uid]
            succ = self.layout.nodes[link.succ_uid]
            source_side, target_side = self.endpoint_sides(link)
            sx = pred.x + NODE_WIDTH if source_side == "R" else pred.x
            tx = succ.x if target_side == "L" else succ.x + NODE_WIDTH
            sy = port_y[(link.index, "pred")]
            ty = port_y[(link.index, "succ")]
            arrow_direction = 1 if target_side == "L" else -1
            base_x = tx - arrow_direction * ARROW_LENGTH

            source_count = port_count[(link.index, "pred")]
            source_spacing = min(7.0, 60.0 / max(1, source_count - 1))
            source_offset = 8.0 + port_slot[(link.index, "pred")] * source_spacing
            source_bend_x = sx + source_offset * (1 if source_side == "R" else -1)
            channel_direction = -1 if succ.y <= pred.y else 1
            base[link.index] = {
                "start": (sx, sy),
                "tip": (tx, ty),
                "base": (base_x, ty),
                "source_bend_x": source_bend_x,
                "source_side": source_side,
                "target_side": target_side,
                "arrow_direction": arrow_direction,
                "channel_direction": channel_direction,
                "direct": False,
            }

        # Balance complex links between the upper and lower gaps of each row.
        # The choice minimizes concurrent interval lanes; ties retain the
        # natural target direction and then prefer the lower channel.
        routed_by_row: dict[float, list[Link]] = collections.defaultdict(list)
        for link in routed_links:
            routed_by_row[round(self.layout.nodes[link.pred_uid].y, 6)].append(link)
        for row_links in routed_by_row.values():
            side_items: dict[int, list[tuple[int, float, float]]] = {-1: [], 1: []}
            side_counts = {-1: 0, 1: 0}
            ordered_links = sorted(
                row_links,
                key=lambda link: abs(
                    float(base[link.index]["tip"][0]) - float(base[link.index]["source_bend_x"])
                ),
                reverse=True,
            )
            for link in ordered_links:
                geometry = base[link.index]
                item = (
                    link.index,
                    float(geometry["source_bend_x"]),
                    float(geometry["tip"][0]),
                )
                natural = int(geometry["channel_direction"])
                choices: list[tuple[tuple[int, int, int, int], int, int]] = []
                for direction in (-1, 1):
                    _, candidate_count = assign_interval_lanes(side_items[direction] + [item], clearance=3.0)
                    score = (
                        max(candidate_count, side_counts[-direction]),
                        candidate_count + side_counts[-direction],
                        0 if direction == natural else 1,
                        0 if direction < 0 else 1,
                    )
                    choices.append((score, direction, candidate_count))
                _, chosen_direction, chosen_count = min(choices)
                geometry["channel_direction"] = chosen_direction
                side_items[chosen_direction].append(item)
                side_counts[chosen_direction] = chosen_count

        horizontal_groups: dict[tuple[float, int], list[tuple[int, float, float]]] = collections.defaultdict(list)
        for link in routed_links:
            geometry = base[link.index]
            source_bend_x = float(geometry["source_bend_x"])
            target_x = float(geometry["tip"][0])
            horizontal_groups[
                (round(self.layout.nodes[link.pred_uid].y, 6), int(geometry["channel_direction"]))
            ].append(
                (link.index, source_bend_x, target_x)
            )

        horizontal_lane: dict[int, int] = {}
        horizontal_lane_count: dict[int, int] = {}
        maximum_horizontal_lanes = 1
        for items in horizontal_groups.values():
            assignment, count = assign_interval_lanes(items, clearance=3.0)
            maximum_horizontal_lanes = max(maximum_horizontal_lanes, count)
            horizontal_lane.update(assignment)
            for link_index in assignment:
                horizontal_lane_count[link_index] = count

        for link in routed_links:
            geometry = base[link.index]
            count = horizontal_lane_count[link.index]
            half_gap = (Y_SPACING - NODE_HEIGHT) / 2.0
            available_height = max(12.0, half_gap - 16.0)
            spacing = min(10.0, available_height / max(1, count - 1))
            direction = int(geometry["channel_direction"])
            pred_y = self.layout.nodes[link.pred_uid].y
            if direction > 0:
                geometry["track_y"] = pred_y + 8.0 + horizontal_lane[link.index] * spacing
            else:
                geometry["track_y"] = pred_y - NODE_HEIGHT - 8.0 - horizontal_lane[link.index] * spacing

        target_groups: dict[tuple[str, float], list[tuple[int, float, float]]] = collections.defaultdict(list)
        for link in routed_links:
            geometry = base[link.index]
            target_groups[(str(geometry["target_side"]), round(float(geometry["tip"][0]), 6))].append(
                (link.index, float(geometry["track_y"]), float(geometry["tip"][1]))
            )

        target_lane: dict[int, int] = {}
        target_lane_count: dict[int, int] = {}
        maximum_target_lanes = 1
        for items in target_groups.values():
            assignment, count = assign_interval_lanes(items, clearance=3.0)
            maximum_target_lanes = max(maximum_target_lanes, count)
            target_lane.update(assignment)
            for link_index in assignment:
                target_lane_count[link_index] = count

        for link in routed_links:
            geometry = base[link.index]
            count = target_lane_count[link.index]
            available_width = X_SPACING - NODE_WIDTH - 20.0
            spacing = min(8.0, available_width / max(1, count - 1))
            offset = 8.0 + target_lane[link.index] * spacing
            target_x = float(geometry["tip"][0])
            direction = int(geometry["arrow_direction"])
            geometry["target_bend_x"] = target_x - direction * offset

        # Prefer the two-turn routing shown in the user's reference:
        # horizontal out of the source, one vertical trunk, then horizontal
        # into the target arrow.  Only links that cannot use a clear trunk
        # fall back to the upper/lower detour.
        allocated_verticals: list[tuple[float, float, float]] = []
        allocated_horizontals: list[tuple[float, float, float]] = []

        def spans_overlap(a1: float, a2: float, b1: float, b2: float) -> bool:
            return min(max(a1, a2), max(b1, b2)) - max(min(a1, a2), min(b1, b2)) > 0.01

        def simplify_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
            simplified: list[tuple[float, float]] = []
            for point in points:
                point = (float(point[0]), float(point[1]))
                if simplified and math.isclose(point[0], simplified[-1][0], abs_tol=1e-6) and math.isclose(
                    point[1], simplified[-1][1], abs_tol=1e-6
                ):
                    continue
                simplified.append(point)
                while len(simplified) >= 3:
                    a, b, c = simplified[-3:]
                    vertical = math.isclose(a[0], b[0], abs_tol=1e-6) and math.isclose(
                        b[0], c[0], abs_tol=1e-6
                    )
                    horizontal = math.isclose(a[1], b[1], abs_tol=1e-6) and math.isclose(
                        b[1], c[1], abs_tol=1e-6
                    )
                    if not (vertical or horizontal):
                        break
                    simplified[-2:] = [c]
            return simplified

        def orthogonal_segments(
            points: list[tuple[float, float]],
        ) -> list[tuple[tuple[float, float], tuple[float, float]]]:
            return [
                (first, second)
                for first, second in zip(points, points[1:])
                if not (
                    math.isclose(first[0], second[0], abs_tol=1e-6)
                    and math.isclose(first[1], second[1], abs_tol=1e-6)
                )
            ]

        def enters_endpoint_node(
            segment: tuple[tuple[float, float], tuple[float, float]], node: DrawingNode
        ) -> bool:
            (x1, y1), (x2, y2) = segment
            left, right = node.x, node.x + NODE_WIDTH
            bottom, top = node.y - NODE_HEIGHT, node.y
            if math.isclose(y1, y2, abs_tol=1e-6):
                return bottom + 0.01 < y1 < top - 0.01 and spans_overlap(x1, x2, left, right)
            if math.isclose(x1, x2, abs_tol=1e-6):
                return left + 0.01 < x1 < right - 0.01 and spans_overlap(y1, y2, bottom, top)
            return True

        def path_hits_node(link: Link, points: list[tuple[float, float]]) -> bool:
            segments = orthogonal_segments(points)
            endpoint_uids = {link.pred_uid, link.succ_uid}
            for uid, node in self.layout.nodes.items():
                if uid in endpoint_uids:
                    if any(enters_endpoint_node(segment, node) for segment in segments):
                        return True
                    continue
                rectangle = (
                    node.x - 2.0,
                    node.y - NODE_HEIGHT - 2.0,
                    node.x + NODE_WIDTH + 2.0,
                    node.y + 2.0,
                )
                if any(segment_intersects_rectangle(first, second, rectangle) for first, second in segments):
                    return True
            return False

        def path_has_channel_clearance(points: list[tuple[float, float]]) -> bool:
            segments = orthogonal_segments(points)
            local_verticals: list[tuple[float, float, float]] = []
            local_horizontals: list[tuple[float, float, float]] = []
            for (x1, y1), (x2, y2) in segments:
                if math.isclose(x1, x2, abs_tol=1e-6):
                    low, high = sorted((y1, y2))
                    if any(
                        spans_overlap(low, high, other_low, other_high)
                        and abs(x1 - other_x) < MIN_VERTICAL_CHANNEL_SPACING - 1e-6
                        for other_x, other_low, other_high in allocated_verticals + local_verticals
                    ):
                        return False
                    local_verticals.append((x1, low, high))
                elif math.isclose(y1, y2, abs_tol=1e-6):
                    low, high = sorted((x1, x2))
                    if any(
                        spans_overlap(low, high, other_low, other_high)
                        and abs(y1 - other_y) < MIN_HORIZONTAL_CHANNEL_SPACING - 1e-6
                        for other_y, other_low, other_high in allocated_horizontals + local_horizontals
                    ):
                        return False
                    local_horizontals.append((y1, low, high))
                else:
                    return False
            return True

        def register_path(points: list[tuple[float, float]]) -> None:
            for (x1, y1), (x2, y2) in orthogonal_segments(points):
                if math.isclose(x1, x2, abs_tol=1e-6):
                    low, high = sorted((y1, y2))
                    allocated_verticals.append((x1, low, high))
                else:
                    low, high = sorted((x1, x2))
                    allocated_horizontals.append((y1, low, high))

        def path_is_clear(link: Link, points: list[tuple[float, float]]) -> bool:
            return not path_hits_node(link, points) and path_has_channel_clearance(points)

        def label_position(points: list[tuple[float, float]]) -> tuple[float, float]:
            horizontal = [
                (abs(second[0] - first[0]), first, second)
                for first, second in orthogonal_segments(points)
                if math.isclose(first[1], second[1], abs_tol=1e-6)
            ]
            if horizontal:
                _, first, second = max(horizontal, key=lambda item: item[0])
                return ((first[0] + second[0]) / 2.0, first[1] + 3.8)
            first, second = points[0], points[-1]
            return ((first[0] + second[0]) / 2.0, (first[1] + second[1]) / 2.0)

        geometry_by_link: dict[int, dict[str, object]] = {}
        for link in self.layout.links:
            geometry = base[link.index]
            if not geometry["direct"]:
                continue
            points = simplify_points([geometry["start"], geometry["base"]])
            geometry["points"] = points
            geometry["label_position"] = label_position(points)
            geometry["route_mode"] = "direct"
            geometry_by_link[link.index] = geometry
            register_path(points)

        source_dogleg_links = 0
        target_dogleg_links = 0
        detour_links = 0
        relaxed_detour_links = 0
        for link in sorted(routed_links, key=lambda item: item.index):
            geometry = base[link.index]
            start = (float(geometry["start"][0]), float(geometry["start"][1]))
            arrow_base = (float(geometry["base"][0]), float(geometry["base"][1]))
            source_shift = 1 if geometry["source_side"] == "R" else -1
            target_shift = -int(geometry["arrow_direction"])
            selected_points: list[tuple[float, float]] | None = None
            route_mode = ""

            # Source-side trunks match the second reference image most closely.
            for step in range(16):
                trunk_x = float(geometry["source_bend_x"]) + source_shift * step * MIN_VERTICAL_CHANNEL_SPACING
                candidate = simplify_points(
                    [start, (trunk_x, start[1]), (trunk_x, arrow_base[1]), arrow_base]
                )
                if path_is_clear(link, candidate):
                    selected_points = candidate
                    route_mode = "source_dogleg"
                    source_dogleg_links += 1
                    break

            # If the source side is blocked, try the same two-turn shape with
            # the vertical trunk just outside the target side.
            if selected_points is None:
                for step in range(16):
                    trunk_x = float(geometry["target_bend_x"]) + target_shift * step * MIN_VERTICAL_CHANNEL_SPACING
                    candidate = simplify_points(
                        [start, (trunk_x, start[1]), (trunk_x, arrow_base[1]), arrow_base]
                    )
                    if path_is_clear(link, candidate):
                        selected_points = candidate
                        route_mode = "target_dogleg"
                        target_dogleg_links += 1
                        break

            # Retain the proven upper/lower channel only where a single trunk
            # would cross a node or violate the channel spacing.
            if selected_points is None:
                track_y = float(geometry["track_y"])
                source_x0 = float(geometry["source_bend_x"])
                target_x0 = float(geometry["target_bend_x"])
                for source_step in range(24):
                    source_x = source_x0 + source_shift * source_step * MIN_VERTICAL_CHANNEL_SPACING
                    for target_step in range(24):
                        target_x = target_x0 + target_shift * target_step * MIN_VERTICAL_CHANNEL_SPACING
                        candidate = simplify_points(
                            [
                                start,
                                (source_x, start[1]),
                                (source_x, track_y),
                                (target_x, track_y),
                                (target_x, arrow_base[1]),
                                arrow_base,
                            ]
                        )
                        if path_is_clear(link, candidate):
                            selected_points = candidate
                            break
                    if selected_points is not None:
                        break
                if selected_points is None:
                    # Very high-degree endpoints can have fixed port levels
                    # closer than the preferred spacing.  Keep node avoidance
                    # mandatory and relax only parallel clearance in that case.
                    for source_step in range(40):
                        source_x = source_x0 + source_shift * source_step * MIN_VERTICAL_CHANNEL_SPACING
                        for target_step in range(40):
                            target_x = target_x0 + target_shift * target_step * MIN_VERTICAL_CHANNEL_SPACING
                            candidate = simplify_points(
                                [
                                    start,
                                    (source_x, start[1]),
                                    (source_x, track_y),
                                    (target_x, track_y),
                                    (target_x, arrow_base[1]),
                                    arrow_base,
                                ]
                            )
                            if not path_hits_node(link, candidate):
                                selected_points = candidate
                                relaxed_detour_links += 1
                                break
                        if selected_points is not None:
                            break
                if selected_points is None:
                    raise RuntimeError(f"No node-clear route for link {link.index}")
                route_mode = "detour"
                detour_links += 1

            geometry["points"] = selected_points
            geometry["label_position"] = label_position(selected_points)
            geometry["route_mode"] = route_mode
            geometry_by_link[link.index] = geometry
            register_path(selected_points)

        return geometry_by_link, {
            "direct_links": len(direct_geometry),
            "routed_links": len(routed_links),
            "two_turn_links": source_dogleg_links + target_dogleg_links,
            "source_dogleg_links": source_dogleg_links,
            "target_dogleg_links": target_dogleg_links,
            "four_turn_links": detour_links,
            "relaxed_detour_links": relaxed_detour_links,
            "upper_channel_links": sum(
                int(base[link.index].get("channel_direction", 0)) > 0
                and geometry_by_link[link.index].get("route_mode") == "detour"
                for link in routed_links
            ),
            "lower_channel_links": sum(
                int(base[link.index].get("channel_direction", 0)) < 0
                and geometry_by_link[link.index].get("route_mode") == "detour"
                for link in routed_links
            ),
            "max_direct_lanes": maximum_direct_lanes,
            "max_horizontal_lanes": maximum_horizontal_lanes,
            "max_target_lanes": maximum_target_lanes,
        }

    def draw_link(self, link: Link, ordinal: int) -> None:
        del ordinal
        pred = self.layout.nodes[link.pred_uid]
        succ = self.layout.nodes[link.succ_uid]
        layer = "AON_LINK_CRITICAL" if link.critical else f"AON_LINK_{link.relation}"
        geometry = self.link_geometry[link.index]
        points = list(geometry["points"])
        tip_x, tip_y = geometry["tip"]
        base_x, base_y = geometry["base"]
        vertices = points + [
            (base_x, base_y + ARROW_HALF_HEIGHT),
            (tip_x, tip_y),
            (base_x, base_y - ARROW_HALF_HEIGHT),
            (base_x, base_y),
        ]
        entity = self.msp.add_lwpolyline(vertices, dxfattribs={"layer": layer})
        entity.set_xdata(
            "AON_LINK",
            [
                (1000, f"PRED_ID={pred.task.task_id}"),
                (1000, f"SUCC_ID={succ.task.task_id}"),
                (1000, f"RELATION={link.label}"),
            ],
        )
        if link.label != "FS":
            label_x, label_y = geometry["label_position"]
            self.text(layer, label_x, label_y, 3.2, link.label, max_width=34.0)

    def draw_node(self, node: DrawingNode) -> None:
        task = node.task
        x, y = node.x, node.y
        width, height = NODE_WIDTH, NODE_HEIGHT
        issue = not task.predecessor_uids or not task.successor_uids or task.manual
        layer = "AON_BOUNDARY" if node.boundary else "AON_CRITICAL" if task.critical else "AON_WARNING" if issue else "AON_NODE"
        if task.milestone:
            cx, cy = x + width / 2, y - height / 2
            points = [(cx, y), (x + width, cy), (cx, y - height), (x, cy), (cx, y)]
            for start, end in zip(points, points[1:]):
                self.line(layer, start[0], start[1], end[0], end[1])
        else:
            self.rectangle(layer, x, y, width, height)
            self.line(layer, x, y - 12, x + width, y - 12)
            self.line(layer, x, y - 38, x + width, y - 38)
            self.line(layer, x, y - 51, x + width, y - 51)
            for split in (50, 100):
                self.line(layer, x + split, y - 38, x + split, y - height)
        prefix = "[外部] " if node.boundary else ""
        self.text(
            "AON_TEXT", x + 2.2, y - 8.1, 4.3,
            f"{prefix}ID {task.task_id} | WBS {task.wbs}", max_width=144.0,
        )
        for index, name_line in enumerate(wrap_display(task.name, 50, 2)):
            self.text(
                "AON_TEXT", x + 2.2, y - 20.2 - index * 11.2, 6.0,
                name_line, max_width=144.0,
            )
        self.text("AON_TEXT", x + 1.8, y - 46.7, 4.5, f"ES {date_only(task.early_start)}", max_width=45.0)
        self.text("AON_TEXT", x + 51.8, y - 46.7, 4.5, f"D {compact_number(task.duration_days)}d", max_width=45.0)
        self.text("AON_TEXT", x + 101.8, y - 46.7, 4.5, f"EF {date_only(task.early_finish)}", max_width=45.0)
        self.text("AON_TEXT", x + 1.8, y - 59.7, 4.5, f"LS {date_only(task.late_start)}", max_width=45.0)
        self.text(
            "AON_TEXT", x + 51.8, y - 59.7, 4.1,
            f"TF {compact_number(task.total_slack_days)} / FF {compact_number(task.free_slack_days)}",
            max_width=45.0,
        )
        self.text("AON_TEXT", x + 101.8, y - 59.7, 4.5, f"LF {date_only(task.late_finish)}", max_width=45.0)

    def build(self) -> None:
        self.text(
            "AON_TITLE", self.layout.min_x + 6, self.layout.max_y - 14, 9.0,
            self.layout.title, max_width=self.layout.width - 20,
        )
        self.text(
            "AON_TEXT",
            self.layout.min_x + 6,
            self.layout.max_y - 31,
            4.8,
            "節點：ID/WBS、作業名稱、ES/D/EF、LS/TF/FF/LF；紅色為Project要徑；橘色為開放端點或手動排程；關係線為單一物件。",
            max_width=self.layout.width - 20,
        )
        for zone, (top, bottom) in self.layout.lane_ranges.items():
            self.text("AON_TITLE", self.layout.min_x + 6, top - 9, 5.5, zone, max_width=230.0)
            self.line("AON_LANE", self.layout.min_x + 2, top + 18, self.layout.max_x - 2, top + 18)
            self.line("AON_LANE", self.layout.min_x + 2, bottom - 18, self.layout.max_x - 2, bottom - 18)
        for ordinal, link in enumerate(self.layout.links):
            self.draw_link(link, ordinal)
        for node in self.layout.nodes.values():
            self.draw_node(node)

    def save(self, path: Path) -> dict:
        self.build()
        center = ((self.layout.min_x + self.layout.max_x) / 2, (self.layout.min_y + self.layout.max_y) / 2)
        view_height = max(self.layout.height, self.layout.width / 1.65) * 1.08
        self.doc.set_modelspace_vport(height=view_height, center=center)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp.dxf")
        self.doc.saveas(temp_path)
        loaded = ezdxf.readfile(temp_path)
        auditor = loaded.audit()
        if auditor.errors:
            raise RuntimeError(f"{path.name} DXF audit errors: {auditor.errors}")
        counts: dict[str, int] = {}
        for entity in loaded.modelspace():
            counts[entity.dxftype()] = counts.get(entity.dxftype(), 0) + 1
        temp_path.replace(path)
        return {
            "file": path.name,
            "bytes": path.stat().st_size,
            "audit_errors": len(auditor.errors),
            "entities": len(loaded.modelspace()),
            "types": counts,
            "layout_optimization": getattr(self.layout, "optimization_stats", {}),
            "routing": self.route_stats,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("xml", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--full-only", action="store_true")
    args = parser.parse_args()
    model = parse_project(args.xml)
    specs = [
        ("AON_FULL_R2010_V5_DOGLEG.dxf", "富邦人壽新竹湖口開發新建工程｜AON全工程網圖", None),
        ("AON_A1_R2010.dxf", "A1區｜AON分區網圖（含跨區邊界作業）", {"A1區"}),
        ("AON_A2_R2010.dxf", "A2區｜AON分區網圖（含跨區邊界作業）", {"A2區"}),
        ("AON_B_R2010.dxf", "B區｜AON分區網圖（含跨區邊界作業）", {"B區"}),
        ("AON_COMMON_CLOSE_R2010.dxf", "共同、前置及驗收｜AON分區網圖（含跨區邊界作業）", {"共同／前置", "其他共同工程", "驗收／送電"}),
    ]
    if args.full_only:
        specs = specs[:1]
    results = []
    for filename, title, zones in specs:
        layout = enlarge_layout(build_layout(model, title, zones))
        results.append(EzdxfAonWriter(layout).save(args.output / filename))
    (args.output / "ezdxf_validation.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

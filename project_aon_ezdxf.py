#!/usr/bin/env python3
"""Generate AutoCAD-compatible AON drawings with the ezdxf library."""

from __future__ import annotations

import argparse
import collections
import json
import math
import sys
import unicodedata
from datetime import date, datetime
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
    "AON_LINK_FS": {"color": 5, "true_color": 0x4DA6FF, "lineweight": 25},
    "AON_LINK_SS": {"color": 4, "true_color": 0x00D5D8, "lineweight": 25},
    "AON_LINK_FF": {"color": 3, "true_color": 0x2ECC71, "lineweight": 25},
    "AON_LINK_SF": {"color": 6, "true_color": 0xFF4FD8, "lineweight": 25},
    "AON_LINK_CRITICAL": {"color": 1, "true_color": 0xFF3030, "lineweight": 35},
}


NODE_WIDTH = 150.0
NODE_HEIGHT = 64.0
# Compact month grid: enough room for a 150-unit node and routing channels,
# without allowing a multi-year schedule to expand excessively.
X_SPACING = 250.0
# Extra vertical room is reserved for visibly separated horizontal channels.
# The endpoint-group cleanup below then spreads any remaining near-coincident
# horizontal stubs without adding corners or changing relationship objects.
Y_SPACING = 175.0
ARROW_LENGTH = 4.8
ARROW_HALF_HEIGHT = 3.1
MIN_VERTICAL_CHANNEL_SPACING = 14.0
MIN_HORIZONTAL_CHANNEL_SPACING = 9.0
PREFERRED_HORIZONTAL_CHANNEL_SPACING = 13.0
# Every routed relationship must visibly leave and enter a work node through a
# horizontal stub.  Measure the target stub from the arrow base (not its tip),
# otherwise the arrow length consumes almost the entire apparent clearance.
MIN_ENDPOINT_STUB_LENGTH = 28.0
ENDPOINT_CHANNEL_STEP = 14.0


TIME_SCALE_TITLES = {"week": "週", "month": "月", "quarter": "季", "year": "年"}


def task_dates(task) -> tuple[date, date]:
    start_raw = task.early_start or task.start
    finish_raw = task.early_finish or task.finish or start_raw
    try:
        start = datetime.fromisoformat(start_raw).date()
        finish = datetime.fromisoformat(finish_raw).date()
    except (TypeError, ValueError):
        return date(1970, 1, 1), date(1970, 1, 1)
    if finish < start:
        start, finish = finish, start
    return start, finish


def period_key(value: date, scale: str) -> tuple[int, ...]:
    if scale == "week":
        iso = value.isocalendar()
        return iso.year, iso.week
    if scale == "quarter":
        return value.year, (value.month - 1) // 3 + 1
    if scale == "year":
        return (value.year,)
    return value.year, value.month


def dominant_period(task, scale: str) -> tuple[int, ...]:
    start, finish = task_dates(task)
    counts: collections.Counter[tuple[int, ...]] = collections.Counter()
    for ordinal in range(start.toordinal(), finish.toordinal() + 1):
        counts[period_key(date.fromordinal(ordinal), scale)] += 1
    return min(counts, key=lambda key: (-counts[key], key))


def period_label(key: tuple[int, ...], scale: str) -> str:
    if scale == "week":
        return f"{key[0]:04d}-W{key[1]:02d}"
    if scale == "quarter":
        return f"{key[0]:04d}-Q{key[1]}"
    if scale == "year":
        return f"{key[0]:04d}"
    return f"{key[0]:04d}-{key[1]:02d}"


def resolve_time_scale(layout: DrawingLayout, requested: str) -> str:
    if requested != "auto":
        return requested
    starts, finishes = zip(*(task_dates(node.task) for node in layout.nodes.values()))
    span_days = (max(finishes) - min(starts)).days + 1
    if span_days <= 120:
        return "week"
    if span_days <= 1460:
        return "month"
    if span_days <= 3650:
        return "quarter"
    return "year"


def enlarge_layout(layout: DrawingLayout, time_scale: str = "month") -> DrawingLayout:
    """Lay out tasks by a user-selectable dominant time period."""
    scale = resolve_time_scale(layout, time_scale)
    task_period = (
        {uid: (node.task.rank,) for uid, node in layout.nodes.items()}
        if scale == "none"
        else {uid: dominant_period(node.task, scale) for uid, node in layout.nodes.items()}
    )
    periods = sorted(set(task_period.values()))
    period_index = {period: index for index, period in enumerate(periods)}
    zone_order = sorted(
        {node.task.zone for node in layout.nodes.values()},
        key=lambda zone: (
            sum(node.task.zone == zone for node in layout.nodes.values()),
            min(node.task.task_id for node in layout.nodes.values() if node.task.zone == zone),
        ),
    )
    major_zones = set(zone_order)

    adjacency: dict[int, list[int]] = collections.defaultdict(list)
    for link in layout.links:
        if link.relation != "FS":
            continue
        if layout.nodes[link.pred_uid].task.critical or layout.nodes[link.succ_uid].task.critical:
            continue
        # Nodes sharing a month need separate rows; backward links must not
        # create a chain that reverses the chronological x-axis.
        if period_index[task_period[link.pred_uid]] >= period_index[task_period[link.succ_uid]]:
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
                period_index[task_period[uid]] - period_index[task_period[pred_uid]],
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
                -len(chain),
                min(period_index[task_period[uid]] for uid in chain),
                layout.nodes[chain[0]].task.task_id,
            )
        )
        occupied_by_row: list[set[int]] = []
        for chain in group_chains:
            chain_ranks = sorted(period_index[task_period[uid]] for uid in chain)
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
        # Moving a complete packed row does not add bends: aligned dependency
        # chains remain aligned.  Rows with fewer activities are placed first.
        local_rows = range(len(occupied_by_row))
        ordered_rows = sorted(
            local_rows,
            key=lambda row: (
                sum(row_assignment[uid] == cursor + row for uid in row_assignment),
                min(
                    layout.nodes[uid].task.task_id
                    for uid in row_assignment
                    if row_assignment[uid] == cursor + row
                ),
            ),
        )
        remap = {old_row: new_row for new_row, old_row in enumerate(ordered_rows)}
        for uid in list(row_assignment):
            old_row = row_assignment[uid] - cursor
            if old_row in remap:
                row_assignment[uid] = cursor + remap[old_row]
        group_row_counts[group] = len(occupied_by_row)
        if occupied_by_row:
            cursor += len(occupied_by_row) + 1

    # Each time period receives enough horizontal slots for every critical
    # activity in that period.  This keeps the entire critical path on the
    # center line without stacking critical nodes on top of each other.
    critical_by_period: dict[tuple[int, ...], list[int]] = collections.defaultdict(list)
    for uid, node in layout.nodes.items():
        if node.task.critical:
            critical_by_period[task_period[uid]].append(uid)
    for uids in critical_by_period.values():
        uids.sort(key=lambda uid: (layout.nodes[uid].task.rank, task_dates(layout.nodes[uid].task)[0], layout.nodes[uid].task.task_id))

    period_starts: dict[tuple[int, ...], float] = {}
    period_centers: dict[tuple[int, ...], float] = {}
    boundaries: list[float] = []
    cursor_x = 0.0
    for period in periods:
        slots = max(1, len(critical_by_period.get(period, [])))
        period_starts[period] = cursor_x
        first_center = cursor_x + NODE_WIDTH / 2
        last_center = cursor_x + (slots - 1) * X_SPACING + NODE_WIDTH / 2
        period_centers[period] = (first_center + last_center) / 2
        if not boundaries:
            boundaries.append(first_center - X_SPACING / 2)
        cursor_x += slots * X_SPACING
        boundaries.append(last_center + X_SPACING / 2)

    critical_uids = {uid for uids in critical_by_period.values() for uid in uids}
    for period, uids in critical_by_period.items():
        for slot, uid in enumerate(uids):
            layout.nodes[uid].x = period_starts[period] + slot * X_SPACING
            layout.nodes[uid].y = 0.0
            layout.nodes[uid].task.row = 0

    # Place noncritical zone bands around the centered critical path.  Smaller
    # zones remain above larger zones as requested, while packed chains retain
    # a common row and therefore retain their bend reduction.
    noncritical_rows_by_zone: dict[str, list[int]] = {}
    for zone in zone_order:
        rows = sorted({row_assignment[uid] for uid, node in layout.nodes.items() if uid not in critical_uids and node.task.zone == zone})
        noncritical_rows_by_zone[zone] = rows

    # Build relationship information used by the side and row ordering passes.
    row_members: dict[int, list[int]] = collections.defaultdict(list)
    for uid, old_row in row_assignment.items():
        if uid not in critical_uids:
            row_members[old_row].append(uid)
    linked_rows: dict[int, list[int | None]] = collections.defaultdict(list)
    for link in layout.links:
        pred_row = None if link.pred_uid in critical_uids else row_assignment[link.pred_uid]
        succ_row = None if link.succ_uid in critical_uids else row_assignment[link.succ_uid]
        if pred_row is not None:
            linked_rows[pred_row].append(succ_row)
        if succ_row is not None:
            linked_rows[succ_row].append(pred_row)

    ordered_noncritical_rows = sorted(
        [
            (zone, row)
            for zone in zone_order
            for row in noncritical_rows_by_zone[zone]
        ],
        key=lambda item: (
            len(row_members[item[1]]),
            min(task_dates(layout.nodes[uid].task)[0] for uid in row_members[item[1]]),
            min(layout.nodes[uid].task.task_id for uid in row_members[item[1]]),
        ),
    )
    split = (len(ordered_noncritical_rows) + 1) // 2
    upper_rows = ordered_noncritical_rows[:split]
    lower_rows = ordered_noncritical_rows[split:]

    # Preserve the requested upper/lower capacity while swapping complete
    # packed rows to keep directly related operations on the same side of the
    # critical path.  This prevents an upper predecessor from sending a very
    # long branch to an otherwise movable operation at the bottom.
    row_edges: collections.Counter[tuple[int, int]] = collections.Counter()
    for link in layout.links:
        if link.pred_uid in critical_uids or link.succ_uid in critical_uids:
            continue
        first = row_assignment[link.pred_uid]
        second = row_assignment[link.succ_uid]
        if first == second:
            continue
        row_edges[tuple(sorted((first, second)))] += 1

    side = {row: 1 for _, row in upper_rows}
    side.update({row: -1 for _, row in lower_rows})

    def side_score(candidate: dict[int, int]) -> int:
        cut_cost = sum(
            weight
            for (first, second), weight in row_edges.items()
            if candidate[first] != candidate[second]
        )
        upper_activity_count = sum(
            len(row_members[row]) for row, value in candidate.items() if value > 0
        )
        return cut_cost * 100 + upper_activity_count

    current_score = side_score(side)
    for _ in range(80):
        best: tuple[int, int, int] | None = None
        for upper_row in [row for row, value in side.items() if value > 0]:
            for lower_row in [row for row, value in side.items() if value < 0]:
                side[upper_row], side[lower_row] = -1, 1
                score = side_score(side)
                side[upper_row], side[lower_row] = 1, -1
                if score < current_score and (best is None or score < best[0]):
                    best = (score, upper_row, lower_row)
        if best is None:
            break
        current_score, upper_row, lower_row = best
        side[upper_row], side[lower_row] = -1, 1

    order_index = {row: index for index, (_, row) in enumerate(ordered_noncritical_rows)}
    row_zone = {row: zone for zone, row in ordered_noncritical_rows}
    upper_rows = sorted(
        [(row_zone[row], row) for row, value in side.items() if value > 0],
        key=lambda item: order_index[item[1]],
    )
    lower_rows = sorted(
        [(row_zone[row], row) for row, value in side.items() if value < 0],
        key=lambda item: order_index[item[1]],
    )

    # With the upper/lower membership fixed, reorder rows on each side by the
    # average position of their related rows.  Keeping membership fixed avoids
    # destabilising the reserved routing corridors, while the barycentric
    # sweep removes many avoidable X crossings.  Activity count remains only a
    # tie-breaker, as crossing reduction is the primary layout objective.
    for _ in range(8):
        provisional = {
            row: len(upper_rows) - index
            for index, (_, row) in enumerate(upper_rows)
        }
        provisional.update(
            {
                row: -(index + 1)
                for index, (_, row) in enumerate(lower_rows)
            }
        )

        def fixed_side_barycenter(row: int) -> float:
            neighbors = linked_rows.get(row, [])
            if not neighbors:
                return float(provisional[row])
            return sum(
                0.0 if neighbor is None else provisional.get(neighbor, 0)
                for neighbor in neighbors
            ) / len(neighbors)

        next_upper = sorted(
            upper_rows,
            key=lambda item: (
                -fixed_side_barycenter(item[1]),
                len(row_members[item[1]]),
                order_index[item[1]],
            ),
        )
        next_lower = sorted(
            lower_rows,
            key=lambda item: (
                -fixed_side_barycenter(item[1]),
                len(row_members[item[1]]),
                order_index[item[1]],
            ),
        )
        if next_upper == upper_rows and next_lower == lower_rows:
            break
        upper_rows, lower_rows = next_upper, next_lower

    vertical_row: dict[int, int] = {}
    for index, (_, old_row) in enumerate(upper_rows):
        vertical_row[old_row] = len(upper_rows) - index
    for index, (_, old_row) in enumerate(lower_rows):
        vertical_row[old_row] = -(index + 1)

    for uid, node in layout.nodes.items():
        if uid in critical_uids:
            continue
        node.x = period_centers[task_period[uid]] - NODE_WIDTH / 2
        node.task.row = vertical_row[row_assignment[uid]]
        node.y = node.task.row * Y_SPACING

    # Activities that fall in the same displayed period cannot be placed on
    # one horizontal row.  Keep every same-period dependency component in a
    # monotone vertical order instead.  Without this pass a sequence such as
    # 79 -> 80 -> 81 -> 82 could be placed down/up/down in one month column,
    # forcing the arrows to double back and producing several nearly joined
    # horizontal segments.
    same_period_neighbors: dict[int, set[int]] = collections.defaultdict(set)
    same_period_links: list[Link] = []
    for link in layout.links:
        if link.pred_uid in critical_uids or link.succ_uid in critical_uids:
            continue
        pred = layout.nodes[link.pred_uid]
        succ = layout.nodes[link.succ_uid]
        if task_period[link.pred_uid] != task_period[link.succ_uid]:
            continue
        if (pred.task.row > 0) != (succ.task.row > 0):
            continue
        same_period_neighbors[link.pred_uid].add(link.succ_uid)
        same_period_neighbors[link.succ_uid].add(link.pred_uid)
        same_period_links.append(link)

    ordering_predecessors: dict[int, list[int]] = collections.defaultdict(list)
    ordering_successors: dict[int, list[int]] = collections.defaultdict(list)
    for link in layout.links:
        ordering_predecessors[link.succ_uid].append(link.pred_uid)
        ordering_successors[link.pred_uid].append(link.succ_uid)

    ordered_same_period_uids: set[int] = set()
    preferred_between_link_indices: set[int] = set()
    reordered_same_period_components = 0
    seen_same_period: set[int] = set()
    for seed_uid in sorted(same_period_neighbors):
        if seed_uid in seen_same_period:
            continue
        component: set[int] = set()
        queue = collections.deque([seed_uid])
        while queue:
            uid = queue.popleft()
            if uid in component:
                continue
            component.add(uid)
            queue.extend(same_period_neighbors[uid] - component)
        seen_same_period.update(component)
        if len(component) < 2:
            continue

        # Kahn ordering keeps explicit dependency direction authoritative;
        # Project rank/start/ID provide a stable order for parallel branches.
        component_successors: dict[int, list[int]] = collections.defaultdict(list)
        indegree = {uid: 0 for uid in component}
        for link in same_period_links:
            if link.pred_uid in component and link.succ_uid in component:
                component_successors[link.pred_uid].append(link.succ_uid)
                indegree[link.succ_uid] += 1
        # Branching components need a full layered-graph optimization; moving
        # them as one sorted chain can improve one branch but badly congest
        # another.  Restrict this local pass to true one-in/one-out sequences.
        if any(
            indegree[uid] > 1 or len(component_successors.get(uid, [])) > 1
            for uid in component
        ):
            continue
        ready = sorted(
            (uid for uid, degree in indegree.items() if degree == 0),
            key=lambda uid: (
                layout.nodes[uid].task.rank,
                task_dates(layout.nodes[uid].task)[0],
                layout.nodes[uid].task.task_id,
            ),
        )
        dependency_order: list[int] = []
        while ready:
            uid = ready.pop(0)
            dependency_order.append(uid)
            for successor in sorted(
                component_successors.get(uid, []),
                key=lambda other: (
                    layout.nodes[other].task.rank,
                    task_dates(layout.nodes[other].task)[0],
                    layout.nodes[other].task.task_id,
                ),
            ):
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.append(successor)
                    ready.sort(
                        key=lambda other: (
                            layout.nodes[other].task.rank,
                            task_dates(layout.nodes[other].task)[0],
                            layout.nodes[other].task.task_id,
                        )
                    )
        if len(dependency_order) != len(component):
            continue

        original_rows = {uid: layout.nodes[uid].task.row for uid in component}

        def sequence_reversal_count(candidate_rows: dict[int, int]) -> int:
            rows = [
                candidate_rows.get(uid, original_rows[uid])
                for uid in dependency_order
            ]
            directions = [
                (next_row > current_row) - (next_row < current_row)
                for current_row, next_row in zip(rows, rows[1:])
                if current_row != next_row
            ]
            return sum(
                first != second
                for first, second in zip(directions, directions[1:])
            )

        original_reversals = sequence_reversal_count({})
        if original_reversals == 0:
            continue

        def component_order_cost(candidate_rows: dict[int, int]) -> float:
            def row_of(uid: int) -> int:
                return candidate_rows.get(uid, layout.nodes[uid].task.row)

            incident_links = {
                link.index: link
                for uid in component
                for link in layout.links
                if link.pred_uid == uid or link.succ_uid == uid
            }
            length_cost = sum(
                abs(row_of(link.pred_uid) - row_of(link.succ_uid))
                for link in incident_links.values()
            )
            affected_centers = component | {
                uid
                for member in component
                for uid in ordering_predecessors.get(member, []) + ordering_successors.get(member, [])
            }
            reversal_count = sum(
                1
                for center_uid in affected_centers
                for pred_uid in ordering_predecessors.get(center_uid, [])
                for succ_uid in ordering_successors.get(center_uid, [])
                if (
                    (row_of(center_uid) - row_of(pred_uid))
                    * (row_of(succ_uid) - row_of(center_uid))
                    < 0
                )
            )
            movement_cost = sum(
                abs(candidate_rows[uid] - original_rows[uid])
                for uid in candidate_rows
            )
            return length_cost + reversal_count * 18.0 + movement_cost * 0.05

        # Search short windows around the reversal instead of sorting the
        # complete sequence.  A two-node exchange is enough for the common
        # up/down/up case and leaves every unrelated row untouched.
        candidates: list[
            tuple[int, int, int, float, int, int, dict[int, int]]
        ] = []
        for start_index in range(len(dependency_order) - 2):
            maximum_end = min(len(dependency_order), start_index + 8)
            for end_index in range(start_index + 3, maximum_end + 1):
                window = dependency_order[start_index:end_index]
                window_rows = sorted(original_rows[uid] for uid in window)
                for rows in (window_rows, list(reversed(window_rows))):
                    assignment = dict(zip(window, rows))
                    moved_nodes = sum(
                        assignment[uid] != original_rows[uid] for uid in window
                    )
                    total_displacement = sum(
                        abs(assignment[uid] - original_rows[uid]) for uid in window
                    )
                    if moved_nodes > 2 or total_displacement > 8:
                        continue
                    improvement = original_reversals - sequence_reversal_count(
                        assignment
                    )
                    if improvement <= 0:
                        continue
                    candidates.append(
                        (
                            -improvement,
                            moved_nodes,
                            total_displacement,
                            component_order_cost(assignment),
                            start_index,
                            end_index,
                            assignment,
                        )
                    )
        if not candidates:
            continue
        _, _, _, _, _, _, chosen_assignment = min(candidates)
        reordered_same_period_components += 1
        for uid, row in chosen_assignment.items():
            layout.nodes[uid].task.row = row
            layout.nodes[uid].y = row * Y_SPACING
        ordered_same_period_uids.update(chosen_assignment)
        preferred_between_link_indices.update(
            link.index
            for link in same_period_links
            if (
                link.pred_uid in chosen_assignment
                and link.succ_uid in chosen_assignment
            )
        )

    # Individual branch nodes may still land on the opposite side from all of
    # their dependencies because row packing works on whole chains.  Allow a
    # node to use a free row on the other side when that materially shortens
    # its incident relationships.  The critical row remains reserved.
    incident: dict[int, list[int]] = collections.defaultdict(list)
    predecessors: dict[int, list[int]] = collections.defaultdict(list)
    successors: dict[int, list[int]] = collections.defaultdict(list)
    for link in layout.links:
        incident[link.pred_uid].append(link.succ_uid)
        incident[link.succ_uid].append(link.pred_uid)
        predecessors[link.succ_uid].append(link.pred_uid)
        successors[link.pred_uid].append(link.succ_uid)
    occupied = {
        (round(node.x, 6), node.task.row): uid
        for uid, node in layout.nodes.items()
    }
    max_side_row = max(abs(node.task.row) for node in layout.nodes.values())
    for _ in range(3):
        moved = False
        for uid, node in sorted(
            layout.nodes.items(),
            key=lambda item: (-item[1].task.rank, -len(incident.get(item[0], [])), item[1].task.task_id),
        ):
            if (
                uid in critical_uids
                or uid in ordered_same_period_uids
                or not predecessors.get(uid)
                or node.task.row == 0
            ):
                continue
            weighted_neighbors = [
                (layout.nodes[other], 3.0) for other in predecessors[uid]
            ] + [
                (layout.nodes[other], 1.0) for other in successors.get(uid, [])
            ]

            def local_reversal_penalty(candidate_y: float) -> float:
                reversals = sum(
                    1
                    for pred_uid in predecessors.get(uid, [])
                    for succ_uid in successors.get(uid, [])
                    if (
                        (candidate_y - layout.nodes[pred_uid].y)
                        * (layout.nodes[succ_uid].y - candidate_y)
                        < 0
                    )
                )
                return reversals * Y_SPACING * 12.0

            current_cost = sum(
                weight * abs(node.y - other.y) for other, weight in weighted_neighbors
            ) + abs(node.y) * 0.08 + local_reversal_penalty(node.y)
            opposite_sign = -1 if node.task.row > 0 else 1
            candidates: list[tuple[float, int]] = []
            for magnitude in range(1, max_side_row + 1):
                candidate_row = opposite_sign * magnitude
                owner = occupied.get((round(node.x, 6), candidate_row))
                if owner is not None and owner != uid:
                    continue
                candidate_y = candidate_row * Y_SPACING
                cost = sum(
                    weight * abs(candidate_y - other.y)
                    for other, weight in weighted_neighbors
                ) + abs(candidate_y) * 0.08 + local_reversal_penalty(candidate_y)
                candidates.append((cost, candidate_row))
            if not candidates:
                continue
            best_cost, best_row = min(candidates)
            if best_cost > current_cost - Y_SPACING:
                continue
            occupied.pop((round(node.x, 6), node.task.row), None)
            node.task.row = best_row
            node.y = best_row * Y_SPACING
            occupied[(round(node.x, 6), best_row)] = uid
            moved = True
        if not moved:
            break

    layout.lane_ranges = {}
    for zone in zone_order:
        zone_nodes = [node for node in layout.nodes.values() if node.task.zone == zone and not node.task.critical]
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
        "time_periods": len(periods),
        "time_scale": scale,
        "same_period_components_reordered": reordered_same_period_components,
        "preferred_between_links": len(preferred_between_link_indices),
    }
    layout.preferred_between_links = preferred_between_link_indices

    layout.time_axis = [] if scale == "none" else [
        (period_label(period, scale), period_centers[period])
        for period in periods
    ]
    layout.time_axis_title = "" if scale == "none" else f"時間座標（{TIME_SCALE_TITLES[scale]}）"
    layout.time_boundaries = [] if scale == "none" else boundaries

    layout.min_x = min(node.x for node in layout.nodes.values()) - 270.0
    layout.max_x = max(node.x for node in layout.nodes.values()) + NODE_WIDTH + 90.0
    layout.min_y = min(node.y for node in layout.nodes.values()) - NODE_HEIGHT - 110.0
    layout.max_y = max(node.y for node in layout.nodes.values()) + 250.0
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
                layer_attrs = {key: value for key, value in attrs.items() if key != "true_color"}
                layer = self.doc.layers.add(name, **layer_attrs)
                if "true_color" in attrs:
                    value = attrs["true_color"]
                    layer.rgb = ((value >> 16) & 255, (value >> 8) & 255, value & 255)
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
            top_offset = 2.0
            usable_height = NODE_HEIGHT - 4.0
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
                # Direct relationships already occupy fixed elevations.  Take
                # the closest pool slot out for each reserved level, then map
                # the remaining slots top-to-bottom by the counterpart node's
                # y-position.  This makes lower successors leave through lower
                # ports instead of crossing an upper/direct relationship.
                available = list(pool)
                average_counterpart_y = sum(item[2] for item in endpoints) / max(1, len(endpoints))
                prefer_upper_ports = average_counterpart_y > node_top + 1e-6
                prefer_lower_ports = average_counterpart_y < node_top - NODE_HEIGHT - 1e-6
                for value in sorted(reserved, reverse=True):
                    if available:
                        if prefer_upper_ports:
                            # Reserve the lower equidistant slot so upper
                            # successors retain the higher source port.
                            tie_break = lambda level: level
                        elif prefer_lower_ports:
                            # Reserve the higher equidistant slot so lower
                            # successors retain the lower source port.
                            tie_break = lambda level: -level
                        else:
                            tie_break = lambda level: -level
                        closest = min(
                            available,
                            key=lambda level: (abs(level - value), tie_break(level)),
                        )
                        available.remove(closest)
                pool = sorted(available[:count], reverse=True)
            for slot, ((link_index, role, _), level) in enumerate(zip(ordered, pool)):
                # Nearby nodes often generate identical port elevations.  A
                # small deterministic offset prevents exact endpoint overlap
                # while preserving the top-to-bottom fan-out order.
                jitter = ((((link_index + 1) * 613) % 997) / 996.0 - 0.5) * 1.8
                node_top = self.layout.nodes[uid].y
                adjusted_level = max(
                    node_top - NODE_HEIGHT + 4.0,
                    min(node_top - 4.0, level + jitter),
                )
                port_y[(link_index, role)] = adjusted_level
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
            source_spacing = min(
                ENDPOINT_CHANNEL_STEP,
                98.0 / max(1, source_count - 1),
            )
            source_offset = (
                MIN_ENDPOINT_STUB_LENGTH
                + port_slot[(link.index, "pred")] * source_spacing
            )
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

        # Keep vertically separated dependencies on the side of their target:
        # a lower successor must leave through the lower channel and an upper
        # successor through the upper channel.  Only same-row links are
        # balanced between both sides to reduce congestion.
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
                pred_y = self.layout.nodes[link.pred_uid].y
                succ_y = self.layout.nodes[link.succ_uid].y
                if not math.isclose(pred_y, succ_y, abs_tol=1e-6):
                    geometry["channel_direction"] = natural
                    side_items[natural].append(item)
                    _, side_counts[natural] = assign_interval_lanes(
                        side_items[natural], clearance=3.0
                    )
                    continue
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
            spacing = min(
                PREFERRED_HORIZONTAL_CHANNEL_SPACING,
                available_height / max(1, count - 1),
            )
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
            available_width = X_SPACING - NODE_WIDTH - 2.0 * MIN_ENDPOINT_STUB_LENGTH
            spacing = min(
                ENDPOINT_CHANNEL_STEP,
                max(4.0, available_width / max(1, count - 1)),
            )
            offset = MIN_ENDPOINT_STUB_LENGTH + target_lane[link.index] * spacing
            arrow_base_x = float(geometry["base"][0])
            direction = int(geometry["arrow_direction"])
            geometry["target_bend_x"] = arrow_base_x - direction * offset

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

        def respects_endpoint_stubs(points: list[tuple[float, float]]) -> bool:
            """Keep vertical channels visibly clear of both endpoint nodes."""
            segments = orthogonal_segments(points)
            if not segments:
                return False
            first, last = segments[0], segments[-1]
            first_horizontal = math.isclose(first[0][1], first[1][1], abs_tol=1e-6)
            last_horizontal = math.isclose(last[0][1], last[1][1], abs_tol=1e-6)
            if not first_horizontal or not last_horizontal:
                return False
            first_length = abs(first[1][0] - first[0][0])
            last_length = abs(last[1][0] - last[0][0])
            return (
                first_length >= MIN_ENDPOINT_STUB_LENGTH - 1e-6
                and last_length >= MIN_ENDPOINT_STUB_LENGTH - 1e-6
            )

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
            return (
                respects_endpoint_stubs(points)
                and not path_hits_node(link, points)
                and path_has_channel_clearance(points)
            )

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
        emergency_detour_links = 0
        forced_detour_links = 0
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
                    for source_step in range(28):
                        source_x = source_x0 + source_shift * source_step * MIN_VERTICAL_CHANNEL_SPACING
                        for target_step in range(28):
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
                                relaxed_detour_links += 1
                                break
                        if selected_points is not None:
                            break
                if selected_points is None:
                    # Search the clear outer frame above and below the entire
                    # network.  Source and target trunks are filtered
                    # independently before combining them, keeping this rare
                    # fallback both reliable and reasonably fast.
                    node_top = max(node.y for node in self.layout.nodes.values())
                    node_bottom = min(node.y - NODE_HEIGHT for node in self.layout.nodes.values())
                    lane_offset = (2 + emergency_detour_links) * Y_SPACING
                    upper_outside = node_top + lane_offset
                    lower_outside = node_bottom - lane_offset
                    outside_levels = (
                        (lower_outside,)
                        if int(geometry["channel_direction"]) < 0
                        else (upper_outside,)
                    )

                    def corridor_candidates(origin: float) -> list[float]:
                        values = [origin]
                        for step in range(1, 121):
                            values.extend((origin + step * MIN_VERTICAL_CHANNEL_SPACING, origin - step * MIN_VERTICAL_CHANNEL_SPACING))
                        return values

                    for outside_y in outside_levels:
                        source_candidates = []
                        for source_x in corridor_candidates(source_x0):
                            partial = simplify_points([start, (source_x, start[1]), (source_x, outside_y)])
                            if not path_hits_node(link, partial) and path_has_channel_clearance(partial):
                                source_candidates.append(source_x)
                                if len(source_candidates) >= 12:
                                    break
                        target_candidates = []
                        for target_x in corridor_candidates(target_x0):
                            partial = simplify_points([(target_x, outside_y), (target_x, arrow_base[1]), arrow_base])
                            if not path_hits_node(link, partial) and path_has_channel_clearance(partial):
                                target_candidates.append(target_x)
                                if len(target_candidates) >= 12:
                                    break
                        combinations = sorted(
                            ((abs(source_x - target_x), source_x, target_x) for source_x in source_candidates for target_x in target_candidates),
                            key=lambda item: item[0],
                        )
                        for _, source_x, target_x in combinations:
                            candidate = simplify_points(
                                [start, (source_x, start[1]), (source_x, outside_y), (target_x, outside_y), (target_x, arrow_base[1]), arrow_base]
                            )
                            if path_is_clear(link, candidate):
                                selected_points = candidate
                                emergency_detour_links += 1
                                break
                        if selected_points is not None:
                            break
                if selected_points is None:
                    # Absolute last resort: preserve a valid relationship
                    # object and complete the drawing instead of aborting the
                    # whole conversion because of one unusually dense link.
                    forced_slot = forced_detour_links + 1
                    route_jitter = ((link.index + 1) * 0.017) % 1.0
                    # Keep endpoint stubs short; the endpoint port and unique
                    # outer Y lane provide separation without long overlaps.
                    forced_source_x = source_x0 + source_shift * (forced_slot * 2.5 + route_jitter)
                    forced_target_x = target_x0 + target_shift * (forced_slot * 2.5 + route_jitter)
                    lane_sign = 1 if int(geometry["channel_direction"]) > 0 else -1
                    if lane_sign < 0:
                        outside_y = min(start[1], arrow_base[1]) - NODE_HEIGHT - (2 + forced_slot) * 12.0 - route_jitter
                    else:
                        outside_y = max(start[1], arrow_base[1]) + (2 + forced_slot) * 12.0 + route_jitter
                    source_lane_y = start[1] + lane_sign * (forced_slot * 1.37 + route_jitter)
                    target_lane_y = arrow_base[1] + lane_sign * (forced_slot * 1.37 + route_jitter)
                    # Keep the first/last stubs short so a relationship does
                    # not run along the port level of an unrelated nearby
                    # node before turning into its private lane.
                    source_port_x = start[0] + source_shift * (4.0 + route_jitter)
                    target_port_x = arrow_base[0] + target_shift * (4.0 + route_jitter)
                    selected_points = simplify_points(
                        [
                            start,
                            (source_port_x, start[1]),
                            (source_port_x, source_lane_y),
                            (forced_source_x, source_lane_y),
                            (forced_source_x, outside_y),
                            (forced_target_x, outside_y),
                            (forced_target_x, target_lane_y),
                            (target_port_x, target_lane_y),
                            (target_port_x, arrow_base[1]),
                            arrow_base,
                        ]
                    )
                    forced_detour_links += 1
                route_mode = "detour"
                detour_links += 1

            geometry["points"] = selected_points
            geometry["label_position"] = label_position(selected_points)
            geometry["route_mode"] = route_mode
            geometry_by_link[link.index] = geometry
            register_path(selected_points)

        # Final whole-drawing de-overlap pass.  Local routing decisions cannot
        # always see a later emergency path from another zone.  Re-route any
        # remaining collinear segment onto a unique outer lane.
        accepted_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
        accepted_all_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
        accepted_link_segments: list[
            tuple[int, tuple[tuple[float, float], tuple[float, float]]]
        ] = []
        deoverlap_links = 0
        node_avoidance_links = 0
        between_node_corridor_links = 0
        link_by_index = {link.index: link for link in self.layout.links}
        node_top = max(node.y for node in self.layout.nodes.values())
        node_bottom = min(node.y - NODE_HEIGHT for node in self.layout.nodes.values())

        def segments_too_close(
            first: tuple[tuple[float, float], tuple[float, float]],
            second: tuple[tuple[float, float], tuple[float, float]],
        ) -> bool:
            (a, b), (c, d) = first, second
            first_vertical = math.isclose(a[0], b[0], abs_tol=1e-6)
            second_vertical = math.isclose(c[0], d[0], abs_tol=1e-6)
            first_horizontal = math.isclose(a[1], b[1], abs_tol=1e-6)
            second_horizontal = math.isclose(c[1], d[1], abs_tol=1e-6)
            if first_vertical and second_vertical:
                return abs(a[0] - c[0]) < MIN_VERTICAL_CHANNEL_SPACING and spans_overlap(a[1], b[1], c[1], d[1])
            if first_horizontal and second_horizontal:
                return abs(a[1] - c[1]) < MIN_HORIZONTAL_CHANNEL_SPACING and spans_overlap(a[0], b[0], c[0], d[0])
            return False

        def physically_touches(
            first: tuple[tuple[float, float], tuple[float, float]],
            second: tuple[tuple[float, float], tuple[float, float]],
        ) -> bool:
            (a, b), (c, d) = first, second
            first_vertical = math.isclose(a[0], b[0], abs_tol=1e-6)
            second_vertical = math.isclose(c[0], d[0], abs_tol=1e-6)
            if first_vertical and second_vertical:
                return math.isclose(a[0], c[0], abs_tol=1e-6) and spans_overlap(a[1], b[1], c[1], d[1])
            if not first_vertical and not second_vertical:
                return math.isclose(a[1], c[1], abs_tol=1e-6) and spans_overlap(a[0], b[0], c[0], d[0])
            vertical, horizontal = (first, second) if first_vertical else (second, first)
            (v1, v2), (h1, h2) = vertical, horizontal
            intersects = (
                min(h1[0], h2[0]) - 1e-6 <= v1[0] <= max(h1[0], h2[0]) + 1e-6
                and min(v1[1], v2[1]) - 1e-6 <= h1[1] <= max(v1[1], v2[1]) + 1e-6
            )
            if not intersects:
                return False
            # A clean X crossing is sometimes unavoidable in a dense AON
            # graph and does not imply a dependency.  A T/corner contact,
            # however, looks like one relationship joins another, so it is
            # prohibited whenever the intersection is an endpoint of
            # either segment.
            at_vertical_end = math.isclose(h1[1], v1[1], abs_tol=1e-6) or math.isclose(
                h1[1], v2[1], abs_tol=1e-6
            )
            at_horizontal_end = math.isclose(v1[0], h1[0], abs_tol=1e-6) or math.isclose(
                v1[0], h2[0], abs_tol=1e-6
            )
            return at_vertical_end or at_horizontal_end

        def proper_crossing_point(
            first: tuple[tuple[float, float], tuple[float, float]],
            second: tuple[tuple[float, float], tuple[float, float]],
        ) -> tuple[float, float] | None:
            """Return the point of an interior orthogonal X crossing."""
            (a, b), (c, d) = first, second
            first_vertical = math.isclose(a[0], b[0], abs_tol=1e-6)
            second_vertical = math.isclose(c[0], d[0], abs_tol=1e-6)
            if first_vertical == second_vertical:
                return None
            vertical, horizontal = (first, second) if first_vertical else (second, first)
            (v1, v2), (h1, h2) = vertical, horizontal
            if (
                min(h1[0], h2[0]) + 1e-6 < v1[0] < max(h1[0], h2[0]) - 1e-6
                and min(v1[1], v2[1]) + 1e-6 < h1[1] < max(v1[1], v2[1]) - 1e-6
            ):
                return (v1[0], h1[1])
            return None

        def properly_crosses(
            first: tuple[tuple[float, float], tuple[float, float]],
            second: tuple[tuple[float, float], tuple[float, float]],
        ) -> bool:
            return proper_crossing_point(first, second) is not None

        def crosses_same_source(link: Link, points: list[tuple[float, float]]) -> bool:
            # Port ordering already separates the source fan-out.  Do not turn
            # a remaining clean X crossing into a hard routing failure: dense
            # networks can have no alternative once node and no-touch rules
            # are applied.  Crossings remain a route-order preference rather
            # than an absolute feasibility condition.
            return False

        def touches_accepted(points: list[tuple[float, float]]) -> bool:
            segments = orthogonal_segments(points)
            return any(
                physically_touches(segment, accepted)
                for segment in segments
                for accepted in accepted_all_segments
            )

        def overlaps_accepted(points: list[tuple[float, float]]) -> bool:
            segments = orthogonal_segments(points)
            # The first/last segment is the short fan-out between a fixed node
            # port and its channel.  High-degree nodes cannot physically keep
            # full channel clearance inside a 64-unit edge; enforce clearance
            # after those short endpoint stubs.
            channel_segments = segments[1:-1] if len(segments) > 2 else segments
            too_close = any(
                segments_too_close(segment, accepted)
                for segment in channel_segments
                for accepted in accepted_segments
            )
            if too_close:
                return True

            # Endpoint fan-out stubs may use reduced spacing at a high-degree
            # node, but two different relationships must still never share,
            # touch, or cross the same geometric segment.

            return touches_accepted(points)

        def remove_unnecessary_detours(
            link: Link, points: list[tuple[float, float]]
        ) -> list[tuple[float, float]]:
            """Remove node-unnecessary U-turns and repeated orthogonal bends."""
            current = simplify_points(points)
            while len(current) > 2:
                replacement = None
                for gap in range(len(current) - 1, 1, -1):
                    for start_index in range(0, len(current) - gap):
                        end_index = start_index + gap
                        first = current[start_index]
                        last = current[end_index]
                        aligned = math.isclose(first[0], last[0], abs_tol=1e-6) or math.isclose(
                            first[1], last[1], abs_tol=1e-6
                        )
                        if not aligned:
                            continue
                        candidate = simplify_points(
                            current[: start_index + 1]
                            + [last]
                            + current[end_index + 1 :]
                        )
                        if len(candidate) >= len(current) or path_hits_node(link, candidate):
                            continue
                        replacement = candidate
                        if replacement is not None:
                            break
                    if replacement is not None:
                        break
                if replacement is None:
                    break
                current = replacement
            return current

        # Simplify each relationship before resolving conflicts with other
        # relationships.  This removes self-returning U shapes and staircase
        # bends that do not avoid any work node.
        for link_index, geometry in geometry_by_link.items():
            link = link_by_index[link_index]
            original = list(geometry["points"])
            simplified = remove_unnecessary_detours(link, original)
            if not bool(geometry.get("direct")) and not respects_endpoint_stubs(simplified):
                simplified = original
            geometry["points"] = simplified
            geometry["label_position"] = label_position(simplified)

        final_order = sorted(
            geometry_by_link,
            key=lambda index: (
                0 if bool(geometry_by_link[index].get("direct")) else 1,
                0
                if (
                    self.layout.nodes[link_by_index[index].pred_uid].task.critical
                    or self.layout.nodes[link_by_index[index].succ_uid].task.critical
                )
                else 1,
                0
                if path_hits_node(link_by_index[index], list(geometry_by_link[index]["points"]))
                else 1,
                -abs(
                    float(geometry_by_link[index]["start"][1])
                    - float(geometry_by_link[index]["base"][1])
                ),
                index,
            ),
        )
        for link_index in final_order:
            geometry = geometry_by_link[link_index]
            points = list(geometry["points"])
            link = link_by_index[link_index]
            hits_node = path_hits_node(link, points)
            bad_endpoint_stubs = (
                not bool(geometry.get("direct"))
                and not respects_endpoint_stubs(points)
            )
            if (
                hits_node
                or bad_endpoint_stubs
                or overlaps_accepted(points)
                or crosses_same_source(link, points)
            ):
                start = (float(geometry["start"][0]), float(geometry["start"][1]))
                arrow_base = (float(geometry["base"][0]), float(geometry["base"][1]))
                source_shift = 1 if geometry.get("source_side", "R") == "R" else -1
                target_shift = -int(geometry["arrow_direction"])
                chosen = None
                chosen_mode = ""

                # First retry the shortest dogleg with a wider trunk search.
                # This is the preferred shape for vertically separated nodes:
                # leave the source horizontally, turn directly toward the
                # target, then enter the arrow horizontally.
                for attempt in range(1, 121):
                    trunk_x = start[0] + source_shift * (
                        10.0 + attempt * MIN_VERTICAL_CHANNEL_SPACING
                    )
                    candidate = simplify_points(
                        [start, (trunk_x, start[1]), (trunk_x, arrow_base[1]), arrow_base]
                    )
                    if respects_endpoint_stubs(candidate) and not path_hits_node(link, candidate) and not overlaps_accepted(candidate) and not crosses_same_source(link, candidate):
                        chosen = candidate
                        break

                # For vertically separated work nodes, try the free band
                # between their boxes before going around the far side of the
                # target.  This yields H-V-H-V-H without the misleading
                # overshoot/U-turn seen when a link went above an upper target
                # (or below a lower target) and then came back.
                if chosen is None:
                    pred_node = self.layout.nodes[link.pred_uid]
                    succ_node = self.layout.nodes[link.succ_uid]
                    if succ_node.y > pred_node.y:
                        corridor_low = pred_node.y + 12.0
                        corridor_high = succ_node.y - NODE_HEIGHT - 12.0
                    elif succ_node.y < pred_node.y:
                        corridor_low = succ_node.y + 12.0
                        corridor_high = pred_node.y - NODE_HEIGHT - 12.0
                    else:
                        corridor_low = 1.0
                        corridor_high = 0.0
                    if corridor_low <= corridor_high:
                        midpoint = (corridor_low + corridor_high) / 2.0
                        corridor_levels = {
                            midpoint,
                            corridor_low,
                            corridor_high,
                        }
                        level_count = int(
                            (corridor_high - corridor_low)
                            // PREFERRED_HORIZONTAL_CHANNEL_SPACING
                        )
                        for level_index in range(1, min(8, level_count + 1)):
                            corridor_levels.add(
                                corridor_low
                                + level_index * PREFERRED_HORIZONTAL_CHANNEL_SPACING
                            )
                        source_x0 = float(geometry["source_bend_x"])
                        target_x0 = float(geometry["target_bend_x"])
                        x_pairs = sorted(
                            (
                                (
                                    source_step + target_step,
                                    source_x0
                                    + source_shift
                                    * source_step
                                    * MIN_VERTICAL_CHANNEL_SPACING,
                                    target_x0
                                    + target_shift
                                    * target_step
                                    * MIN_VERTICAL_CHANNEL_SPACING,
                                )
                                for source_step in range(8)
                                for target_step in range(8)
                            ),
                            key=lambda item: (
                                item[0],
                                abs(item[1] - item[2]),
                            ),
                        )
                        for corridor_y in sorted(
                            corridor_levels,
                            key=lambda level: abs(level - midpoint),
                        ):
                            for _, source_x, target_x in x_pairs:
                                candidate = simplify_points(
                                    [
                                        start,
                                        (source_x, start[1]),
                                        (source_x, corridor_y),
                                        (target_x, corridor_y),
                                        (target_x, arrow_base[1]),
                                        arrow_base,
                                    ]
                                )
                                if (
                                    respects_endpoint_stubs(candidate)
                                    and not path_hits_node(link, candidate)
                                    and not overlaps_accepted(candidate)
                                    and not crosses_same_source(link, candidate)
                                ):
                                    chosen = candidate
                                    chosen_mode = "between_nodes"
                                    between_node_corridor_links += 1
                                    break
                            if chosen is not None:
                                break

                # A compact four-turn corridor stays on the target side and
                # expands around nearby node rectangles.  This is preferred
                # to a drawing-wide U-shaped emergency route.
                for attempt in range(1, 401) if chosen is None else ():
                    side = -1 if arrow_base[1] <= start[1] else 1
                    local_edge = (
                        min(start[1], arrow_base[1]) - NODE_HEIGHT
                        if side < 0
                        else max(start[1], arrow_base[1])
                    )
                    outside_y = local_edge + side * (
                        14.0 + attempt * MIN_HORIZONTAL_CHANNEL_SPACING
                    )
                    source_x = start[0] + source_shift * (
                        10.0 + attempt * MIN_VERTICAL_CHANNEL_SPACING
                    )
                    target_x = arrow_base[0] + target_shift * (
                        10.0 + attempt * MIN_VERTICAL_CHANNEL_SPACING
                    )
                    candidate = simplify_points(
                        [
                            start,
                            (source_x, start[1]),
                            (source_x, outside_y),
                            (target_x, outside_y),
                            (target_x, arrow_base[1]),
                            arrow_base,
                        ]
                    )
                    if respects_endpoint_stubs(candidate) and not path_hits_node(link, candidate) and not overlaps_accepted(candidate) and not crosses_same_source(link, candidate):
                        chosen = candidate
                        break

                # If a node blocks every single-trunk option, use a local
                # detour on the target side (down for a lower successor, up
                # for an upper successor).  Never send a downward dependency
                # to the top outer frame merely to alternate lane usage.
                for attempt in range(1, 161) if chosen is None else ():
                    slot = deoverlap_links + attempt
                    side = -1 if arrow_base[1] <= start[1] else 1
                    local_edge = min(start[1], arrow_base[1]) - NODE_HEIGHT if side < 0 else max(start[1], arrow_base[1])
                    outside_y = local_edge + side * (14.0 + slot * MIN_HORIZONTAL_CHANNEL_SPACING)
                    source_exit_x = start[0] + source_shift * (
                        MIN_ENDPOINT_STUB_LENGTH + (attempt - 1) * 15.0
                    )
                    target_exit_x = arrow_base[0] + target_shift * (
                        MIN_ENDPOINT_STUB_LENGTH + (attempt - 1) * 15.0
                    )
                    source_lane_y = start[1] + side * (5.0 + slot * 11.0)
                    target_lane_y = arrow_base[1] + side * (5.0 + slot * 11.0)
                    source_outer_x = source_exit_x + source_shift * (8.0 + slot * 16.0)
                    target_outer_x = target_exit_x + target_shift * (8.0 + slot * 16.0)
                    candidate = simplify_points(
                        [
                            start,
                            (source_exit_x, start[1]),
                            (source_exit_x, source_lane_y),
                            (source_outer_x, source_lane_y),
                            (source_outer_x, outside_y),
                            (target_outer_x, outside_y),
                            (target_outer_x, target_lane_y),
                            (target_exit_x, target_lane_y),
                            (target_exit_x, arrow_base[1]),
                            arrow_base,
                        ]
                    )
                    if respects_endpoint_stubs(candidate) and not path_hits_node(link, candidate) and not overlaps_accepted(candidate) and not crosses_same_source(link, candidate):
                        chosen = candidate
                        break
                if chosen is None:
                    # Dense long links may not have the preferred full channel
                    # spacing.  Relax spacing only, never node clearance or
                    # physical line separation, and keep the route on the
                    # natural target side.
                    for attempt in range(1, 1001):
                        side = -1 if arrow_base[1] <= start[1] else 1
                        local_edge = (
                            min(start[1], arrow_base[1]) - NODE_HEIGHT
                            if side < 0
                            else max(start[1], arrow_base[1])
                        )
                        outside_y = local_edge + side * (18.0 + attempt * 4.37)
                        source_x = start[0] + source_shift * (12.0 + attempt * 5.13)
                        target_x = arrow_base[0] + target_shift * (12.0 + attempt * 5.13)
                        candidate = simplify_points(
                            [
                                start,
                                (source_x, start[1]),
                                (source_x, outside_y),
                                (target_x, outside_y),
                                (target_x, arrow_base[1]),
                                arrow_base,
                            ]
                        )
                        if respects_endpoint_stubs(candidate) and not path_hits_node(link, candidate) and not touches_accepted(candidate) and not crosses_same_source(link, candidate):
                            chosen = candidate
                            break
                if chosen is None:
                    # Source and target corridors can require different x
                    # offsets.  Search them independently as the final
                    # node-clear fallback instead of sending the line to the
                    # opposite side of the drawing.
                    side = -1 if arrow_base[1] <= start[1] else 1
                    local_edge = (
                        min(start[1], arrow_base[1]) - NODE_HEIGHT
                        if side < 0
                        else max(start[1], arrow_base[1])
                    )
                    for y_step in range(1, 301):
                        outside_y = local_edge + side * (
                            18.0 + y_step * MIN_HORIZONTAL_CHANNEL_SPACING
                        )
                        source_candidates: list[float] = []
                        target_candidates: list[float] = []
                        for x_step in range(1, 161):
                            source_x = start[0] + source_shift * (
                                10.0 + x_step * MIN_VERTICAL_CHANNEL_SPACING
                            )
                            source_partial = simplify_points(
                                [start, (source_x, start[1]), (source_x, outside_y)]
                            )
                            if not path_hits_node(link, source_partial):
                                source_candidates.append(source_x)
                            target_x = arrow_base[0] + target_shift * (
                                10.0 + x_step * MIN_VERTICAL_CHANNEL_SPACING
                            )
                            target_partial = simplify_points(
                                [(target_x, outside_y), (target_x, arrow_base[1]), arrow_base]
                            )
                            if not path_hits_node(link, target_partial):
                                target_candidates.append(target_x)
                            if len(source_candidates) >= 5 and len(target_candidates) >= 5:
                                break
                        for source_x in source_candidates[:5]:
                            for target_x in target_candidates[:5]:
                                candidate = simplify_points(
                                    [
                                        start,
                                        (source_x, start[1]),
                                        (source_x, outside_y),
                                        (target_x, outside_y),
                                        (target_x, arrow_base[1]),
                                        arrow_base,
                                    ]
                                )
                                if respects_endpoint_stubs(candidate) and not path_hits_node(link, candidate) and not touches_accepted(candidate) and not crosses_same_source(link, candidate):
                                    chosen = candidate
                                    break
                            if chosen is not None:
                                break
                        if chosen is not None:
                            break
                if chosen is not None:
                    shortened = remove_unnecessary_detours(link, chosen)
                    if respects_endpoint_stubs(shortened) and not path_hits_node(link, shortened) and not overlaps_accepted(shortened):
                        chosen = shortened
                    points = chosen
                    geometry["points"] = points
                    geometry["label_position"] = label_position(points)
                    geometry["route_mode"] = chosen_mode or "deoverlap_outer"
                    deoverlap_links += 1
                    if hits_node:
                        node_avoidance_links += 1
                else:
                    raise RuntimeError(f"No clear final route for link {link.index}")
            segments = orthogonal_segments(points)
            accepted_segments.extend(segments[1:-1] if len(segments) > 2 else segments)
            accepted_all_segments.extend(segments)
            accepted_link_segments.extend(
                (link.pred_uid, segment) for segment in segments[:2]
            )

        def path_self_intersects(points: list[tuple[float, float]]) -> bool:
            segments = orthogonal_segments(points)
            for first_index, first in enumerate(segments):
                for second_index in range(first_index + 2, len(segments)):
                    # The first and last segments of an open polyline are not
                    # adjacent; any meeting between them is also a loop.
                    second = segments[second_index]
                    (a, b), (c, d) = first, second
                    first_vertical = math.isclose(a[0], b[0], abs_tol=1e-6)
                    second_vertical = math.isclose(c[0], d[0], abs_tol=1e-6)
                    if first_vertical and second_vertical:
                        if math.isclose(a[0], c[0], abs_tol=1e-6) and spans_overlap(
                            a[1], b[1], c[1], d[1]
                        ):
                            return True
                    elif not first_vertical and not second_vertical:
                        if math.isclose(a[1], c[1], abs_tol=1e-6) and spans_overlap(
                            a[0], b[0], c[0], d[0]
                        ):
                            return True
                    else:
                        vertical, horizontal = (first, second) if first_vertical else (second, first)
                        (v1, v2), (h1, h2) = vertical, horizontal
                        if (
                            min(h1[0], h2[0]) - 1e-6 <= v1[0] <= max(h1[0], h2[0]) + 1e-6
                            and min(v1[1], v2[1]) - 1e-6 <= h1[1] <= max(v1[1], v2[1]) + 1e-6
                        ):
                            return True
            return False

        # Post-route corner reduction.  At this stage every relationship has
        # a valid path, so an L/direct shortcut is accepted only when it keeps
        # node clearance, removes self-loops, and remains physically separate
        # from every other relationship.
        post_simplified_links = 0
        for link_index in sorted(geometry_by_link):
            geometry = geometry_by_link[link_index]
            current = list(geometry["points"])
            if len(current) <= 2:
                continue
            link = link_by_index[link_index]
            other_segments = [
                segment
                for other_index, other_geometry in geometry_by_link.items()
                if other_index != link_index
                for segment in orthogonal_segments(list(other_geometry["points"]))
            ]
            same_source_other_segments = [
                segment
                for other_index, other_geometry in geometry_by_link.items()
                if (
                    other_index != link_index
                    and link_by_index[other_index].pred_uid == link.pred_uid
                )
                for segment in orthogonal_segments(list(other_geometry["points"]))[:2]
            ]
            raw_candidates: list[list[tuple[float, float]]] = []
            for start_index in range(len(current) - 2):
                for end_index in range(start_index + 2, len(current)):
                    first = current[start_index]
                    last = current[end_index]
                    bridges = [[last]] if (
                        math.isclose(first[0], last[0], abs_tol=1e-6)
                        or math.isclose(first[1], last[1], abs_tol=1e-6)
                    ) else [
                        [(last[0], first[1]), last],
                        [(first[0], last[1]), last],
                    ]
                    for bridge in bridges:
                        candidate = simplify_points(
                            current[: start_index + 1] + bridge + current[end_index + 1 :]
                        )
                        if len(candidate) < len(current):
                            raw_candidates.append(candidate)
            raw_candidates.sort(
                key=lambda candidate: (
                    len(candidate),
                    sum(
                        abs(second[0] - first[0]) + abs(second[1] - first[1])
                        for first, second in zip(candidate, candidate[1:])
                    ),
                )
            )
            for candidate in raw_candidates[:24]:
                if (
                    (not bool(geometry.get("direct")) and not respects_endpoint_stubs(candidate))
                    or path_hits_node(link, candidate)
                    or path_self_intersects(candidate)
                ):
                    continue
                candidate_segments = orthogonal_segments(candidate)
                if any(
                    physically_touches(segment, other)
                    for segment in candidate_segments
                    for other in other_segments
                ):
                    continue
                current = candidate
                geometry["points"] = current
                geometry["label_position"] = label_position(current)
                post_simplified_links += 1
                break

        # Revisit only the small same-period chain windows that the layout
        # pass reordered.  Once every other relationship is known, an
        # overshooting outer route can often be moved into the free band
        # between the two work boxes without disturbing the rest of the
        # drawing.  This late pass is deliberately narrow so dense unrelated
        # branches keep the proven global routing order.
        preferred_between_cleanup_links = 0
        preferred_link_indices = set(
            getattr(self.layout, "preferred_between_links", set())
        )
        for link_index in sorted(preferred_link_indices):
            geometry = geometry_by_link.get(link_index)
            if geometry is None or bool(geometry.get("direct")):
                continue
            link = link_by_index[link_index]
            current = list(geometry["points"])
            start = (float(geometry["start"][0]), float(geometry["start"][1]))
            arrow_base = (float(geometry["base"][0]), float(geometry["base"][1]))
            endpoint_low, endpoint_high = sorted((start[1], arrow_base[1]))
            current_horizontals = [
                first[1]
                for first, second in orthogonal_segments(current)[1:-1]
                if math.isclose(first[1], second[1], abs_tol=1e-6)
            ]
            if not any(
                level < endpoint_low - 1e-6 or level > endpoint_high + 1e-6
                for level in current_horizontals
            ):
                continue

            pred_node = self.layout.nodes[link.pred_uid]
            succ_node = self.layout.nodes[link.succ_uid]
            if succ_node.y > pred_node.y:
                corridor_low = pred_node.y + 12.0
                corridor_high = succ_node.y - NODE_HEIGHT - 12.0
            elif succ_node.y < pred_node.y:
                corridor_low = succ_node.y + 12.0
                corridor_high = pred_node.y - NODE_HEIGHT - 12.0
            else:
                continue
            if corridor_low > corridor_high:
                continue

            other_segments = [
                segment
                for other_index, other_geometry in geometry_by_link.items()
                if other_index != link_index
                for segment in orthogonal_segments(list(other_geometry["points"]))
            ]
            midpoint = (corridor_low + corridor_high) / 2.0
            corridor_levels = {corridor_low, midpoint, corridor_high}
            level = corridor_low
            while level <= corridor_high + 1e-6:
                corridor_levels.add(level)
                level += PREFERRED_HORIZONTAL_CHANNEL_SPACING

            source_shift = 1 if geometry.get("source_side", "R") == "R" else -1
            target_shift = -int(geometry["arrow_direction"])
            source_x0 = float(geometry["source_bend_x"])
            target_x0 = float(geometry["target_bend_x"])
            replacement = None
            for corridor_y in sorted(
                corridor_levels,
                key=lambda candidate_y: abs(candidate_y - midpoint),
            ):
                source_candidates: list[float] = []
                target_candidates: list[float] = []
                for x_step in range(20):
                    source_x = (
                        source_x0
                        + source_shift * x_step * 7.0
                    )
                    source_partial = simplify_points(
                        [start, (source_x, start[1]), (source_x, corridor_y)]
                    )
                    if not path_hits_node(link, source_partial):
                        source_candidates.append(source_x)
                    target_x = (
                        target_x0
                        + target_shift * x_step * 7.0
                    )
                    target_partial = simplify_points(
                        [
                            (target_x, corridor_y),
                            (target_x, arrow_base[1]),
                            arrow_base,
                        ]
                    )
                    if not path_hits_node(link, target_partial):
                        target_candidates.append(target_x)
                combinations = sorted(
                    (
                        (
                            abs(source_x - source_x0)
                            + abs(target_x - target_x0),
                            abs(source_x - target_x),
                            source_x,
                            target_x,
                        )
                        for source_x in source_candidates[:16]
                        for target_x in target_candidates[:16]
                    ),
                    key=lambda item: (item[0], item[1]),
                )
                for _, _, source_x, target_x in combinations:
                    candidate = simplify_points(
                        [
                            start,
                            (source_x, start[1]),
                            (source_x, corridor_y),
                            (target_x, corridor_y),
                            (target_x, arrow_base[1]),
                            arrow_base,
                        ]
                    )
                    if (
                        not respects_endpoint_stubs(candidate)
                        or path_hits_node(link, candidate)
                        or path_self_intersects(candidate)
                    ):
                        continue
                    candidate_segments = orthogonal_segments(candidate)
                    if any(
                        physically_touches(segment, other)
                        for segment in candidate_segments
                        for other in other_segments
                    ):
                        continue
                    replacement = candidate
                    break
                if replacement is not None:
                    break
            if replacement is not None:
                geometry["points"] = replacement
                geometry["label_position"] = label_position(replacement)
                geometry["route_mode"] = "between_nodes_cleanup"
                preferred_between_cleanup_links += 1

        # Spread only endpoint groups whose horizontal stubs still appear
        # glued to a neighboring endpoint stub.  This is a late visual pass:
        # complete relationship paths already exist, so a candidate vertical
        # shift is accepted only if the whole updated polyline remains clear.
        # Moving the first/last two points keeps every relationship a single
        # polyline and adds no extra corner.
        endpoint_group_spread_adjustments = 0
        endpoint_group_spread_links: set[int] = set()

        def endpoint_stub_records() -> list[
            tuple[int, str, tuple[tuple[float, float], tuple[float, float]]]
        ]:
            records = []
            for other_index, other_geometry in geometry_by_link.items():
                segments = orthogonal_segments(list(other_geometry["points"]))
                if not segments:
                    continue
                records.append((other_index, "pred", segments[0]))
                if len(segments) > 1:
                    records.append((other_index, "succ", segments[-1]))
            return records

        def horizontal_stub_score(
            candidate_records: list[
                tuple[int, str, tuple[tuple[float, float], tuple[float, float]]]
            ],
            fixed_records: list[
                tuple[int, str, tuple[tuple[float, float], tuple[float, float]]]
            ],
        ) -> tuple[int, float]:
            conflicts = 0
            deficit = 0.0
            comparisons = [
                (candidate, fixed)
                for candidate in candidate_records
                for fixed in fixed_records
            ]
            comparisons.extend(
                (candidate_records[first], candidate_records[second])
                for first in range(len(candidate_records))
                for second in range(first + 1, len(candidate_records))
            )
            for (_, _, first), (_, _, second) in comparisons:
                (a, b), (c, d) = first, second
                if not (
                    math.isclose(a[1], b[1], abs_tol=1e-6)
                    and math.isclose(c[1], d[1], abs_tol=1e-6)
                ):
                    continue
                overlap = min(max(a[0], b[0]), max(c[0], d[0])) - max(
                    min(a[0], b[0]), min(c[0], d[0])
                )
                if overlap <= 0.01:
                    continue
                gap = abs(a[1] - c[1])
                if gap >= PREFERRED_HORIZONTAL_CHANNEL_SPACING - 1e-6:
                    continue
                conflicts += 1
                deficit += (
                    PREFERRED_HORIZONTAL_CHANNEL_SPACING - gap
                ) * min(overlap, 80.0)
            return conflicts, deficit

        for (uid, _), endpoints in sorted(
            endpoint_groups.items(),
            key=lambda item: (
                self.layout.nodes[item[0][0]].y,
                self.layout.nodes[item[0][0]].x,
                item[0][1],
            ),
        ):
            group_roles = [
                (link_index, role)
                for link_index, role, _ in endpoints
                if link_index in geometry_by_link
            ]
            if not group_roles:
                continue
            group_indices = {link_index for link_index, _ in group_roles}
            all_endpoint_records = endpoint_stub_records()
            fixed_endpoint_records = [
                record
                for record in all_endpoint_records
                if record[0] not in group_indices
            ]
            current_records = [
                record
                for record in all_endpoint_records
                if (record[0], record[1]) in group_roles
            ]
            current_score = horizontal_stub_score(
                current_records, fixed_endpoint_records
            )
            if current_score[0] == 0:
                continue

            node = self.layout.nodes[uid]
            current_levels = []
            for link_index, role in group_roles:
                points = list(geometry_by_link[link_index]["points"])
                current_levels.append(points[0][1] if role == "pred" else points[-1][1])
            minimum_shift = max(
                node.y - NODE_HEIGHT + 4.0 - level for level in current_levels
            )
            maximum_shift = min(node.y - 4.0 - level for level in current_levels)
            candidate_shifts = {minimum_shift, maximum_shift}
            for step in range(-4, 5):
                candidate_shifts.add(step * PREFERRED_HORIZONTAL_CHANNEL_SPACING)
            for _, _, segment in current_records:
                level = segment[0][1]
                low_x, high_x = sorted((segment[0][0], segment[1][0]))
                for _, _, other_segment in fixed_endpoint_records:
                    other_low, other_high = sorted(
                        (other_segment[0][0], other_segment[1][0])
                    )
                    if min(high_x, other_high) - max(low_x, other_low) <= 0.01:
                        continue
                    other_level = other_segment[0][1]
                    candidate_shifts.add(
                        other_level + PREFERRED_HORIZONTAL_CHANNEL_SPACING - level
                    )
                    candidate_shifts.add(
                        other_level - PREFERRED_HORIZONTAL_CHANNEL_SPACING - level
                    )

            feasible_shifts = sorted(
                {
                    max(minimum_shift, min(maximum_shift, float(shift)))
                    for shift in candidate_shifts
                    if abs(float(shift)) > 1e-6
                },
                key=abs,
            )
            fixed_all_segments = [
                segment
                for other_index, other_geometry in geometry_by_link.items()
                if other_index not in group_indices
                for segment in orthogonal_segments(list(other_geometry["points"]))
            ]
            best: tuple[
                tuple[int, float, float],
                float,
                dict[int, list[tuple[float, float]]],
            ] | None = None
            for shift in feasible_shifts:
                candidate_paths: dict[int, list[tuple[float, float]]] = {}
                candidate_endpoint_records = []
                valid = True
                for link_index, role in group_roles:
                    geometry = geometry_by_link[link_index]
                    points = list(geometry["points"])
                    if len(points) < 2:
                        valid = False
                        break
                    if role == "pred":
                        points[0] = (points[0][0], points[0][1] + shift)
                        points[1] = (points[1][0], points[1][1] + shift)
                    else:
                        points[-2] = (points[-2][0], points[-2][1] + shift)
                        points[-1] = (points[-1][0], points[-1][1] + shift)
                    points = simplify_points(points)
                    link = link_by_index[link_index]
                    if (
                        not respects_endpoint_stubs(points)
                        or path_hits_node(link, points)
                        or path_self_intersects(points)
                    ):
                        valid = False
                        break
                    candidate_paths[link_index] = points
                    segments = orthogonal_segments(points)
                    endpoint_segment = segments[0] if role == "pred" else segments[-1]
                    candidate_endpoint_records.append(
                        (link_index, role, endpoint_segment)
                    )
                if not valid:
                    continue

                candidate_all_segments = [
                    (link_index, segment)
                    for link_index, points in candidate_paths.items()
                    for segment in orthogonal_segments(points)
                ]
                if any(
                    physically_touches(segment, other)
                    for _, segment in candidate_all_segments
                    for other in fixed_all_segments
                ):
                    continue
                if any(
                    first_index != second_index
                    and physically_touches(first, second)
                    for first_index, first in candidate_all_segments
                    for second_index, second in candidate_all_segments
                ):
                    continue

                score = horizontal_stub_score(
                    candidate_endpoint_records, fixed_endpoint_records
                )
                ranked_score = (score[0], score[1], abs(shift))
                if (score[0], score[1]) >= current_score:
                    continue
                if best is None or ranked_score < best[0]:
                    best = (ranked_score, shift, candidate_paths)
            if best is None:
                continue

            _, shift, candidate_paths = best
            for link_index, role in group_roles:
                geometry = geometry_by_link[link_index]
                points = candidate_paths[link_index]
                geometry["points"] = points
                geometry["label_position"] = label_position(points)
                if role == "pred":
                    geometry["start"] = (
                        float(geometry["start"][0]),
                        float(geometry["start"][1]) + shift,
                    )
                else:
                    geometry["base"] = (
                        float(geometry["base"][0]),
                        float(geometry["base"][1]) + shift,
                    )
                    geometry["tip"] = (
                        float(geometry["tip"][0]),
                        float(geometry["tip"][1]) + shift,
                    )
                endpoint_group_spread_links.add(link_index)
            endpoint_group_spread_adjustments += 1

        return geometry_by_link, {
            "direct_links": len(direct_geometry),
            "routed_links": len(routed_links),
            "two_turn_links": source_dogleg_links + target_dogleg_links,
            "source_dogleg_links": source_dogleg_links,
            "target_dogleg_links": target_dogleg_links,
            "four_turn_links": detour_links,
            "relaxed_detour_links": relaxed_detour_links,
            "emergency_detour_links": emergency_detour_links,
            "forced_detour_links": forced_detour_links,
            "deoverlap_links": deoverlap_links,
            "node_avoidance_links": node_avoidance_links,
            "between_node_corridor_links": between_node_corridor_links,
            "post_simplified_links": post_simplified_links,
            "preferred_between_cleanup_links": preferred_between_cleanup_links,
            "endpoint_group_spread_adjustments": endpoint_group_spread_adjustments,
            "endpoint_group_spread_links": len(endpoint_group_spread_links),
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
        if self.layout.time_axis:
            axis_y = self.layout.max_y - 66
            centers = [x for _, x in self.layout.time_axis]
            boundaries = self.layout.time_boundaries
            self.text("AON_TITLE", self.layout.min_x + 6, axis_y + 16, 5.5, self.layout.time_axis_title, max_width=150.0)
            self.line("AON_LANE", boundaries[0], axis_y, boundaries[-1], axis_y)
            for boundary in boundaries:
                self.line("AON_LANE", boundary, axis_y, boundary, self.layout.min_y + 12)
            for label, center_x in self.layout.time_axis:
                self.text("AON_TITLE", center_x - 25, axis_y + 9, 5.2, label, max_width=50.0)
        self.text("AON_TITLE", self.layout.min_x + 6, 10, 5.5, "要徑（中央水平帶）", max_width=180.0)
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

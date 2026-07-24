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
Y_SPACING = 145.0
ARROW_LENGTH = 4.8
ARROW_HALF_HEIGHT = 3.1
MIN_VERTICAL_CHANNEL_SPACING = 14.0
MIN_HORIZONTAL_CHANNEL_SPACING = 9.0
# Visual routing targets.  The smaller MIN_* values remain emergency hard
# limits for exceptionally dense regions; ordinary paths and post-processing
# use the preferred distance so parallel segments and nearby bends remain
# clearly distinguishable.
PREFERRED_HORIZONTAL_CHANNEL_SPACING = 18.0
PREFERRED_VERTICAL_CHANNEL_SPACING = 18.0
# Every non-direct relationship must visibly leave and enter a work node with
# a horizontal segment.  A vertical channel may never begin on the node edge.
MIN_ENDPOINT_STUB_LENGTH = 28.0
ENDPOINT_CHANNEL_STEP = 18.0
# Non-endpoint work boxes are hard obstacles with a visible safety halo.
NODE_ROUTE_CLEARANCE = 12.0


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


def enlarge_layout(
    layout: DrawingLayout,
    time_scale: str = "month",
    *,
    downstream_priority: bool = True,
) -> DrawingLayout:
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

    # Measure the complete reachable branch, not only the immediate number of
    # successors.  At a branch, the route carrying more downstream work is the
    # preferred non-critical horizontal mainline.  In the TEST build this is a
    # general chain rule rather than a final tie-break; all later crossing,
    # collision and routing checks remain the acceptance authority.
    downstream_members: dict[int, frozenset[int]] = {}

    def reachable_downstream(uid: int) -> frozenset[int]:
        cached = downstream_members.get(uid)
        if cached is not None:
            return cached
        members: set[int] = set()
        for successor in adjacency.get(uid, []):
            members.add(successor)
            members.update(reachable_downstream(successor))
        result = frozenset(members)
        downstream_members[uid] = result
        return result

    downstream_count = {
        uid: len(reachable_downstream(uid))
        for uid in layout.nodes
    }

    for pred_uid, successors in adjacency.items():
        pred = layout.nodes[pred_uid].task
        successors.sort(
            key=lambda uid: (
                (
                    -downstream_count[uid],
                    period_index[task_period[uid]] - period_index[task_period[pred_uid]],
                    pred.zone != layout.nodes[uid].task.zone,
                    not (pred.critical and layout.nodes[uid].task.critical),
                    abs(pred.task_id - layout.nodes[uid].task.task_id),
                    uid,
                )
                if downstream_priority
                else (
                    period_index[task_period[uid]] - period_index[task_period[pred_uid]],
                    pred.zone != layout.nodes[uid].task.zone,
                    not (pred.critical and layout.nodes[uid].task.critical),
                    abs(pred.task_id - layout.nodes[uid].task.task_id),
                    uid,
                )
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
    # Keep the proven v1.7.15 maximum-matching algorithm, but let downstream
    # workload lead successor preference in this TEST build.
    downstream_mainline_choices = sum(
        len(adjacency.get(uid, [])) > 1
        and downstream_count[successor]
        == max(downstream_count[candidate] for candidate in adjacency[uid])
        for uid, successor in selected_successor.items()
    )
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

    # Barycentric row sweeps reduce crossings without changing the requested
    # zone order (zones with fewer activities remain above larger zones).
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

    for _ in range(6):
        flat_rows = [row for zone in zone_order for row in noncritical_rows_by_zone[zone]]
        split_hint = (len(flat_rows) + 1) // 2
        provisional = {
            row: (split_hint - index if index < split_hint else -(index - split_hint + 1))
            for index, row in enumerate(flat_rows)
        }
        for zone in zone_order:
            def barycenter(row: int) -> float:
                neighbors = linked_rows.get(row, [])
                if not neighbors:
                    return float(provisional.get(row, 0))
                return sum(0.0 if neighbor is None else provisional.get(neighbor, 0) for neighbor in neighbors) / len(neighbors)
            noncritical_rows_by_zone[zone].sort(
                key=lambda row: (-barycenter(row), len(row_members[row]), min(layout.nodes[uid].task.task_id for uid in row_members[row]))
            )
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
            if uid in critical_uids or not predecessors.get(uid) or node.task.row == 0:
                continue
            weighted_neighbors = [
                (layout.nodes[other], 3.0) for other in predecessors[uid]
            ] + [
                (layout.nodes[other], 1.0) for other in successors.get(uid, [])
            ]
            current_cost = sum(
                weight * abs(node.y - other.y) for other, weight in weighted_neighbors
            ) + abs(node.y) * 0.08
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
                ) + abs(candidate_y) * 0.08
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

    # Final drawing-wide row review.  Packing and zone balancing above are
    # intentionally coarse; they can still leave a short FS chain alternating
    # up/down between rows even though its time slots do not overlap.  Make
    # small, collision-free row moves now, before routing, and score them by
    # the relationships they straighten and the crossings they remove.
    # Critical activities stay on row zero and no activity changes its X/time
    # position.
    layout_review_moves = 0
    whole_chain_review_moves = 0
    whole_chains_aligned = 0
    review_links = [
        link
        for link in layout.links
        if link.relation == "FS" and link.pred_uid in layout.nodes and link.succ_uid in layout.nodes
    ]
    incident_links: dict[int, list[Link]] = collections.defaultdict(list)
    for link in review_links:
        incident_links[link.pred_uid].append(link)
        incident_links[link.succ_uid].append(link)

    def review_point(uid: int, override_uid: int | None = None, override_row: int | None = None) -> tuple[float, float]:
        node = layout.nodes[uid]
        row = override_row if uid == override_uid and override_row is not None else node.task.row
        return (node.x + NODE_WIDTH / 2.0, row * Y_SPACING - NODE_HEIGHT / 2.0)

    def proper_review_crossing(
        first: Link,
        second: Link,
        override_uid: int | None = None,
        override_row: int | None = None,
    ) -> bool:
        if {first.pred_uid, first.succ_uid} & {second.pred_uid, second.succ_uid}:
            return False
        def proxy_segments(link: Link) -> list[tuple[tuple[float, float], tuple[float, float]]]:
            start = review_point(link.pred_uid, override_uid, override_row)
            end = review_point(link.succ_uid, override_uid, override_row)
            if math.isclose(start[1], end[1], abs_tol=1e-6):
                return [(start, end)]
            # Approximate the actual H-V-H router rather than using a diagonal
            # chord.  The old diagonal proxy missed the important case where
            # one long vertical trunk cuts through several horizontal chains.
            direction = 1.0 if end[0] >= start[0] else -1.0
            minimum_stub = MIN_ENDPOINT_STUB_LENGTH
            available = abs(end[0] - start[0])
            if available >= 2.0 * minimum_stub:
                trunk_x = (start[0] + end[0]) / 2.0
            else:
                trunk_x = start[0] + direction * minimum_stub
            return [
                (start, (trunk_x, start[1])),
                ((trunk_x, start[1]), (trunk_x, end[1])),
                ((trunk_x, end[1]), end),
            ]

        def orthogonal_crosses(
            one: tuple[tuple[float, float], tuple[float, float]],
            two: tuple[tuple[float, float], tuple[float, float]],
        ) -> bool:
            (a, b), (c, d) = one, two
            one_horizontal = math.isclose(a[1], b[1], abs_tol=1e-6)
            two_horizontal = math.isclose(c[1], d[1], abs_tol=1e-6)
            if one_horizontal == two_horizontal:
                return False
            horizontal = one if one_horizontal else two
            vertical = two if one_horizontal else one
            hx1, hx2 = sorted((horizontal[0][0], horizontal[1][0]))
            vy1, vy2 = sorted((vertical[0][1], vertical[1][1]))
            vx = vertical[0][0]
            hy = horizontal[0][1]
            # Endpoint touches are handled by routing; the layout score counts
            # only a true interior crossing of two unrelated relationships.
            return (
                hx1 + 1e-6 < vx < hx2 - 1e-6
                and vy1 + 1e-6 < hy < vy2 - 1e-6
            )

        return any(
            orthogonal_crosses(one, two)
            for one in proxy_segments(first)
            for two in proxy_segments(second)
        )

    occupied = {
        (round(node.x, 6), node.task.row): uid
        for uid, node in layout.nodes.items()
    }

    def review_metrics() -> tuple[int, int, int]:
        horizontal = sum(
            layout.nodes[link.pred_uid].task.row == layout.nodes[link.succ_uid].task.row
            for link in review_links
        )
        crossings = 0
        for first_index, first in enumerate(review_links):
            for second in review_links[first_index + 1 :]:
                crossings += proper_review_crossing(first, second)
        reversals = 0
        for middle_uid in layout.nodes:
            incoming_rows = [
                layout.nodes[link.pred_uid].task.row
                for link in incident_links.get(middle_uid, [])
                if link.succ_uid == middle_uid
            ]
            outgoing_rows = [
                layout.nodes[link.succ_uid].task.row
                for link in incident_links.get(middle_uid, [])
                if link.pred_uid == middle_uid
            ]
            middle_row = layout.nodes[middle_uid].task.row
            reversals += sum(
                (pred_row - middle_row) * (succ_row - middle_row) > 0
                for pred_row in incoming_rows
                for succ_row in outgoing_rows
            )
        return horizontal, crossings, reversals

    review_before = review_metrics()

    # Review complete non-critical FS chains before individual activities.
    # Moving one activity at a time cannot clear an occupied destination row
    # and often leaves an otherwise straight sequence split across lanes.  A
    # chain candidate is therefore evaluated as one layout operation, with
    # drawing-wide crossings as the primary score and horizontal continuity as
    # the next criterion.  X/time coordinates never change.
    fs_predecessors: dict[int, list[int]] = collections.defaultdict(list)
    fs_successors: dict[int, list[int]] = collections.defaultdict(list)
    for link in review_links:
        if link.pred_uid in critical_uids or link.succ_uid in critical_uids:
            continue
        fs_predecessors[link.succ_uid].append(link.pred_uid)
        fs_successors[link.pred_uid].append(link.succ_uid)

    # Reuse the maximum-matching chains built by the first layout pass.  They
    # remain valid horizontal-chain candidates even when one of their nodes
    # has an additional incoming or outgoing branch.  The older one-in/one-out
    # extraction split exactly the branch-plus-main-chain arrangements that
    # should be reviewed as a single block.
    review_chains = [
        [uid for uid in chain if uid not in critical_uids]
        for chain in chains
    ]
    review_chains = [chain for chain in review_chains if len(chain) >= 2]

    def review_global_score() -> tuple[int, int, int, int]:
        horizontal, crossings, reversals = review_metrics()
        vertical_span = sum(
            abs(
                layout.nodes[link.pred_uid].task.row
                - layout.nodes[link.succ_uid].task.row
            )
            for link in review_links
        )
        return crossings, -horizontal, reversals, vertical_span

    max_review_row = max(abs(node.task.row) for node in layout.nodes.values())
    occupied = {
        (round(node.x, 6), node.task.row): uid
        for uid, node in layout.nodes.items()
    }
    for _ in range(4):
        chain_changed = False
        for chain in sorted(review_chains, key=lambda item: (-len(item), layout.nodes[item[0]].task.task_id)):
            original_rows = {uid: layout.nodes[uid].task.row for uid in chain}
            current_score = review_global_score()
            candidate_rows = {
                row
                for row in range(-max_review_row - 2, max_review_row + 3)
                if row != 0
            }
            candidate_rows.update(original_rows.values())
            feasible_rows = []
            for candidate_row in candidate_rows:
                if all(
                    (owner := occupied.get((round(layout.nodes[uid].x, 6), candidate_row)))
                    is None
                    or owner in chain
                    for uid in chain
                ):
                    feasible_rows.append(candidate_row)
            best_row = None
            best_score = current_score
            best_ranked = current_score + (
                min(abs(row) for row in original_rows.values()),
                min(original_rows.values()),
            )
            for candidate_row in feasible_rows:
                for uid in chain:
                    layout.nodes[uid].task.row = candidate_row
                    layout.nodes[uid].y = candidate_row * Y_SPACING
                candidate_score = review_global_score()
                ranked = candidate_score + (abs(candidate_row), candidate_row)
                if ranked < best_ranked:
                    best_row = candidate_row
                    best_score = candidate_score
                    best_ranked = ranked
                for uid, original_row in original_rows.items():
                    layout.nodes[uid].task.row = original_row
                    layout.nodes[uid].y = original_row * Y_SPACING
            if best_row is None:
                continue
            for uid, original_row in original_rows.items():
                occupied.pop((round(layout.nodes[uid].x, 6), original_row), None)
            for uid in chain:
                layout.nodes[uid].task.row = best_row
                layout.nodes[uid].y = best_row * Y_SPACING
                occupied[(round(layout.nodes[uid].x, 6), best_row)] = uid
            whole_chain_review_moves += sum(
                original_row != best_row for original_row in original_rows.values()
            )
            whole_chains_aligned += 1
            chain_changed = True
        if not chain_changed:
            break

    # Compare complete row ordering, not chain length, WBS order, or the
    # original upper/lower position.  Adjacent row exchanges are sufficient
    # to reach any useful local permutation while keeping the search bounded
    # on large schedules.  A swap is accepted only when the drawing-wide
    # orthogonal crossing score improves (then horizontal continuity,
    # reversals, and vertical span break ties).
    row_swap_passes = 0
    row_swap_moves = 0
    for _ in range(8):
        rows = sorted({node.task.row for node in layout.nodes.values() if node.task.row != 0})
        improved = False
        for first_row, second_row in zip(rows, rows[1:]):
            first_uids = [uid for uid, node in layout.nodes.items() if node.task.row == first_row]
            second_uids = [uid for uid, node in layout.nodes.items() if node.task.row == second_row]
            if not first_uids or not second_uids:
                continue
            before = review_global_score()
            for uid in first_uids:
                layout.nodes[uid].task.row = second_row
                layout.nodes[uid].y = second_row * Y_SPACING
            for uid in second_uids:
                layout.nodes[uid].task.row = first_row
                layout.nodes[uid].y = first_row * Y_SPACING
            after = review_global_score()
            if after < before:
                row_swap_moves += len(first_uids) + len(second_uids)
                row_swap_passes += 1
                improved = True
            else:
                for uid in first_uids:
                    layout.nodes[uid].task.row = first_row
                    layout.nodes[uid].y = first_row * Y_SPACING
                for uid in second_uids:
                    layout.nodes[uid].task.row = second_row
                    layout.nodes[uid].y = second_row * Y_SPACING
        if not improved:
            break

    occupied = {
        (round(node.x, 6), node.task.row): uid
        for uid, node in layout.nodes.items()
    }

    def review_local_cost(uid: int, candidate_row: int) -> float:
        affected = incident_links.get(uid, [])
        cost = abs(candidate_row) * 0.35
        for link in affected:
            other_uid = link.succ_uid if link.pred_uid == uid else link.pred_uid
            other_row = layout.nodes[other_uid].task.row
            row_gap = abs(candidate_row - other_row)
            # A horizontal dependency removes a vertical trunk and two bends,
            # so it is considerably more valuable than merely shortening one.
            cost += 0.0 if row_gap == 0 else 72.0 + row_gap * 5.0
            for other in review_links:
                if other is link or uid in (other.pred_uid, other.succ_uid):
                    continue
                if proper_review_crossing(link, other, uid, candidate_row):
                    cost += 240.0

        # Penalize a peak/valley at this operation: arrows that arrive from
        # one side and immediately return to it are the visual up/down/up case.
        incoming_rows = [
            layout.nodes[link.pred_uid].task.row
            for link in affected
            if link.succ_uid == uid
        ]
        outgoing_rows = [
            layout.nodes[link.succ_uid].task.row
            for link in affected
            if link.pred_uid == uid
        ]
        for pred_row in incoming_rows:
            for succ_row in outgoing_rows:
                if (pred_row - candidate_row) * (succ_row - candidate_row) > 0:
                    cost += 55.0
        return cost

    for _ in range(8):
        changed = False
        for uid, node in sorted(
            layout.nodes.items(),
            key=lambda item: (
                -len(incident_links.get(item[0], [])),
                item[1].task.rank,
                item[1].task.task_id,
            ),
        ):
            if uid in critical_uids or not incident_links.get(uid):
                continue
            neighbor_rows = {
                layout.nodes[
                    link.succ_uid if link.pred_uid == uid else link.pred_uid
                ].task.row
                for link in incident_links[uid]
            }
            candidates = {node.task.row, node.task.row - 1, node.task.row + 1}
            candidates.update(row for row in neighbor_rows if row != 0)
            if neighbor_rows:
                ordered_neighbor_rows = sorted(neighbor_rows)
                candidates.add(ordered_neighbor_rows[len(ordered_neighbor_rows) // 2])
            candidates.discard(0)
            feasible = []
            for candidate_row in candidates:
                owner = occupied.get((round(node.x, 6), candidate_row))
                if owner is None or owner == uid:
                    feasible.append(candidate_row)
            if not feasible:
                continue
            current_cost = review_local_cost(uid, node.task.row)
            best_row, best_cost = min(
                ((row, review_local_cost(uid, row)) for row in feasible),
                key=lambda item: (item[1], abs(item[0]), item[0]),
            )
            if best_row == node.task.row or best_cost >= current_cost - 8.0:
                continue
            occupied.pop((round(node.x, 6), node.task.row), None)
            node.task.row = best_row
            node.y = best_row * Y_SPACING
            occupied[(round(node.x, 6), best_row)] = uid
            layout_review_moves += 1
            changed = True
        if not changed:
            break

    hierarchical_chain_rows = 0
    hierarchical_chain_moves = 0
    if downstream_priority:
        # TEST4 layout stage: keep the Project critical path on row 0, then
        # place complete non-critical main/branch chains as horizontal units.
        # Long chains are placed first and closest to their connected trunk.
        # Shorter branches reuse a row only when their complete time spans do
        # not overlap; otherwise a new upper/lower row is inserted.
        noncritical_chains = [
            [uid for uid in chain if uid not in critical_uids]
            for chain in chains
        ]
        noncritical_chains = [chain for chain in noncritical_chains if chain]
        chain_of = {
            uid: chain_index
            for chain_index, chain in enumerate(noncritical_chains)
            for uid in chain
        }
        chain_spans: dict[int, set[int]] = {}
        chain_anchor_rank: dict[int, float] = {}
        for chain_index, chain in enumerate(noncritical_chains):
            ranks = sorted(period_index[task_period[uid]] for uid in chain)
            chain_spans[chain_index] = set(range(ranks[0], ranks[-1] + 1))
            chain_anchor_rank[chain_index] = sum(ranks) / len(ranks)

        chain_neighbors: dict[int, collections.Counter[int | None]] = (
            collections.defaultdict(collections.Counter)
        )
        parent_chains: dict[int, set[int | None]] = collections.defaultdict(set)
        for link in layout.links:
            pred_chain = chain_of.get(link.pred_uid)
            succ_chain = chain_of.get(link.succ_uid)
            if pred_chain == succ_chain:
                continue
            if pred_chain is not None:
                other = succ_chain if link.succ_uid not in critical_uids else None
                chain_neighbors[pred_chain][other] += 1
            if succ_chain is not None:
                other = pred_chain if link.pred_uid not in critical_uids else None
                chain_neighbors[succ_chain][other] += 1
                parent_chains[succ_chain].add(other)

        ordered_chain_indices = sorted(
            range(len(noncritical_chains)),
            key=lambda chain_index: (
                -len(noncritical_chains[chain_index]),
                chain_anchor_rank[chain_index],
                layout.nodes[noncritical_chains[chain_index][0]].task.task_id,
            ),
        )
        assigned_chain_row: dict[int, int] = {}
        occupied_ranks_by_row: dict[int, set[int]] = collections.defaultdict(set)
        side_load = {-1: 0, 1: 0}
        sibling_side_load: collections.Counter[tuple[int | None, int]] = (
            collections.Counter()
        )
        max_candidate_row = max(8, len(noncritical_chains) + 2)
        for chain_index in ordered_chain_indices:
            span = chain_spans[chain_index]
            placed_neighbors = [
                (
                    0 if neighbor is None else assigned_chain_row[neighbor],
                    weight,
                    neighbor,
                )
                for neighbor, weight in chain_neighbors.get(
                    chain_index, collections.Counter()
                ).items()
                if neighbor is None or neighbor in assigned_chain_row
            ]
            candidates = [
                row
                for magnitude in range(1, max_candidate_row + 1)
                for row in (magnitude, -magnitude)
                if span.isdisjoint(occupied_ranks_by_row[row])
            ]
            if not candidates:
                candidates = [max_candidate_row + 1, -(max_candidate_row + 1)]

            def hierarchy_cost(row: int) -> tuple[float, int, int]:
                sign = 1 if row > 0 else -1
                connection_cost = sum(
                    weight * abs(row - neighbor_row)
                    for neighbor_row, weight, _ in placed_neighbors
                )
                # Branches sharing a parent are distributed above and below
                # when the parent is the centered critical trunk.  Away from
                # the critical row, distance naturally keeps children near
                # their own horizontal branch trunk.
                sibling_cost = sum(
                    sibling_side_load[(parent, sign)]
                    for parent in parent_chains.get(chain_index, set())
                )
                balance_cost = side_load[sign] / max(
                    1, len(noncritical_chains[chain_index])
                )
                return (
                    connection_cost * 40.0
                    + sibling_cost * 18.0
                    + balance_cost
                    + abs(row) * 2.0,
                    abs(row),
                    row,
                )

            chosen_row = min(candidates, key=hierarchy_cost)
            assigned_chain_row[chain_index] = chosen_row
            occupied_ranks_by_row[chosen_row].update(span)
            sign = 1 if chosen_row > 0 else -1
            side_load[sign] += len(noncritical_chains[chain_index])
            for parent in parent_chains.get(chain_index, set()):
                sibling_side_load[(parent, sign)] += 1
            for uid in noncritical_chains[chain_index]:
                node = layout.nodes[uid]
                hierarchical_chain_moves += node.task.row != chosen_row
                node.task.row = chosen_row
                node.y = chosen_row * Y_SPACING
            hierarchical_chain_rows = max(
                hierarchical_chain_rows, abs(chosen_row)
            )

    review_after = review_metrics()

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
        "downstream_mainline_choices": downstream_mainline_choices,
        "chains": len(chains),
        "rows": max(row_assignment.values()) + 1,
        "group_rows": group_row_counts,
        "time_periods": len(periods),
        "time_scale": scale,
        "final_layout_review_moves": layout_review_moves,
        "whole_chain_review_moves": whole_chain_review_moves,
        "whole_chains_aligned": whole_chains_aligned,
        "row_swap_passes": row_swap_passes,
        "row_swap_moves": row_swap_moves,
        "hierarchical_chain_rows": hierarchical_chain_rows,
        "hierarchical_chain_moves": hierarchical_chain_moves,
        "horizontal_fs_before_review": review_before[0],
        "horizontal_fs_after_review": review_after[0],
        "crossing_proxy_before_review": review_before[1],
        "crossing_proxy_after_review": review_after[1],
        "vertical_reversals_before_review": review_before[2],
        "vertical_reversals_after_review": review_after[2],
    }

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
        margin = NODE_ROUTE_CLEARANCE
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

        # A direct horizontal relationship is part of the same source fan as
        # upward and downward relationships.  When a source has both direct
        # and routed successors, reserve the direct port at its true
        # top-to-bottom target rank instead of forcing it into a centre lane.
        # This prevents the routed ports from crowding or crossing around a
        # separately chosen horizontal level.
        all_source_endpoints: dict[tuple[int, str], list[Link]] = collections.defaultdict(list)
        for link in self.layout.links:
            source_side, _ = self.endpoint_sides(link)
            all_source_endpoints[(link.pred_uid, source_side)].append(link)
        global_source_port_y: dict[int, float] = {}
        for (uid, _), links in all_source_endpoints.items():
            ordered = sorted(
                links,
                key=lambda link: (
                    -self.layout.nodes[link.succ_uid].y,
                    self.layout.nodes[link.succ_uid].task.task_id,
                    link.index,
                ),
            )
            node_top = self.layout.nodes[uid].y
            levels = [
                node_top - 4.0 - (index + 1) * (NODE_HEIGHT - 8.0) / (len(ordered) + 1)
                for index in range(len(ordered))
            ]
            for link, level in zip(ordered, levels):
                global_source_port_y[link.index] = level
                if link.index in direct_level:
                    direct_level[link.index] = level

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
                # When every routed counterpart is on one side of a reserved
                # direct relationship, keep every routed port on that same
                # side.  Merely removing the nearest generic pool slot can
                # leave one upper branch below the direct line, forcing an
                # immediate crossing at the source.
                node_bottom = node_top - NODE_HEIGHT
                if prefer_upper_ports:
                    low = max(reserved) + 4.0
                    high = node_top - 4.0
                    if high - low >= max(4.0, count * 3.0):
                        pool = [
                            high - (index + 1) * (high - low) / (count + 1)
                            for index in range(count)
                        ]
                elif prefer_lower_ports:
                    high = min(reserved) - 4.0
                    low = node_bottom + 4.0
                    if high - low >= max(4.0, count * 3.0):
                        pool = [
                            high - (index + 1) * (high - low) / (count + 1)
                            for index in range(count)
                        ]
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
                if role == "pred":
                    # The source-side port has already been assigned from the
                    # complete successor order, including direct horizontal
                    # relationships.  Never let the routed-only endpoint pool
                    # overwrite that global rank.
                    adjusted_level = global_source_port_y[link_index]
                port_y[(link_index, role)] = adjusted_level
                port_slot[(link_index, role)] = slot
                port_count[(link_index, role)] = count

        # Plan source fan-outs as one complete group per source side.  Upward,
        # same-row and downward successors share one target order; higher
        # targets use higher ports.  Bend distance is mirrored by direction:
        # the highest upward and lowest downward targets turn nearest. Nearby source
        # nodes in the same time column receive non-overlapping X corridor
        # bands so their fan-outs cannot interleave.
        source_groups: dict[tuple[int, str], list[Link]] = collections.defaultdict(list)
        for link in routed_links:
            source_side, _ = self.endpoint_sides(link)
            source_groups[(link.pred_uid, source_side)].append(link)

        fan_turn_rank: dict[int, int] = {}
        fan_direction: dict[int, int] = {}
        for (uid, side), group_links in source_groups.items():
            source_y = self.layout.nodes[uid].y
            for link in group_links:
                target_y = self.layout.nodes[link.succ_uid].y
                direction = (target_y > source_y) - (target_y < source_y)
                fan_direction[link.index] = direction
            by_direction: dict[int, list[Link]] = collections.defaultdict(list)
            for link in group_links:
                by_direction[fan_direction[link.index]].append(link)
            for direction, directional_links in by_direction.items():
                ordered_links = sorted(
                    directional_links,
                    key=lambda item: (
                        -self.layout.nodes[item.succ_uid].y
                        if direction > 0
                        else self.layout.nodes[item.succ_uid].y,
                        self.layout.nodes[item.succ_uid].task.task_id,
                        item.index,
                    ),
                )
                for position, link in enumerate(ordered_links):
                    fan_turn_rank[link.index] = position

        source_band_base: dict[tuple[int, str], float] = collections.defaultdict(float)
        # Build regional competition groups from overlapping horizontal fan
        # spans, not only identical source X coordinates.  Two nearby sources
        # whose candidate corridors cover the same X range must coordinate
        # their vertical bands even when their nodes occupy adjacent columns.
        fan_records: list[
            tuple[tuple[int, str], str, float, float, float]
        ] = []
        for key, group_links in source_groups.items():
            if len(group_links) < 2:
                continue
            uid, side = key
            node = self.layout.nodes[uid]
            source_x = node.x + NODE_WIDTH if side == "R" else node.x
            target_xs = []
            for link in group_links:
                _, target_side = self.endpoint_sides(link)
                target = self.layout.nodes[link.succ_uid]
                target_xs.append(
                    target.x if target_side == "L" else target.x + NODE_WIDTH
                )
            low = min([source_x] + target_xs) - MIN_ENDPOINT_STUB_LENGTH
            high = max([source_x] + target_xs) + MIN_ENDPOINT_STUB_LENGTH
            fan_records.append((key, side, low, high, node.y))

        regional_clusters: list[
            list[tuple[tuple[int, str], str, float, float, float]]
        ] = []
        for record in sorted(fan_records, key=lambda item: (item[1], item[2], -item[4])):
            _, side, low, high, source_y = record
            matching_cluster = None
            for cluster in regional_clusters:
                first = cluster[0]
                if first[1] != side:
                    continue
                cluster_low = min(item[2] for item in cluster)
                cluster_high = max(item[3] for item in cluster)
                vertically_near = any(
                    abs(source_y - item[4]) <= 4.0 * Y_SPACING
                    for item in cluster
                )
                if vertically_near and min(high, cluster_high) >= max(low, cluster_low):
                    matching_cluster = cluster
                    break
            if matching_cluster is None:
                regional_clusters.append([record])
            else:
                matching_cluster.append(record)

        for cluster in regional_clusters:
            cursor = 0.0
            for key, _, _, _, _ in sorted(
                cluster,
                key=lambda item: (
                    item[4],
                    self.layout.nodes[item[0][0]].task.task_id,
                ),
                reverse=True,
            ):
                source_band_base[key] = cursor
                cursor += (
                    max(1, len(source_groups[key]))
                    * ENDPOINT_CHANNEL_STEP
                    + PREFERRED_VERTICAL_CHANNEL_SPACING
                )

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
                + source_band_base[
                    (
                        link.pred_uid,
                        source_side,
                    )
                ]
                + fan_turn_rank.get(link.index, 0) * source_spacing
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
                    node.x - NODE_ROUTE_CLEARANCE,
                    node.y - NODE_HEIGHT - NODE_ROUTE_CLEARANCE,
                    node.x + NODE_WIDTH + NODE_ROUTE_CLEARANCE,
                    node.y + NODE_ROUTE_CLEARANCE,
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

        def path_crossing_count(points: list[tuple[float, float]]) -> int:
            """Count proper crossings with already accepted relationships.

            Parallel clearance remains a hard condition in path_is_clear().
            This score covers perpendicular intersections: when any safe
            zero-crossing candidate exists it outranks every crossing route.
            """
            crossings = 0
            for (a, b) in orthogonal_segments(points):
                candidate_vertical = math.isclose(a[0], b[0], abs_tol=1e-6)
                if candidate_vertical:
                    y_low, y_high = sorted((a[1], b[1]))
                    for other_y, other_low, other_high in allocated_horizontals:
                        if (
                            other_low + 1e-6 < a[0] < other_high - 1e-6
                            and y_low + 1e-6 < other_y < y_high - 1e-6
                        ):
                            crossings += 1
                else:
                    x_low, x_high = sorted((a[0], b[0]))
                    for other_x, other_low, other_high in allocated_verticals:
                        if (
                            x_low + 1e-6 < other_x < x_high - 1e-6
                            and other_low + 1e-6 < a[1] < other_high - 1e-6
                        ):
                            crossings += 1
            return crossings

        def path_length(points: list[tuple[float, float]]) -> float:
            return sum(
                abs(second[0] - first[0]) + abs(second[1] - first[1])
                for first, second in orthogonal_segments(points)
            )

        def path_x_reversals(points: list[tuple[float, float]]) -> int:
            """Count changes against the overall source-to-target X direction."""
            if len(points) < 2:
                return 0
            overall = (points[-1][0] > points[0][0]) - (
                points[-1][0] < points[0][0]
            )
            if overall == 0:
                return 0
            return sum(
                1
                for first, second in orthogonal_segments(points)
                if not math.isclose(first[0], second[0], abs_tol=1e-6)
                and ((second[0] > first[0]) - (second[0] < first[0])) != overall
            )

        def path_y_reversals(points: list[tuple[float, float]]) -> int:
            """Count vertical travel away from the target or beyond it."""
            if len(points) < 2:
                return 0
            overall = (points[-1][1] > points[0][1]) - (
                points[-1][1] < points[0][1]
            )
            vertical_directions = [
                (second[1] > first[1]) - (second[1] < first[1])
                for first, second in orthogonal_segments(points)
                if math.isclose(first[0], second[0], abs_tol=1e-6)
                and not math.isclose(first[1], second[1], abs_tol=1e-6)
            ]
            if overall == 0:
                return len(vertical_directions)
            return sum(direction != overall for direction in vertical_directions)

        def first_vertical_x(
            points: list[tuple[float, float]],
        ) -> float | None:
            for first, second in orthogonal_segments(points):
                if math.isclose(first[0], second[0], abs_tol=1e-6):
                    return first[0]
            return None

        def source_fan_is_locked(link: Link) -> bool:
            source_side, _ = self.endpoint_sides(link)
            return len(source_groups.get((link.pred_uid, source_side), [])) >= 2

        def preserves_locked_source_bend(
            link: Link,
            candidate: list[tuple[float, float]],
            current: list[tuple[float, float]],
        ) -> bool:
            if not source_fan_is_locked(link):
                return True
            candidate_x = first_vertical_x(candidate)
            current_x = first_vertical_x(current)
            return (
                candidate_x is not None
                and current_x is not None
                and math.isclose(candidate_x, current_x, abs_tol=1e-6)
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
        routed_links_in_fan_order = sorted(
            routed_links,
            key=lambda item: (
                round(self.layout.nodes[item.pred_uid].x, 6),
                -self.layout.nodes[item.pred_uid].y,
                self.layout.nodes[item.pred_uid].task.task_id,
                fan_turn_rank.get(item.index, 0),
                item.index,
            ),
        )
        for link in routed_links_in_fan_order:
            geometry = base[link.index]
            start = (float(geometry["start"][0]), float(geometry["start"][1]))
            arrow_base = (float(geometry["base"][0]), float(geometry["base"][1]))
            source_shift = 1 if geometry["source_side"] == "R" else -1
            target_shift = -int(geometry["arrow_direction"])
            fan_key = (
                link.pred_uid,
                str(geometry["source_side"]),
            )
            locked_source_fan = len(
                source_groups.get(fan_key, [])
            ) >= 2
            row_span = abs(
                self.layout.nodes[link.pred_uid].task.row
                - self.layout.nodes[link.succ_uid].task.row
            )

            def source_steps(limit: int) -> range:
                return range(1) if locked_source_fan else range(limit)

            selected_points: list[tuple[float, float]] | None = None
            route_mode = ""
            crossing_fallback: tuple[
                tuple[int, int, int, float], list[tuple[float, float]], str
            ] | None = None

            # Enumerate both source- and target-side trunks before selecting a
            # route.  The previous first-valid policy could keep a short route
            # with a crossing even though an open zero-crossing corridor was
            # available a little farther away.
            dogleg_candidates: list[
                tuple[tuple[int, int, float, int, int], list[tuple[float, float]], str]
            ] = []
            source_dogleg_limit = 80 if row_span >= 3 else 16
            for step in source_steps(source_dogleg_limit):
                trunk_x = float(geometry["source_bend_x"]) + source_shift * step * MIN_VERTICAL_CHANNEL_SPACING
                candidate = simplify_points(
                    [start, (trunk_x, start[1]), (trunk_x, arrow_base[1]), arrow_base]
                )
                if path_is_clear(link, candidate):
                    dogleg_candidates.append(
                        (
                            (
                                path_y_reversals(candidate),
                                path_crossing_count(candidate),
                                path_x_reversals(candidate),
                                path_length(candidate),
                                len(candidate),
                                step,
                            ),
                            candidate,
                            "source_dogleg",
                        )
                    )

            source_candidate_count = len(dogleg_candidates)
            target_dogleg_limit = 80 if row_span >= 3 else 16
            for step in (
                range(target_dogleg_limit)
                if not locked_source_fan or source_candidate_count == 0
                else ()
            ):
                trunk_x = float(geometry["target_bend_x"]) + target_shift * step * MIN_VERTICAL_CHANNEL_SPACING
                candidate = simplify_points(
                    [start, (trunk_x, start[1]), (trunk_x, arrow_base[1]), arrow_base]
                )
                if path_is_clear(link, candidate):
                    dogleg_candidates.append(
                        (
                            (
                                path_y_reversals(candidate),
                                path_crossing_count(candidate),
                                path_x_reversals(candidate),
                                path_length(candidate),
                                len(candidate),
                                step,
                            ),
                            candidate,
                            "target_dogleg",
                        )
                    )
            if dogleg_candidates:
                score, candidate_points, candidate_mode = min(dogleg_candidates, key=lambda item: item[0])
                if score[0] == 0 and score[1] == 0 and score[2] == 0:
                    selected_points = candidate_points
                    route_mode = candidate_mode
                    if route_mode == "source_dogleg":
                        source_dogleg_links += 1
                    else:
                        target_dogleg_links += 1
                else:
                    crossing_fallback = (
                        (score[0], score[1], score[2], score[3]),
                        candidate_points,
                        candidate_mode,
                    )

            # Retain the proven upper/lower channel only where a single trunk
            # would cross a node or violate the channel spacing.
            if selected_points is None:
                track_y = float(geometry["track_y"])
                source_x0 = float(geometry["source_bend_x"])
                target_x0 = float(geometry["target_bend_x"])
                detour_candidates: list[
                    tuple[tuple[int, int, float, int, int, int], list[tuple[float, float]]]
                ] = []
                for source_step in source_steps(24):
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
                            detour_candidates.append(
                                (
                                    (
                                        path_y_reversals(candidate),
                                        path_crossing_count(candidate),
                                        path_x_reversals(candidate),
                                        path_length(candidate),
                                        len(candidate),
                                        source_step + target_step,
                                        abs(source_step - target_step),
                                    ),
                                    candidate,
                                )
                            )
                if detour_candidates:
                    score, candidate_points = min(detour_candidates, key=lambda item: item[0])
                    if score[0] == 0 and score[1] == 0 and score[2] == 0:
                        selected_points = candidate_points
                    else:
                        fallback = (
                            (score[0], score[1], score[2], score[3]),
                            candidate_points,
                            "detour",
                        )
                        if crossing_fallback is None or fallback[0] < crossing_fallback[0]:
                            crossing_fallback = fallback

                # Before using the drawing-wide outer frame, scan every open
                # horizontal band between activity rows.  This catches the
                # common case where the preassigned track is busy but a short,
                # safe H-V-H-V-H corridor exists one row above or below.
                if selected_points is None:
                    row_tops = sorted(
                        {node.y for node in self.layout.nodes.values()},
                        reverse=True,
                    )
                    local_levels: set[float] = {track_y}
                    for upper_top, lower_top in zip(row_tops, row_tops[1:]):
                        upper_bottom_clear = (
                            upper_top - NODE_HEIGHT - NODE_ROUTE_CLEARANCE
                        )
                        lower_top_clear = lower_top + NODE_ROUTE_CLEARANCE
                        if lower_top_clear < upper_bottom_clear - 1e-6:
                            local_levels.add(
                                (upper_bottom_clear + lower_top_clear) / 2.0
                            )
                    midpoint_y = (start[1] + arrow_base[1]) / 2.0
                    level_candidates = sorted(
                        local_levels,
                        key=lambda level: (
                            0
                            if min(start[1], arrow_base[1]) <= level <= max(start[1], arrow_base[1])
                            else 1,
                            abs(level - midpoint_y),
                        ),
                    )[:16]
                    local_candidates: list[
                        tuple[
                            tuple[int, int, float, int, int],
                            list[tuple[float, float]],
                        ]
                    ] = []
                    for local_y in level_candidates:
                        for source_step in source_steps(12):
                            source_x = (
                                source_x0
                                + source_shift
                                * source_step
                                * MIN_VERTICAL_CHANNEL_SPACING
                            )
                            for target_step in range(12):
                                target_x = (
                                    target_x0
                                    + target_shift
                                    * target_step
                                    * MIN_VERTICAL_CHANNEL_SPACING
                                )
                                candidate = simplify_points(
                                    [
                                        start,
                                        (source_x, start[1]),
                                        (source_x, local_y),
                                        (target_x, local_y),
                                        (target_x, arrow_base[1]),
                                        arrow_base,
                                    ]
                                )
                                if not path_is_clear(link, candidate):
                                    continue
                                local_candidates.append(
                                    (
                                        (
                                            path_y_reversals(candidate),
                                            path_crossing_count(candidate),
                                            path_x_reversals(candidate),
                                            path_length(candidate),
                                            source_step + target_step,
                                            len(candidate),
                                        ),
                                        candidate,
                                    )
                                )
                    if local_candidates:
                        score, candidate_points = min(
                            local_candidates, key=lambda item: item[0]
                        )
                        if score[0] == 0 and score[1] == 0 and score[2] == 0:
                            selected_points = candidate_points
                        else:
                            fallback = (
                                (score[0], score[1], score[2], score[3]),
                                candidate_points,
                                "detour",
                            )
                            if (
                                crossing_fallback is None
                                or fallback[0] < crossing_fallback[0]
                            ):
                                crossing_fallback = fallback
                if selected_points is None:
                    # Very high-degree endpoints can have fixed port levels
                    # closer than the preferred spacing.  Keep node avoidance
                    # mandatory and relax only parallel clearance in that case.
                    for source_step in source_steps(28):
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
                                y_reversals = path_y_reversals(candidate)
                                crossings = path_crossing_count(candidate)
                                reversals = path_x_reversals(candidate)
                                if y_reversals == 0 and crossings == 0 and reversals == 0:
                                    selected_points = candidate
                                    relaxed_detour_links += 1
                                    break
                                fallback = (
                                    (y_reversals, crossings, reversals, path_length(candidate)),
                                    candidate,
                                    "detour",
                                )
                                if crossing_fallback is None or fallback[0] < crossing_fallback[0]:
                                    crossing_fallback = fallback
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
                                y_reversals = path_y_reversals(candidate)
                                crossings = path_crossing_count(candidate)
                                reversals = path_x_reversals(candidate)
                                if y_reversals == 0 and crossings == 0 and reversals == 0:
                                    selected_points = candidate
                                    emergency_detour_links += 1
                                    break
                                fallback = (
                                    (y_reversals, crossings, reversals, path_length(candidate)),
                                    candidate,
                                    "detour",
                                )
                                if crossing_fallback is None or fallback[0] < crossing_fallback[0]:
                                    crossing_fallback = fallback
                        if selected_points is not None:
                            break
                if selected_points is None and crossing_fallback is not None:
                    _, selected_points, route_mode = crossing_fallback

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
                    source_port_x = start[0] + source_shift * MIN_ENDPOINT_STUB_LENGTH
                    target_port_x = arrow_base[0] + target_shift * MIN_ENDPOINT_STUB_LENGTH
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
        locked_fan_relaxation_links = 0
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
            # The source fan-out is one ordered staircase.  Its first two
            # segments may never cross, touch, or exchange order with an
            # already accepted branch from the same source.
            candidate_segments = orthogonal_segments(points)[:2]
            return any(
                pred_uid == link.pred_uid
                and (
                    physically_touches(candidate, accepted)
                    or properly_crosses(candidate, accepted)
                )
                for candidate in candidate_segments
                for pred_uid, accepted in accepted_link_segments
            )

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
            if (
                (not bool(geometry.get("direct")) and not respects_endpoint_stubs(simplified))
                or not preserves_locked_source_bend(link, simplified, original)
                or path_y_reversals(simplified) > path_y_reversals(original)
            ):
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
                fan_turn_rank.get(index, 0),
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

                def keeps_source_fan(candidate: list[tuple[float, float]]) -> bool:
                    return preserves_locked_source_bend(link, candidate, points)

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
                    if respects_endpoint_stubs(candidate) and not path_hits_node(link, candidate) and not overlaps_accepted(candidate) and not crosses_same_source(link, candidate) and keeps_source_fan(candidate) and path_y_reversals(candidate) == 0:
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
                                    and keeps_source_fan(candidate)
                                    and path_y_reversals(candidate) == 0
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
                    if respects_endpoint_stubs(candidate) and not path_hits_node(link, candidate) and not overlaps_accepted(candidate) and not crosses_same_source(link, candidate) and keeps_source_fan(candidate) and path_y_reversals(candidate) == 0:
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
                    if respects_endpoint_stubs(candidate) and not path_hits_node(link, candidate) and not overlaps_accepted(candidate) and not crosses_same_source(link, candidate) and keeps_source_fan(candidate) and path_y_reversals(candidate) == 0:
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
                        if respects_endpoint_stubs(candidate) and not path_hits_node(link, candidate) and not touches_accepted(candidate) and not crosses_same_source(link, candidate) and keeps_source_fan(candidate) and path_y_reversals(candidate) == 0:
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
                                if respects_endpoint_stubs(candidate) and not path_hits_node(link, candidate) and not touches_accepted(candidate) and not crosses_same_source(link, candidate) and keeps_source_fan(candidate) and path_y_reversals(candidate) == 0:
                                    chosen = candidate
                                    break
                            if chosen is not None:
                                break
                        if chosen is not None:
                            break
                if chosen is None:
                    # Do not abort an otherwise valid drawing merely because
                    # the preferred same-source visual ordering has no fully
                    # spaced lane in an exceptionally dense region.  Keep the
                    # assigned source bend first and relax only that visual
                    # ordering test; node clearance, endpoint stubs and
                    # physical separation remain hard requirements.
                    locked_x = first_vertical_x(points)
                    side = -1 if arrow_base[1] <= start[1] else 1
                    local_edge = (
                        min(start[1], arrow_base[1]) - NODE_HEIGHT
                        if side < 0
                        else max(start[1], arrow_base[1])
                    )
                    if locked_x is not None:
                        for attempt in range(1, 801):
                            outside_y = local_edge + side * (18.0 + attempt * 5.17)
                            target_x = arrow_base[0] + target_shift * (
                                12.0 + attempt * 5.31
                            )
                            candidate = simplify_points(
                                [
                                    start,
                                    (locked_x, start[1]),
                                    (locked_x, outside_y),
                                    (target_x, outside_y),
                                    (target_x, arrow_base[1]),
                                    arrow_base,
                                ]
                            )
                            if (
                                respects_endpoint_stubs(candidate)
                                and not path_hits_node(link, candidate)
                                and not touches_accepted(candidate)
                                and keeps_source_fan(candidate)
                                and path_y_reversals(candidate) == 0
                                and not crosses_same_source(link, candidate)
                            ):
                                chosen = candidate
                                chosen_mode = "locked_fan_relaxed_order"
                                locked_fan_relaxation_links += 1
                                break
                if chosen is None and not source_fan_is_locked(link):
                    # Absolute safety fallback for pathological congestion:
                    # search independent source/target trunks.  This may
                    # relax the fan bend order, but never the geometric hard
                    # rules, and is preferable to failing the whole export.
                    side = -1 if arrow_base[1] <= start[1] else 1
                    local_edge = (
                        min(start[1], arrow_base[1]) - NODE_HEIGHT
                        if side < 0
                        else max(start[1], arrow_base[1])
                    )
                    for attempt in range(1, 2001):
                        outside_y = local_edge + side * (21.0 + attempt * 4.73)
                        source_x = start[0] + source_shift * (31.0 + attempt * 5.11)
                        target_x = arrow_base[0] + target_shift * (31.0 + attempt * 5.29)
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
                        if (
                            respects_endpoint_stubs(candidate)
                            and not path_hits_node(link, candidate)
                            and not touches_accepted(candidate)
                        ):
                            chosen = candidate
                            chosen_mode = "hard_clearance_fallback"
                            locked_fan_relaxation_links += 1
                            break
                if chosen is not None:
                    shortened = remove_unnecessary_detours(link, chosen)
                    if respects_endpoint_stubs(shortened) and not path_hits_node(link, shortened) and not overlaps_accepted(shortened) and keeps_source_fan(shortened) and path_y_reversals(shortened) <= path_y_reversals(chosen):
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
                    or path_x_reversals(candidate) > path_x_reversals(current)
                    or path_y_reversals(candidate) > path_y_reversals(current)
                    or not preserves_locked_source_bend(link, candidate, current)
                ):
                    continue
                candidate_segments = orthogonal_segments(candidate)
                if any(
                    physically_touches(segment, other)
                    or properly_crosses(segment, other)
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
                        or path_x_reversals(candidate) > path_x_reversals(current)
                        or path_y_reversals(candidate) > path_y_reversals(current)
                        or not preserves_locked_source_bend(link, candidate, current)
                    ):
                        continue
                    candidate_segments = orthogonal_segments(candidate)
                    if any(
                        physically_touches(segment, other)
                        or properly_crosses(segment, other)
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
                    original_points = list(geometry["points"])
                    points = list(original_points)
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
                        or path_x_reversals(points)
                        > path_x_reversals(original_points)
                        or path_y_reversals(points)
                        > path_y_reversals(original_points)
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
                    or properly_crosses(segment, other)
                    for _, segment in candidate_all_segments
                    for other in fixed_all_segments
                ):
                    continue
                if any(
                    first_index != second_index
                    and (
                        physically_touches(first, second)
                        or properly_crosses(first, second)
                    )
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
            "locked_fan_relaxation_links": locked_fan_relaxation_links,
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

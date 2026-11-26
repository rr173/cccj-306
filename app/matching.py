"""Injective matching of whole records to distinct credential requirements."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Hashable, Optional, TypeVar

Requirement = TypeVar("Requirement", bound=Hashable)
Record = TypeVar("Record", bound=Hashable)


def find_assignment(
    requirements: Sequence[Requirement],
    eligible: Mapping[Requirement, Sequence[Record]],
) -> Optional[dict[Requirement, Record]]:
    """Return a requirement -> record bijection/partial injection, or ``None``.

    Every requirement receives exactly one whole record. Different requirements
    receive different records; a record eligible for several requirements is
    still assigned to at most one. The caller decides tie-breaking, so no
    particular plan is promised when several exist.
    """
    req_list = list(requirements)
    eligible_sets = {req: list(dict.fromkeys(eligible.get(req, ()))) for req in req_list}

    # Static MRV ordering is a small, deterministic performance improvement.
    # Correctness is still provided by backtracking.
    order = sorted(req_list, key=lambda req: (len(eligible_sets[req]), str(req)))
    assignment: dict[Requirement, Record] = {}
    used_records: set[Record] = set()

    def backtrack(index: int) -> bool:
        if index == len(order):
            return True

        requirement = order[index]
        for record in eligible_sets[requirement]:
            if record in used_records:
                continue
            used_records.add(record)
            assignment[requirement] = record
            if backtrack(index + 1):
                return True
            used_records.remove(record)
            assignment.pop(requirement, None)
        return False

    if not backtrack(0):
        return None

    # Return in the caller's requirement order.
    return {req: assignment[req] for req in req_list}


def find_allocation(
    requirements: Sequence[dict],
    points_by_record: Mapping[Record, int],
    options_by_record: Mapping[Record, Sequence[Requirement]],
) -> Optional[dict[Requirement, list[Record]]]:
    """Allocate whole records to requirement items.

    A record appears in at most one requirement list. For ``whole_record``
    requirements, the caller only supplies options when that record itself
    reaches the minimum. For ``sum_points`` requirements, options are based on
    category acceptance and assigned points are summed.
    """
    req_codes = [req["requirement_code"] for req in requirements]
    req_index = {code: i for i, code in enumerate(req_codes)}
    mins = tuple(int(req["min_points"]) for req in requirements)
    is_whole = tuple(req.get("points_mode", "whole_record") == "whole_record" for req in requirements)

    records = list(points_by_record.keys())
    # Most constrained records first; deterministic tie-breaking.
    records.sort(key=lambda code: (len(options_by_record.get(code, ())), -points_by_record[code], str(code)))

    assignment: dict[Requirement, list[Record]] = {code: [] for code in req_codes}
    failed: set[tuple[int, tuple[int, ...], tuple[int, ...]]] = set()

    def is_complete(earned: tuple[int, ...], counts: tuple[int, ...]) -> bool:
        for i in range(len(req_codes)):
            if is_whole[i]:
                if counts[i] != 1:
                    return False
            elif earned[i] < mins[i]:
                return False
        return True

    def prune_possible(
        start: int, earned: tuple[int, ...], counts: tuple[int, ...]
    ) -> bool:
        future = records[start:]
        for i, req_code in enumerate(req_codes):
            if is_whole[i]:
                if counts[i] == 0 and not any(
                    req_code in options_by_record.get(record_code, ())
                    for record_code in future
                ):
                    return False
            elif earned[i] < mins[i]:
                available = sum(
                    points_by_record[record_code]
                    for record_code in future
                    if req_code in options_by_record.get(record_code, ())
                )
                if earned[i] + available < mins[i]:
                    return False
        return True

    def dfs(index: int, earned: tuple[int, ...], counts: tuple[int, ...]) -> bool:
        state = (index, earned, counts)
        if state in failed:
            return False
        if is_complete(earned, counts):
            return True
        if index == len(records):
            failed.add(state)
            return False
        if not prune_possible(index, earned, counts):
            failed.add(state)
            return False

        record_code = records[index]
        options = list(options_by_record.get(record_code, ()))

        for req_code in options:
            req_i = req_index[req_code]
            if is_whole[req_i] and counts[req_i] >= 1:
                continue
            if not is_whole[req_i] and earned[req_i] >= mins[req_i]:
                continue

            assignment[req_code].append(record_code)
            next_earned = list(earned)
            next_earned[req_i] += points_by_record[record_code]
            next_counts = list(counts)
            next_counts[req_i] += 1

            if dfs(index + 1, tuple(next_earned), tuple(next_counts)):
                return True

            assignment[req_code].pop()

        # The student may list extra records that do not have to be consumed.
        if dfs(index + 1, earned, counts):
            return True

        failed.add(state)
        return False

    zero_earned = tuple(0 for _ in req_codes)
    zero_counts = tuple(0 for _ in req_codes)
    if not dfs(0, zero_earned, zero_counts):
        return None
    return {code: list(assignment[code]) for code in req_codes}

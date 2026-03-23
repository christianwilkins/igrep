from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b
import re
from typing import Iterable, Sequence

import sre_parse
from sre_constants import ANY, ASSERT, ASSERT_NOT, AT, BRANCH, CATEGORY, IN, LITERAL, MAX_REPEAT, MIN_REPEAT, NEGATE, NOT_LITERAL, SUBPATTERN


def iter_trigrams(text: str) -> Iterable[str]:
    for index in range(len(text) - 2):
        yield text[index : index + 3]


def unique_trigram_hashes(text: str) -> list[int]:
    if len(text) < 3:
        return []
    unique_trigrams = {text[index : index + 3] for index in range(len(text) - 2)}
    return sorted(hash_token(trigram) for trigram in unique_trigrams)


def hash_token(token: str) -> int:
    digest = blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little", signed=False)


@dataclass(frozen=True)
class LiteralInfo:
    runs: tuple[str, ...]
    concat: str | None


def extract_required_literals(pattern: str, ignore_case: bool = False) -> list[str]:
    flags = re.IGNORECASE if ignore_case else 0
    parsed = sre_parse.parse(pattern, flags)
    info = _sequence_info(list(parsed.data))
    return _normalize_literals([*info.runs, info.concat], ignore_case=ignore_case)


def extract_literal_groups(pattern: str, ignore_case: bool = False) -> list[list[str]]:
    """
    Return OR groups of required literals.

    Each inner list is a conjunction (all literals are required for that branch).
    The outer list is a disjunction (any branch may match).

    For patterns without a clear top-level alternation, this returns one group.
    """
    flags = re.IGNORECASE if ignore_case else 0
    parsed = sre_parse.parse(pattern, flags)
    tokens = list(parsed.data)

    if len(tokens) == 1 and tokens[0][0] == BRANCH:
        _, branches = tokens[0][1]
        groups: list[list[str]] = []
        for branch in branches:
            info = _sequence_info(list(branch))
            group = _normalize_literals([*info.runs, info.concat], ignore_case=ignore_case)
            if group:
                groups.append(group)
        return groups

    return [extract_required_literals(pattern, ignore_case=ignore_case)]


def _normalize_literals(values: Sequence[str | None], ignore_case: bool) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        literal = value.casefold() if ignore_case else value
        if literal in seen:
            continue
        seen.add(literal)
        normalized.append(literal)
    return normalized


def _sequence_info(tokens: Sequence[tuple[object, object]]) -> LiteralInfo:
    runs: list[str] = []
    current: list[str] = []

    for op, arg in tokens:
        if op == LITERAL:
            current.append(chr(arg))
            continue
        if op == SUBPATTERN:
            child = _sequence_info(list(arg[-1].data))
            if child.concat is not None:
                current.append(child.concat)
                continue
            if current:
                runs.append("".join(current))
                current.clear()
            runs.extend(child.runs)
            continue
        if op in {MAX_REPEAT, MIN_REPEAT}:
            min_repeat, max_repeat, subpattern = arg
            child = _sequence_info(list(subpattern.data))
            if min_repeat == max_repeat == 1 and child.concat is not None:
                current.append(child.concat)
                continue
            if current:
                runs.append("".join(current))
                current.clear()
            if min_repeat >= 1:
                if child.concat:
                    runs.append(child.concat)
                else:
                    runs.extend(child.runs)
            continue
        if op == BRANCH:
            if current:
                runs.append("".join(current))
                current.clear()
            _, branches = arg
            branch_infos = [_sequence_info(list(branch)) for branch in branches]
            common_runs = set(branch_infos[0].runs)
            for info in branch_infos[1:]:
                common_runs &= set(info.runs)
            if branch_infos and all(info.concat == branch_infos[0].concat for info in branch_infos):
                concat = branch_infos[0].concat
                if concat is not None:
                    runs.append(concat)
            runs.extend(sorted(common_runs))
            continue
        if op in {IN, ANY, CATEGORY, ASSERT, ASSERT_NOT, AT, NEGATE, NOT_LITERAL}:
            if current:
                runs.append("".join(current))
                current.clear()
            continue
        if current:
            runs.append("".join(current))
            current.clear()

    if current:
        concat = "".join(current)
        runs.append(concat)
        return LiteralInfo(tuple(runs[:-1]), concat)
    return LiteralInfo(tuple(runs), None)

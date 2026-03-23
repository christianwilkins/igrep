from __future__ import annotations

from dataclasses import dataclass
import mmap
import os
import struct
from typing import BinaryIO, Iterable, Sequence


LOOKUP_STRUCT = struct.Struct("<QQII")
UINT64_STRUCT = struct.Struct("<Q")


def encode_varints(values: Iterable[int]) -> bytes:
    output = bytearray()
    for value in values:
        current = value
        while current >= 0x80:
            output.append((current & 0x7F) | 0x80)
            current >>= 7
        output.append(current)
    return bytes(output)


def decode_varints(data: bytes) -> list[int]:
    values: list[int] = []
    value = 0
    shift = 0
    for byte in data:
        value |= (byte & 0x7F) << shift
        if byte & 0x80:
            shift += 7
            continue
        values.append(value)
        value = 0
        shift = 0
    if shift:
        raise ValueError("truncated varint stream")
    return values


def encode_postings(doc_ids: Sequence[int]) -> bytes:
    previous = 0
    deltas = []
    for doc_id in doc_ids:
        deltas.append(doc_id - previous)
        previous = doc_id
    return encode_varints(deltas)


def decode_postings(data: bytes) -> list[int]:
    previous = 0
    result: list[int] = []
    for delta in decode_varints(data):
        previous += delta
        result.append(previous)
    return result


def intersect_postings(left: Sequence[int], right: Sequence[int]) -> list[int]:
    output: list[int] = []
    i = 0
    j = 0
    while i < len(left) and j < len(right):
        left_value = left[i]
        right_value = right[j]
        if left_value == right_value:
            output.append(left_value)
            i += 1
            j += 1
        elif left_value < right_value:
            i += 1
        else:
            j += 1
    return output


def read_uint64_slice(handle: BinaryIO, offset: int, count: int) -> list[int]:
    handle.seek(offset)
    data = handle.read(count * UINT64_STRUCT.size)
    if len(data) != count * UINT64_STRUCT.size:
        raise ValueError("unexpected end of doc_terms file")
    return [value[0] for value in struct.iter_unpack("<Q", data)]


@dataclass(frozen=True)
class LookupEntry:
    token_hash: int
    offset: int
    length: int
    docfreq: int


class LookupTable:
    def __init__(self, path: str) -> None:
        self._file = open(path, "rb")
        size = os.path.getsize(path)
        self._mmap = mmap.mmap(self._file.fileno(), length=0, access=mmap.ACCESS_READ)
        self.count = size // LOOKUP_STRUCT.size

    def close(self) -> None:
        self._mmap.close()
        self._file.close()

    def find(self, token_hash: int) -> LookupEntry | None:
        low = 0
        high = self.count - 1
        while low <= high:
            mid = (low + high) // 2
            entry = self._read(mid)
            if entry.token_hash == token_hash:
                return entry
            if entry.token_hash < token_hash:
                low = mid + 1
            else:
                high = mid - 1
        return None

    def _read(self, index: int) -> LookupEntry:
        start = index * LOOKUP_STRUCT.size
        token_hash, offset, length, docfreq = LOOKUP_STRUCT.unpack_from(self._mmap, start)
        return LookupEntry(token_hash, offset, length, docfreq)


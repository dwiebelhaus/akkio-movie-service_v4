"""Stream an uploaded CSV straight to storage with constant memory (DESIGN.md §8).

Accepts `multipart/form-data` (field `file`) or a raw `text/csv` body. The size limit is
enforced while streaming, and the header row is validated as soon as it arrives, so bad or
oversized uploads are rejected early and the partial file is deleted.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Request
from python_multipart.multipart import MultipartParser, parse_options_header

from app.errors import ApiError
from app.services.csv_import import CsvFileError, Header, parse_header_line
from app.services.storage import Storage

FILE_FIELD = "file"
MAX_HEADER_BYTES = 64 * 1024
# After a 413, read and discard bodies up to this much over the limit so the client reliably
# receives the response instead of a reset connection; anything larger is cut off.
MAX_DRAIN_BYTES = 8 * 1024 * 1024
_WRITE_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Upload:
    key: str
    size: int
    header: Header


def _too_large(max_bytes: int) -> ApiError:
    return ApiError(
        413, "payload_too_large", f"Upload exceeds the {max_bytes} byte limit", {"max_bytes": max_bytes}
    )


async def _drain(chunks: AsyncIterator[bytes], limit: int) -> None:
    drained = 0
    with contextlib.suppress(Exception):
        async for chunk in chunks:
            drained += len(chunk)
            if drained > limit:
                break


async def _multipart_file_chunks(request: Request, boundary: bytes) -> AsyncIterator[bytes]:
    """Yield the bytes of the `file` part from a streaming multipart body."""
    pending: list[bytes] = []
    state = {"field": b"", "value": b"", "headers": {}, "in_file": False, "file_seen": False}

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state["field"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state["value"] += data[start:end]

    def on_header_end() -> None:
        state["headers"][state["field"].lower()] = state["value"]
        state["field"] = state["value"] = b""

    def on_headers_finished() -> None:
        _, opts = parse_options_header(state["headers"].get(b"content-disposition", b""))
        is_file = opts.get(b"name") == FILE_FIELD.encode() and not state["file_seen"]
        state["in_file"] = is_file
        state["file_seen"] |= is_file
        state["headers"] = {}

    def on_part_data(data: bytes, start: int, end: int) -> None:
        if state["in_file"]:
            pending.append(bytes(data[start:end]))

    def on_part_end() -> None:
        state["in_file"] = False

    parser = MultipartParser(
        boundary,
        {
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
        },
    )
    try:
        async for chunk in request.stream():
            parser.write(chunk)
            if pending:
                yield b"".join(pending)
                pending.clear()
        parser.finalize()
    except ApiError:
        raise
    except Exception as exc:  # malformed multipart
        if exc.__class__.__name__ == "ClientDisconnect":
            raise
        raise ApiError(422, "invalid_multipart", "Malformed multipart body") from None
    if pending:
        yield b"".join(pending)
    if not state["file_seen"]:
        raise ApiError(422, "missing_file", f"Multipart body must include a '{FILE_FIELD}' field")


async def receive_csv_upload(request: Request, storage: Storage, max_bytes: int) -> Upload:
    content_type, opts = parse_options_header(request.headers.get("content-type", ""))
    if content_type == b"multipart/form-data":
        boundary = opts.get(b"boundary")
        if not boundary:
            raise ApiError(422, "invalid_multipart", "Multipart body has no boundary")
        chunks = _multipart_file_chunks(request, boundary)
    elif content_type in (b"text/csv", b"application/csv", b"application/octet-stream"):
        chunks = request.stream()
    else:
        raise ApiError(
            415, "unsupported_media_type", "Send multipart/form-data (field 'file') or text/csv"
        )

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes + MAX_HEADER_BYTES:
        await _drain(request.stream(), max_bytes + MAX_DRAIN_BYTES)
        raise _too_large(max_bytes)

    key = storage.new_key("uploads", ".csv")
    file = await asyncio.to_thread(storage.open_write, key)
    size = 0
    header: Header | None = None
    head = b""
    buffer: list[bytes] = []
    buffered = 0
    try:
        async for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                await _drain(chunks, MAX_DRAIN_BYTES)
                raise _too_large(max_bytes)
            if header is None:
                head += chunk
                newline = head.find(b"\n")
                if newline >= 0:
                    header = parse_header_line(head[:newline])
                    head = b""
                elif len(head) > MAX_HEADER_BYTES:
                    raise CsvFileError("Header row is too long; is this a CSV file?")
            buffer.append(chunk)
            buffered += len(chunk)
            if buffered >= _WRITE_CHUNK:
                await asyncio.to_thread(file.write, b"".join(buffer))
                buffer.clear()
                buffered = 0
        if header is None:
            if not head.strip():
                raise CsvFileError("File is empty or has no header row")
            header = parse_header_line(head)  # single-line file without a trailing newline
        if buffer:
            await asyncio.to_thread(file.write, b"".join(buffer))
        await asyncio.to_thread(file.close)
    except BaseException as exc:
        await asyncio.to_thread(file.close)
        await asyncio.to_thread(storage.delete, key)
        if isinstance(exc, CsvFileError):
            raise ApiError(422, "invalid_csv", str(exc)) from None
        raise
    return Upload(key=key, size=size, header=header)

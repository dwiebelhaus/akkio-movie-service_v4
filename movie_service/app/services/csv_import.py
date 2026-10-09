"""CSV header validation and batched, streaming row parsing for imports (DESIGN.md §8)."""

import csv
import io
from dataclasses import dataclass, field
from typing import BinaryIO

from app.domain import Movie

REQUIRED_COLUMNS = ("movie_name", "year", "genres", "rating")
MAX_REJECTED_SAMPLES = 100


class CsvFileError(ValueError):
    """A file-level problem (bad header, encoding, broken CSV) that fails the whole import."""


@dataclass(frozen=True)
class Header:
    indexes: dict[str, int]  # required column -> position
    width: int
    extra_columns: list[str]

    @property
    def warnings(self) -> list[str]:
        return [f"Ignored extra column: {name}" for name in self.extra_columns]


def parse_header(fields: list[str]) -> Header:
    """Validate a header row: required columns in any order, extras ignored."""
    names = [f.strip().lower() for f in fields]
    missing = [c for c in REQUIRED_COLUMNS if c not in names]
    if missing:
        raise CsvFileError(f"Missing required column(s): {', '.join(missing)}")
    repeated = sorted({c for c in REQUIRED_COLUMNS if names.count(c) > 1})
    if repeated:
        raise CsvFileError(f"Duplicate column(s): {', '.join(repeated)}")
    return Header(
        indexes={c: names.index(c) for c in REQUIRED_COLUMNS},
        width=len(names),
        extra_columns=[f.strip() for f in fields if f.strip().lower() not in REQUIRED_COLUMNS],
    )


def parse_header_line(raw: bytes) -> Header:
    """Parse the first line of an uploaded file (bytes, possibly with a UTF-8 BOM)."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvFileError("File is not valid UTF-8 text") from None
    if "\x00" in text:
        raise CsvFileError("File is not a CSV (binary content)")
    try:
        fields = next(csv.reader([text.rstrip("\r\n")]), [])
    except csv.Error as exc:
        raise CsvFileError(f"Invalid CSV header: {exc}") from None
    if not any(f.strip() for f in fields):
        raise CsvFileError("File is empty or has no header row")
    return parse_header(fields)


@dataclass
class Batch:
    movies: list[tuple[int, Movie]] = field(default_factory=list)  # (line number, movie)
    rejected: int = 0
    bytes_read: int = 0


@dataclass
class RejectedRow:
    line: int
    reason: str


class CsvBatchReader:
    """Reads a CSV file in batches with constant memory. Blocking; run it in a thread."""

    def __init__(self, file: BinaryIO, batch_rows: int):
        self._file = file
        self._text = io.TextIOWrapper(file, encoding="utf-8-sig", newline="")
        self._reader = csv.reader(self._text)
        self._batch_rows = batch_rows
        self.rejected_samples: list[RejectedRow] = []
        try:
            first = next(self._reader, None)
        except UnicodeDecodeError:
            # The decoder reads ahead, so this may come from any early part of the file.
            raise CsvFileError("File is not valid UTF-8 text") from None
        except csv.Error as exc:
            raise CsvFileError(f"Invalid CSV header: {exc}") from None
        if first is None:
            raise CsvFileError("File is empty or has no header row")
        self.header = parse_header(first)

    def _reject(self, batch: Batch, line: int, reason: str) -> None:
        batch.rejected += 1
        if len(self.rejected_samples) < MAX_REJECTED_SAMPLES:
            self.rejected_samples.append(RejectedRow(line, reason))

    def read_batch(self) -> Batch | None:
        """Return the next batch, or None at end of file."""
        batch = Batch()
        idx = self.header.indexes
        seen = 0
        try:
            for fields in self._reader:
                if not fields:  # blank line
                    continue
                line = self._reader.line_num
                seen += 1
                if len(fields) != self.header.width:
                    self._reject(batch, line, f"expected {self.header.width} fields, got {len(fields)}")
                else:
                    try:
                        movie = Movie.from_csv_row({c: fields[i] for c, i in idx.items()})
                    except ValueError as exc:
                        self._reject(batch, line, str(exc))
                    else:
                        batch.movies.append((line, movie))
                if seen >= self._batch_rows:
                    break
        except UnicodeDecodeError:
            raise CsvFileError(f"File is not valid UTF-8 near line {self._reader.line_num + 1}") from None
        except csv.Error as exc:
            raise CsvFileError(f"Unreadable CSV near line {self._reader.line_num}: {exc}") from None
        batch.bytes_read = self._file.tell()
        return batch if seen else None

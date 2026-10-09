import io

import pytest

from app.services.csv_import import CsvBatchReader, CsvFileError, parse_header, parse_header_line
from app.services.importer import encode_copy_rows


def reader(text: str | bytes, batch_rows: int = 1000) -> CsvBatchReader:
    data = text.encode() if isinstance(text, str) else text
    return CsvBatchReader(io.BytesIO(data), batch_rows)


def read_all(r: CsvBatchReader):
    batches = []
    while (b := r.read_batch()) is not None:
        batches.append(b)
    return batches


def test_header_any_order_with_extras():
    header = parse_header(["Rating", " genres", "movie_name", "director", "year"])
    assert header.indexes == {"movie_name": 2, "year": 4, "genres": 1, "rating": 0}
    assert header.width == 5
    assert header.warnings == ["Ignored extra column: director"]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (["movie_name", "year"], "Missing required column(s): genres, rating"),
        (["movie_name", "year", "genres", "rating", "year"], "Duplicate column(s): year"),
    ],
)
def test_invalid_headers(fields, message):
    with pytest.raises(CsvFileError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_header(fields)


def test_header_line_handles_bom_and_crlf():
    header = parse_header_line(b"\xef\xbb\xbfmovie_name,year,genres,rating\r")
    assert header.width == 4


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xff\xfe\x00m", "not valid UTF-8"),
        (b"movie_name\x00,year", "binary content"),
        (b"   ", "empty"),
    ],
)
def test_header_line_rejects_non_csv(raw, message):
    with pytest.raises(CsvFileError, match=message):
        parse_header_line(raw)


def test_reader_batches_and_tracks_line_numbers():
    text = "movie_name,year,genres,rating\n" + "".join(f"M{i},2000,Drama,5\n" for i in range(25))
    batches = read_all(reader(text, batch_rows=10))
    assert [len(b.movies) for b in batches] == [10, 10, 5]
    assert batches[0].movies[0][0] == 2  # header is line 1
    assert batches[-1].movies[-1][0] == 26
    assert batches[-1].bytes_read == len(text)


def test_reader_rejects_bad_rows_and_keeps_samples():
    text = 'movie_name,year,genres,rating\nGood,2000,"Drama, Action",5\nBad,2000,Drama\n\nWorse,xx,Drama,1\n'
    r = reader(text)
    (batch,) = read_all(r)
    assert [m.movie_name for _, m in batch.movies] == ["Good"]
    assert batch.rejected == 2
    assert [(s.line, s.reason) for s in r.rejected_samples] == [
        (3, "expected 4 fields, got 3"),
        (5, "invalid year: 'xx'"),
    ]


def test_reader_handles_quoted_newlines():
    text = 'movie_name,year,genres,rating\n"Line\nBreak",2000,Drama,5\nNext,2001,Drama,6\n'
    (batch,) = read_all(reader(text))
    assert [(line, m.movie_name) for line, m in batch.movies] == [(3, "Line Break"), (4, "Next")]


def test_reader_fails_on_invalid_encoding_mid_file():
    data = b"movie_name,year,genres,rating\nOk,2000,Drama,5\n\xff\xfe,2000,Drama,5\n"
    with pytest.raises(CsvFileError, match="not valid UTF-8"):
        read_all(reader(data))


def test_reader_requires_header():
    with pytest.raises(CsvFileError, match="empty"):
        reader("")


def test_copy_encoding_escapes_and_nulls():
    text = 'movie_name,year,genres,rating\n"Tab\\there",,"Drama",\n'
    (batch,) = read_all(reader(text))
    assert encode_copy_rows(batch) == b"2\tTab\\\\there\ttab\\\\there\t\\N\tdrama\tDrama\t\\N\n"

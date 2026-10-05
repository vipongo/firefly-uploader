"""Detect which bank a statement file comes from and parse it."""

from pathlib import Path

from ..models import Statement
from . import revolut, ubs

PARSERS = [ubs, revolut]


class UnknownFormatError(ValueError):
    pass


def decode(data: bytes) -> str:
    """Bank exports are UTF-8 (UBS adds a BOM); fall back to Windows-1252 for older files."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def parse(data: bytes) -> Statement:
    text = decode(data).replace("\r\n", "\n")
    for parser in PARSERS:
        if parser.matches(text):
            return parser.parse(text)
    raise UnknownFormatError("This doesn't look like a UBS or Revolut CSV export")


def parse_file(path: str | Path) -> Statement:
    return parse(Path(path).read_bytes())

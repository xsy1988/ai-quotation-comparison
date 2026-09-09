"""统一中间表示（IR）：文件接入层的输出，版面理解的输入。JSON 可序列化，可落盘为快照。"""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TextBlock:
    text: str
    sheet: str
    row: int
    col: int


@dataclass
class CellValue:
    row: int
    col: int
    value: str | float | int | bool | None


@dataclass
class TableRow:
    sheet: str
    row_number: int
    cells: list[CellValue] = field(default_factory=list)

    def is_empty(self) -> bool:
        return all(c.value is None or c.value == "" for c in self.cells)


@dataclass
class IR:
    source_file: str
    file_hash: str
    file_type: str
    sheets: list[str]
    blocks: list[TextBlock] = field(default_factory=list)
    tables: list[TableRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IR":
        return cls(
            source_file=data["source_file"],
            file_hash=data["file_hash"],
            file_type=data["file_type"],
            sheets=list(data["sheets"]),
            blocks=[TextBlock(**b) for b in data["blocks"]],
            tables=[
                TableRow(sheet=t["sheet"], row_number=t["row_number"], cells=[CellValue(**c) for c in t["cells"]])
                for t in data["tables"]
            ],
        )

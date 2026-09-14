"""Excel 公式求值：只为补齐「缓存值为空」的公式单元格（IR 接入层用）。

背景：`openpyxl` 以 `data_only=True` 读表时，单元格取值来自 Excel 上次保存时写入的**缓存值**；
脚本生成的报价单（从未被 Excel 打开/重算过）里，公式单元格没有缓存值，`cell.value` 是 None。
于是"未税合计 = SUM(各工序)"、"含税单价 = 未税 × 1.13"这类关键金额在 IR 里是空白 —— 版面
理解拿不到金额只能猜（曾把利润单元格当成含税单价），下游勾稽校验必然误报失败。

设计取舍（保证行为可控）：
* **缓存优先**：有缓存值（Excel 算过）的单元格一律不重新计算，其余单据行为与改造前完全一致；
* **只补空**：只有「缓存为空且存在公式」的单元格才会被求值，且按需（惰性）递归解析依赖；
* **严格失败**：不支持的函数/语法、除零、循环引用 → 记为 unresolved，绝不猜测数值；
* 求解结果只作为 IR 单元格值；公式原文另以 `IR.notes` 的 `FORMULA` 行给出（供 LLM 参考，
  不得当作金额出处）。
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable

from app.normalize import display_number

MAX_RANGE_CELLS = 5000
"""单次区间引用展开的单元格上限，防止整列引用（如 A:A）造成病态开销。"""


class FormulaError(Exception):
    """公式无法安全求值（语法不支持、除零、循环引用、引用到文本单元格等）。"""


_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<number>\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)
      | (?P<ref>(?:'[^']*'|[^\s!()+\-*/^&=<>,:]+)?!\$?[A-Za-z]{1,3}\$?\d+
              |\$?[A-Za-z]{1,3}\$?\d+)
      | (?P<name>[A-Za-z_][A-Za-z0-9_.]*)
      | (?P<op><>|<=|>=|[+\-*/^&%=<>(),:])
    )
    """,
    re.VERBOSE,
)

_REF_RE = re.compile(r"^(?:(?P<sheet>'[^']*'|[^\s!()+\-*/^&=<>,:]+)!)?\$?(?P<col>[A-Za-z]{1,3})\$?(?P<row>\d+)$")

_FUNC_ARITY: dict[str, tuple[int, int | None]] = {
    "SUM": (1, None),
    "AVERAGE": (1, None),
    "MIN": (1, None),
    "MAX": (1, None),
    "COUNT": (1, None),
    "COUNTA": (1, None),
    "ABS": (1, 1),
    "INT": (1, 1),
    "ROUND": (2, 2),
    "ROUNDUP": (2, 2),
    "ROUNDDOWN": (2, 2),
    "IF": (2, 3),
    "AND": (1, None),
    "OR": (1, None),
    "NOT": (1, 1),
}


def _col_to_index(letters: str) -> int:
    """列字母 → 1 基列号（A → 1，AA → 27）。"""
    value = 0
    for char in letters.upper():
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value


def _tokenize(formula: str) -> list[tuple[str, Any]]:
    tokens: list[tuple[str, Any]] = []
    pos = 0
    while pos < len(formula):
        match = _TOKEN_RE.match(formula, pos)
        if match is None or match.end() == pos:
            raise FormulaError(f"无法识别的公式片段：{formula[pos:pos + 12]!r}")
        pos = match.end()
        if match.lastgroup == "number":
            tokens.append(("number", float(match.group())))
        elif match.lastgroup == "ref":
            tokens.append(("ref", match.group()))
        elif match.lastgroup == "name":
            tokens.append(("name", match.group().upper()))
        else:
            tokens.append(("op", match.group()))
    return tokens


def _round_half_up(value: float, digits: int) -> float:
    """Excel 的 ROUND 为四舍五入（远离 0），与 Python 的 banker's rounding 不同。"""
    factor = 10 ** digits
    scaled = abs(value) * factor
    result = int(scaled + 0.5) / factor
    return result if value >= 0 else -result


class _Parser:
    """递归下降解析器；单元格取值统一走 resolver 回调。"""

    def __init__(self, formula: str, resolver: Callable[[str | None, int, int, bool], float | None]):
        self.tokens = _tokenize(formula.lstrip("="))
        self.pos = 0
        self._resolve = resolver

    def parse(self) -> float:
        value = self._expression()
        if self.pos != len(self.tokens):
            raise FormulaError("公式尾部存在多余内容")
        return value

    # --- 词法辅助 ---
    def _peek(self) -> tuple[str, Any] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self) -> tuple[str, Any]:
        token = self._peek()
        if token is None:
            raise FormulaError("公式不完整")
        self.pos += 1
        return token

    def _accept_op(self, *ops: str) -> str | None:
        token = self._peek()
        if token is not None and token[0] == "op" and token[1] in ops:
            self.pos += 1
            return token[1]
        return None

    def _expect_op(self, op: str) -> None:
        if self._accept_op(op) is None:
            raise FormulaError(f"缺少 {op}")

    # --- 语法 ---
    def _expression(self) -> float:
        value = self._additive()
        while (op := self._accept_op("<=", ">=", "<>", "<", ">", "=")) is not None:
            right = self._additive()
            comparisons = {
                "<=": value <= right,
                ">=": value >= right,
                "<>": value != right,
                "<": value < right,
                ">": value > right,
                "=": value == right,
            }
            value = 1.0 if comparisons[op] else 0.0
        return value

    def _additive(self) -> float:
        value = self._multiplicative()
        while (op := self._accept_op("+", "-")) is not None:
            right = self._multiplicative()
            value = value + right if op == "+" else value - right
        return value

    def _multiplicative(self) -> float:
        value = self._unary()
        while (op := self._accept_op("*", "/")) is not None:
            right = self._unary()
            if op == "/":
                if right == 0:
                    raise FormulaError("除零")
                value = value / right
            else:
                value = value * right
        return value

    def _unary(self) -> float:
        op = self._accept_op("+", "-")
        if op == "-":
            return -self._unary()
        if op == "+":
            return self._unary()
        return self._power()

    def _power(self) -> float:
        value = self._postfix()
        if self._accept_op("^") is not None:
            return value ** self._unary()
        return value

    def _postfix(self) -> float:
        value = self._primary()
        while self._accept_op("%") is not None:
            value = value / 100
        return value

    def _primary(self) -> float:
        token = self._take()
        if token[0] == "number":
            return token[1]
        if token[0] == "name":
            name = token[1]
            if name in ("TRUE", "FALSE"):
                return 1.0 if name == "TRUE" else 0.0
            if self._peek() == ("op", "("):
                return self._call(name)
            raise FormulaError(f"不支持的名称引用：{name}")
        if token == ("op", "("):
            value = self._expression()
            self._expect_op(")")
            return value
        if token[0] == "ref":
            sheet, row, col = self._split_ref(token[1])
            return self._value(sheet, row, col, False)
        raise FormulaError(f"不支持的公式成分：{token[1]!r}")

    def _call(self, name: str) -> float:
        """函数调用：区间参数按值列表展开，其余按标量求值。"""
        arity = _FUNC_ARITY.get(name)
        if arity is None:
            raise FormulaError(f"不支持的函数：{name}")
        self._expect_op("(")
        args: list[float | list[float | None]] = []
        if self._accept_op(")") is None:
            while True:
                args.append(self._argument())
                if self._accept_op(",") is None:
                    break
            self._expect_op(")")
        minimum, maximum = arity
        if len(args) < minimum or (maximum is not None and len(args) > maximum):
            raise FormulaError(f"{name} 参数个数不合法：{len(args)}")

        if name in ("SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA"):
            flat = [value for arg in args for value in (arg if isinstance(arg, list) else [arg])]
            numbers = [value for value in flat if isinstance(value, (int, float))]
            if name == "SUM":
                return float(sum(numbers))
            if name == "COUNT":
                return float(len(numbers))
            if name == "COUNTA":
                return float(len([value for value in flat if value is not None]))
            if not numbers:
                return 0.0
            if name == "AVERAGE":
                return float(sum(numbers)) / len(numbers)
            return float(min(numbers)) if name == "MIN" else float(max(numbers))
        if name == "IF":
            if isinstance(args[0], list):
                raise FormulaError("IF 条件不能是区间")
            if args[0]:
                return self._scalar(args[1], name)
            return self._scalar(args[2], name) if len(args) == 3 else 0.0
        if name in ("AND", "OR"):
            flat = [value for arg in args for value in (arg if isinstance(arg, list) else [arg])]
            values = [bool(value) for value in flat if value is not None]
            result = all(values) if name == "AND" else any(values)
            return 1.0 if result else 0.0
        value = self._scalar(args[0], name)
        if name == "ABS":
            return abs(value)
        if name == "INT":
            return float(math.floor(value))
        if name == "NOT":
            return 0.0 if value else 1.0
        digits = int(self._scalar(args[1], name))
        factor = 10 ** digits
        if name == "ROUND":
            return _round_half_up(value, digits)
        scaled = abs(value) * factor
        if name == "ROUNDUP":
            rounded = math.ceil(scaled)
        else:
            rounded = math.floor(scaled)
        result = rounded / factor
        return result if value >= 0 else -result

    def _scalar(self, value: float | list[float | None], name: str) -> float:
        if isinstance(value, list):
            raise FormulaError(f"{name} 需要标量参数")
        return value

    def _argument(self) -> float | list[float | None]:
        if self._peek() is not None and self._peek()[0] == "ref":
            lookahead = self.tokens[self.pos + 1] if self.pos + 1 < len(self.tokens) else None
            if lookahead == ("op", ":"):
                first = self._take()[1]
                self._expect_op(":")
                return self._range_values(first, self._take()[1])
        return self._expression()

    def _range_values(self, first: str, last: str) -> list[float | None]:
        sheet_a, row_a, col_a = self._split_ref(first)
        sheet_b, row_b, col_b = self._split_ref(last)
        if sheet_a != sheet_b:
            raise FormulaError("区间跨越两张表")
        rows = range(min(row_a, row_b), max(row_a, row_b) + 1)
        cols = range(min(col_a, col_b), max(col_a, col_b) + 1)
        if len(rows) * len(cols) > MAX_RANGE_CELLS:
            raise FormulaError("区间过大")
        return [self._value(sheet_a, row, col, True) for row in rows for col in cols]

    def _split_ref(self, ref: str) -> tuple[str | None, int, int]:
        match = _REF_RE.match(ref)
        if match is None:
            raise FormulaError(f"无法解析引用：{ref}")
        sheet = match.group("sheet")
        if sheet is not None and sheet.startswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
        return sheet, int(match.group("row")), _col_to_index(match.group("col"))

    def _value(self, sheet: str | None, row: int, col: int, in_range: bool) -> float | None:
        return self._resolve(sheet, row, col, in_range)


class _Solver:
    """带缓存与循环引用检测的求解器：只有被引用到的空公式单元格才会计算。"""

    def __init__(self, value_book, formula_book):
        self._values = value_book
        self._formulas = formula_book
        self._solved: dict[tuple[str, int, int], float] = {}
        self._pending: set[tuple[str, int, int]] = set()
        self._failed: dict[tuple[str, int, int], str] = {}

    def value(self, sheet: str, row: int, col: int, in_range: bool) -> float | None:
        """单元格数值：缓存值优先 → 空公式单元格求解 → 空单元格为 0；文本单元格报错。

        in_range=True 表示处于区间引用中：文本/布尔/空单元格按"跳过"处理（Excel 的 SUM 语义）。
        """
        key = (sheet, row, col)
        if key in self._failed:
            raise FormulaError(f"{sheet}!R{row}C{col} 无法求值：{self._failed[key]}")
        if key in self._solved:
            return self._solved[key]
        if sheet not in self._values.sheetnames:
            raise FormulaError(f"引用不存在的表：{sheet}")
        cached = self._values[sheet].cell(row=row, column=col).value
        if isinstance(cached, bool) or isinstance(cached, (int, float)):
            return float(cached)
        formula = self._formulas[sheet].cell(row=row, column=col).value
        if isinstance(formula, str) and formula.startswith("="):
            return self._solve(key, formula)
        if cached is None:
            return None if in_range else 0.0
        if in_range:
            return None
        raise FormulaError("文本单元格不能参与算术")

    def _solve(self, key: tuple[str, int, int], formula: str) -> float:
        sheet, row, col = key
        if key in self._pending:
            raise FormulaError("循环引用")
        self._pending.add(key)
        try:
            parser = _Parser(formula, lambda ref_sheet, r, c, in_range: self.value(ref_sheet or sheet, r, c, in_range))
            result = parser.parse()
        except FormulaError as exc:
            self._failed[key] = str(exc)
            raise
        finally:
            self._pending.discard(key)
        self._solved[key] = result
        return result


def solve_missing_formulas(value_book, formula_book) -> tuple[dict[tuple[str, int, int], float], list[tuple[str, str, str]]]:
    """求解「缓存值为空且存在公式」的单元格。

    入参为两个已加载的工作簿：value_book（data_only=True，提供缓存值）与 formula_book
    （data_only=False，提供公式）。返回 ({(表, 行, 列): 数值}, [(表, 单元格坐标, 原因)])；
    只扫描并报告"有公式但缓存为空"的单元格，空单元格不应出现在结果里。
    """
    solver = _Solver(value_book, formula_book)
    solved: dict[tuple[str, int, int], float] = {}
    unresolved: list[tuple[str, str, str]] = []
    reported: set[tuple[str, int, int]] = set()
    for sheet_name in value_book.sheetnames:
        if sheet_name not in formula_book.sheetnames:
            continue
        value_sheet = value_book[sheet_name]
        for row in formula_book[sheet_name].iter_rows():
            for cell in row:
                formula = cell.value
                if not isinstance(formula, str) or not formula.startswith("="):
                    continue
                if value_sheet.cell(row=cell.row, column=cell.column).value is not None:
                    continue  # 缓存值优先：Excel 已算过的单元格不重算
                key = (sheet_name, cell.row, cell.column)
                try:
                    value = solver.value(sheet_name, cell.row, cell.column, False)
                    if value is not None:
                        solved[key] = display_number(value)
                except FormulaError as exc:
                    if key not in reported:
                        reported.add(key)
                        unresolved.append((sheet_name, f"R{cell.row}C{cell.column}", f"{exc}（{formula}）"))
    return solved, unresolved

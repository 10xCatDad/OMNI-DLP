"""Experimental research: recognize array-transform candidates without JS execution.

This is a recognizer, not a general JavaScript interpreter. Only structurally
verified array transforms are accepted. The separate YouTube ``n`` challenge is
not supported. It is disconnected from downloads: a unique recognizable transform
does not establish its role as the player's signature function. Standard native
operations are assumed; indirect whole-program prototype effects are not modeled.
Unsupported candidate syntax, ambiguous recognized plans and budgets raise errors.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
import hashlib
import math
import re
from typing import Literal

MAX_SOURCE = 8 * 1024 * 1024
MAX_TOKENS = 1_500_000
MAX_DEPTH = 128
MAX_EXPRESSION = 4096
MAX_OPERATIONS = 128
MAX_INPUT_UNITS = 16384
MAX_CANDIDATES = 128
MAX_FUNCTIONS = 40000
MAX_TABLES = 1024
_MAX_INTEGER = 2**31 - 1
_ASSIGNMENTS = {"=", "+=", "-=", "*=", "/=", "%=", "^=", "|=", "&=", "<<=", ">>=", ">>>=", "**=", "&&=", "||=", "??=", "++", "--"}


class UnsupportedChallenge(ValueError):
    """The input falls outside the verified signature language or its budgets."""


@dataclass(frozen=True, slots=True)
class SignatureOperation:
    kind: Literal["reverse", "drop", "swap"]
    argument: int = 0

    def __post_init__(self):
        if self.kind not in {"reverse", "drop", "swap"}:
            raise UnsupportedChallenge("Unknown signature operation")
        if type(self.argument) is not int or not 0 <= self.argument <= _MAX_INTEGER:
            raise UnsupportedChallenge("Invalid signature operation argument")
        if self.kind == "reverse" and self.argument:
            raise UnsupportedChallenge("Reverse takes no argument")


@dataclass(frozen=True, slots=True)
class SignaturePlan:
    operations: tuple[SignatureOperation, ...]
    source_sha256: str = ""

    def __post_init__(self):
        if (type(self.operations) is not tuple or not self.operations
                or len(self.operations) > MAX_OPERATIONS
                or any(type(op) is not SignatureOperation for op in self.operations)):
            raise UnsupportedChallenge("Invalid signature operation plan")
        if self.source_sha256 and not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise UnsupportedChallenge("Invalid signature source digest")


def apply_signature(plan: SignaturePlan, value: str) -> str:
    """Apply a verified plan using JavaScript UTF-16 string-unit semantics."""
    if type(plan) is not SignaturePlan or not isinstance(value, str):
        raise UnsupportedChallenge("Expected a signature plan and string")
    if len(value) > MAX_INPUT_UNITS:
        raise UnsupportedChallenge("Signature input limit exceeded")
    raw = value.encode("utf-16-le", "surrogatepass")
    if len(raw) // 2 > MAX_INPUT_UNITS:
        raise UnsupportedChallenge("Signature input limit exceeded")
    units = [raw[i:i + 2] for i in range(0, len(raw), 2)]
    for operation in plan.operations:
        if operation.kind == "reverse":
            units.reverse()
        elif operation.kind == "drop":
            del units[:operation.argument]
        else:
            if not units:
                raise UnsupportedChallenge("Cannot swap an empty signature")
            index = operation.argument % len(units)
            units[0], units[index] = units[index], units[0]
    return b"".join(units).decode("utf-16-le", "surrogatepass")


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    value: str
    newline: bool = False


_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"(?:0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?:n)?")
_OPERATOR = re.compile(r">>>=|===|!==|>>>|\*\*=|&&=|\|\|=|\?\?=|<<=|>>=|=>|==|!=|<=|>=|&&|\|\||\?\?|\?\.|\+\+|--|<<|>>|\*\*|[+*/%&|^!-]=|\.\.\.|[{}\[\]().,;:?~+*/%&|^!<>=-]")
_PREFIX = {"return", "throw", "case", "delete", "void", "typeof", "new", "yield", "await", "in", "of", "instanceof", "else", "do"}
_CONTROL = {"if", "while", "for", "with", "switch", "catch"}
_LINE_END = re.compile(r"[\r\n\u2028\u2029]")


def _string(source: str, start: int) -> tuple[str, int]:
    quote = source[start]
    result = []
    index = start + 1
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    while index < len(source):
        char = source[index]
        index += 1
        if char == quote:
            return "".join(result), index
        if char in "\r\n" or (quote == "`" and char == "$" and source[index:index + 1] == "{"):
            raise UnsupportedChallenge("Unsupported multiline or interpolated literal")
        if char != "\\":
            result.append(char)
            continue
        if index == len(source):
            break
        char = source[index]
        index += 1
        if char in "\r\n":
            if char == "\r" and source[index:index + 1] == "\n":
                index += 1
            continue
        if char in "123456789" or (char == "0" and source[index:index + 1].isdigit()):
            raise UnsupportedChallenge("Legacy numeric string escape is unsupported")
        if char in {"x", "u"}:
            count = 2 if char == "x" else 4
            if char == "u" and source[index:index + 1] == "{":
                end = source.find("}", index + 1, index + 9)
                if end < 0:
                    raise UnsupportedChallenge("Invalid Unicode escape")
                digits = source[index + 1:end]
                index = end + 1
            else:
                digits = source[index:index + count]
                index += count
                if len(digits) != count:
                    raise UnsupportedChallenge("Incomplete string escape")
            if not re.fullmatch(r"[0-9a-fA-F]{1,6}", digits) or int(digits, 16) > 0x10FFFF:
                raise UnsupportedChallenge("Invalid string escape")
            result.append(chr(int(digits, 16)))
        else:
            result.append(escapes.get(char, char))
    raise UnsupportedChallenge("Unterminated string")


def _lex(source: str) -> tuple[list[_Token], list[int]]:
    tokens: list[_Token] = []
    pairs: list[int] = []
    stack: list[tuple[int, str, bool]] = []
    index = 0
    expression_allowed = True
    newline = False
    while index < len(source):
        if len(tokens) >= MAX_TOKENS:
            raise UnsupportedChallenge("JavaScript token limit exceeded")
        char = source[index]
        if char.isspace():
            newline = newline or char in "\r\n\u2028\u2029"
            index += 1
            continue
        if source.startswith("//", index):
            match = _LINE_END.search(source, index + 2)
            newline = newline or match is not None
            index = len(source) if match is None else match.end()
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise UnsupportedChallenge("Unterminated comment")
            newline = newline or any(c in source[index:end] for c in "\r\n\u2028\u2029")
            index = end + 2
            continue
        if char in "\"'`":
            value, index = _string(source, index)
            token = _Token("opaque" if char == "`" else "string", value, newline)
            expression_allowed = False
        elif char == "/" and expression_allowed:
            start = index
            index += 1
            in_class = False
            while index < len(source):
                char = source[index]
                index += 1
                if char in "\r\n":
                    raise UnsupportedChallenge("Unterminated regular expression")
                if char == "\\":
                    index += 1
                elif char == "[":
                    in_class = True
                elif char == "]":
                    in_class = False
                elif char == "/" and not in_class:
                    break
            else:
                raise UnsupportedChallenge("Unterminated regular expression")
            while index < len(source) and source[index].isalpha():
                index += 1
            token = _Token("opaque", source[start:index], newline)
            expression_allowed = False
        elif match := _IDENTIFIER.match(source, index):
            value = match[0]
            token = _Token("id", value, newline)
            index = match.end()
            expression_allowed = value in _PREFIX
        elif match := _NUMBER.match(source, index):
            token = _Token("number", match[0], newline)
            index = match.end()
            expression_allowed = False
        elif match := _OPERATOR.match(source, index):
            value = match[0]
            token = _Token("op", value, newline)
            index = match.end()
            previous = tokens[-1].value if tokens else ""
            if value in {"(", "[", "{"}:
                stack.append((len(tokens), value, value == "(" and previous in _CONTROL))
                if len(stack) > MAX_DEPTH:
                    raise UnsupportedChallenge("JavaScript nesting limit exceeded")
                expression_allowed = True
            elif value in {")", "]", "}"}:
                if not stack:
                    raise UnsupportedChallenge("Unbalanced JavaScript delimiters")
                opening, kind, control = stack.pop()
                if {"(": ")", "[": "]", "{": "}"}[kind] != value:
                    raise UnsupportedChallenge("Mismatched JavaScript delimiters")
                pairs[opening] = len(tokens)
                pairs.append(opening)
                tokens.append(token)
                expression_allowed = control or value == "}"
                newline = False
                continue
            else:
                expression_allowed = value not in {"++", "--", ".", "?."}
        else:
            raise UnsupportedChallenge("Unsupported JavaScript token")
        tokens.append(token)
        pairs.append(-1)
        newline = False
        if len(tokens) > MAX_TOKENS:
            raise UnsupportedChallenge("JavaScript token limit exceeded")
    if stack:
        raise UnsupportedChallenge("Unclosed JavaScript delimiter")
    return tokens, pairs


def _integer(raw: str) -> int:
    try:
        if raw.lower().startswith(("0x", "0b", "0o")):
            number = int(raw, 0)
        else:
            value = float(raw)
            if not math.isfinite(value) or not value.is_integer():
                raise ValueError
            number = int(value)
    except (ValueError, OverflowError):
        raise UnsupportedChallenge("Unsupported numeric constant") from None
    if abs(number) > 2**53 - 1:
        raise UnsupportedChallenge("Inexact JavaScript integer")
    return number


# A small expression AST. Every accepted expression is fully consumed.
_PRECEDENCE = {",": 1, "=": 2, "||": 3, "&&": 4, "|": 5, "^": 6, "&": 7,
               "==": 8, "!=": 8, "===": 8, "!==": 8, "<": 9, ">": 9, "<=": 9, ">=": 9,
               "<<": 10, ">>": 10, ">>>": 10, "+": 11, "-": 11, "*": 12, "/": 12, "%": 12}


class _Parser:
    def __init__(self, tokens: list[_Token], start: int, end: int):
        if end - start > MAX_EXPRESSION:
            raise UnsupportedChallenge("Expression size limit exceeded")
        self.tokens, self.pos, self.end = tokens, start, end

    def take(self, value: str) -> bool:
        if self.pos < self.end and self.tokens[self.pos].kind == "op" and self.tokens[self.pos].value == value:
            self.pos += 1
            return True
        return False

    def require(self, value: str):
        if not self.take(value):
            raise UnsupportedChallenge("Unsupported expression syntax")

    def parse(self, minimum: int = 1, depth: int = 0):
        if depth >= 64 or self.pos >= self.end:
            raise UnsupportedChallenge("Invalid or deeply nested expression")
        token = self.tokens[self.pos]
        self.pos += 1
        if token.kind == "string":
            left = ("literal", token.value)
        elif token.kind == "number":
            left = ("literal", _integer(token.value))
        elif token.value in {"!", "~", "+", "-"}:
            left = ("unary", token.value, self.parse(13, depth + 1))
        elif token.value == "(":
            left = self.parse(1, depth + 1)
            self.require(")")
        elif token.kind == "id" and token.value not in {"function", "new", "delete", "typeof", "void", "this"}:
            left = ("id", token.value)
        else:
            raise UnsupportedChallenge("Unsupported expression atom")
        while self.pos < self.end:
            if self.tokens[self.pos].kind != "op":
                break
            value = self.tokens[self.pos].value
            if value == ".":
                self.pos += 1
                if self.pos >= self.end or self.tokens[self.pos].kind != "id":
                    raise UnsupportedChallenge("Invalid property")
                left = ("member", left, ("literal", self.tokens[self.pos].value))
                self.pos += 1
            elif value == "[":
                self.pos += 1
                key = self.parse(1, depth + 1)
                self.require("]")
                left = ("member", left, key)
            elif value == "(":
                self.pos += 1
                args = []
                if not self.take(")"):
                    while True:
                        args.append(self.parse(2, depth + 1))
                        if self.take(")"):
                            break
                        self.require(",")
                left = ("call", left, tuple(args))
            elif value in _PRECEDENCE and _PRECEDENCE[value] >= minimum:
                self.pos += 1
                priority = _PRECEDENCE[value]
                right = self.parse(priority if value == "=" else priority + 1, depth + 1)
                left = ("binary", value, left, right)
            else:
                break
        return left

    def complete(self):
        result = self.parse()
        if self.pos != self.end:
            raise UnsupportedChallenge("Unrecognized expression tail")
        return result


def _signed32(value: int) -> int:
    return (value + 2**31) % 2**32 - 2**31


def _numeric(operator: str, left, right):
    if type(left) not in {int, bool} or type(right) not in {int, bool}:
        raise UnsupportedChallenge("Nonconstant numeric expression")
    if operator in {"==", "===", "!=", "!==", "<", ">", "<=", ">="}:
        return {"==": left == right, "===": type(left) is type(right) and left == right,
                "!=": left != right, "!==": type(left) is not type(right) or left != right,
                "<": left < right, ">": left > right, "<=": left <= right, ">=": left >= right}[operator]
    if operator == "^":
        return _signed32(left) ^ _signed32(right)
    if operator == "&":
        return _signed32(left) & _signed32(right)
    if operator == "|":
        return _signed32(left) | _signed32(right)
    if operator == "<<":
        return _signed32(left << (right & 31))
    if operator == ">>":
        return _signed32(left) >> (right & 31)
    if operator == ">>>":
        return (left % 2**32) >> (right & 31)
    if operator == "+":
        result = left + right
    elif operator == "-":
        result = left - right
    elif operator == "*":
        result = left * right
    elif operator in {"/", "%"} and right:
        quotient = math.trunc(left / right)
        if operator == "/" and quotient * right != left:
            raise UnsupportedChallenge("Noninteger division")
        result = quotient if operator == "/" else left - quotient * right
    else:
        raise UnsupportedChallenge("Unsupported numeric operation")
    if abs(result) > 2**53 - 1:
        raise UnsupportedChallenge("Arithmetic precision limit exceeded")
    return result


def _constant(node, names: dict):
    if node[0] == "literal":
        return node[1]
    if node[0] == "id" and node[1] in names:
        return names[node[1]]
    if node[0] == "id" and node[1] in {"true", "false", "null", "undefined"}:
        return {"true": True, "false": False, "null": None, "undefined": _UNDEFINED}[node[1]]
    if node[0] == "member":
        value, key = _constant(node[1], names), _constant(node[2], names)
        if type(value) is tuple and type(key) is int and 0 <= key < len(value):
            return value[key]
    if node[0] == "unary":
        value = _constant(node[2], names)
        if type(value) in {int, bool}:
            return {"!": lambda: not value, "~": lambda: ~_signed32(value),
                    "+": lambda: int(value), "-": lambda: -int(value)}[node[1]]()
    if node[0] == "binary":
        return _numeric(node[1], _constant(node[2], names), _constant(node[3], names))
    raise UnsupportedChallenge("Expression is not a supported constant")


@dataclass(slots=True)
class _Function:
    name: str
    params: tuple[str, ...]
    start: int
    end: int
    definition: int
    parent: int = -1


class _Source:
    def __init__(self, source: str):
        self.tokens, self.pairs = _lex(source)
        self.functions: list[_Function] = []
        self.tables: dict[str, tuple[str, ...]] = {}
        self.helpers: dict[str, dict[str, tuple[str, str]]] = {}
        self.bindings: dict[str, int] = {}
        self.refs: dict[str, list[int]] = {}
        self._declarations: dict[int, set[int]] = {}
        self._index()

    def value(self, index: int) -> str:
        return self.tokens[index].value if 0 <= index < len(self.tokens) else ""

    def split(self, start: int, end: int, separator: str) -> list[tuple[int, int]]:
        parts = []
        left = start
        while start < end:
            if self.tokens[start].kind == "op" and self.value(start) == separator:
                parts.append((left, start))
                left = start + 1
            elif self.pairs[start] > start:
                start = self.pairs[start]
            start += 1
        parts.append((left, end))
        return parts

    def expression(self, start: int, end: int):
        return _Parser(self.tokens, start, end).complete()

    def _index(self):
        tokens = self.tokens
        for index, token in enumerate(tokens):
            if token.kind == "id":
                self.refs.setdefault(token.value, []).append(index)
            if token.kind != "id" or token.value != "function":
                continue
            opening = index + 1
            name = ""
            if self.value(opening) != "(":
                name = self.value(opening)
                opening += 1
            elif self.value(index - 1) == "=" and tokens[index - 2].kind == "id" and self.value(index - 3) != ".":
                name = self.value(index - 2)
            if self.value(opening) != "(" or self.value(self.pairs[opening] + 1) != "{":
                continue
            close = self.pairs[opening]
            args = self.split(opening + 1, close, ",") if close > opening + 1 else []
            if any(end != start + 1 or tokens[start].kind != "id" for start, end in args):
                continue
            params = tuple(tokens[start].value for start, _ in args)
            if len(set(params)) != len(params):
                continue
            self.functions.append(_Function(name, params, close + 2, self.pairs[close + 1], index))
            if len(self.functions) > MAX_FUNCTIONS:
                raise UnsupportedChallenge("JavaScript function limit exceeded")
        self.function_by_definition = {function.definition: function for function in self.functions}
        self.function_names: dict[str, list[_Function]] = {}
        self.parameter_names = {name for function in self.functions for name in function.params}
        for function in self.functions:
            self.function_names.setdefault(function.name, []).append(function)
        stack = []
        for index, function in enumerate(self.functions):
            while stack and self.functions[stack[-1]].end < function.start:
                stack.pop()
            function.parent = stack[-1] if stack else -1
            stack.append(index)
        self.function_starts = [function.start for function in self.functions]
        for index, token in enumerate(tokens):
            # Literal "words".split("delimiter") only; no computed table builders.
            if (token.kind != "id" or self.value(index + 1) != "="
                    or self.value(index - 1) == "." or index + 8 >= len(tokens)):
                continue
            if (tokens[index + 2].kind == "string" and self.value(index + 3) == "."
                    and self.value(index + 4) == "split" and self.value(index + 5) == "("
                    and tokens[index + 6].kind == "string" and self.value(index + 7) == ")"
                    and self.value(index + 8) in {";", ",", "}"}):
                delimiter = tokens[index + 6].value
                if not delimiter:
                    continue
                table = tuple(tokens[index + 2].value.split(delimiter))
                if len(table) > 4096 or any(len(value) > 65536 for value in table):
                    raise UnsupportedChallenge("String table limit exceeded")
                self.tables[token.value] = table
                self.bindings[token.value] = index
                if len(self.tables) > MAX_TABLES:
                    raise UnsupportedChallenge("String table count limit exceeded")
        for name in tuple(self.tables):
            if not self.stable(name, self.bindings[name]):
                del self.tables[name]

    def stable(self, name: str, binding: int) -> bool:
        """Conservative unique-binding/no-escape check for external constants."""
        if not self.unconditional(binding):
            return False
        if name in self.parameter_names or name in self.function_names:
            return False
        for index in self.refs.get(name, []):
            if index == binding or self.value(index - 1) in {".", "?."}:
                continue
            after = index + 1
            if self.value(after) == "[":
                after = self.pairs[after] + 1
            elif self.value(after) == "." and self.tokens[after + 1].kind == "id":
                after += 2
            else:
                return False
            if self.value(after) in _ASSIGNMENTS | {"[", "."}:
                return False
            if self.value(index - 1) in {"delete", "++", "--"}:
                return False
        return True

    def unconditional(self, binding: int) -> bool:
        """Require an ordinary declaration at the containing function's top level."""
        owner = self.containing(binding)
        start, end = (owner.start, owner.end) if owner else (0, len(self.tokens))
        if start not in self._declarations:
            declarations = set()
            position = start
            while position < end:
                if self.value(position) == ";":
                    position += 1
                    continue
                stop = self.statement_end(position, end)
                right = stop - 1 if self.value(stop - 1) == ";" else stop
                left = position + 1 if self.value(position) in {"var", "let", "const"} else position
                for begin, finish in self.split(left, right, ","):
                    if finish > begin + 2 and self.tokens[begin].kind == "id" and self.value(begin + 1) == "=":
                        declarations.add(begin)
                if self.value(position) == "function" and self.tokens[position + 1].kind == "id":
                    declarations.add(position + 1)
                position = stop
            self._declarations[start] = declarations
        return binding in self._declarations[start]

    def visible(self, name: str, function: _Function) -> bool:
        owner = self.containing(self.bindings[name])
        return owner is None or owner.start <= function.start and function.end <= owner.end

    def containing(self, position: int) -> _Function | None:
        index = bisect_right(self.function_starts, position) - 1
        while index >= 0:
            function = self.functions[index]
            if function.start <= position < function.end:
                return function
            index = function.parent
        return None

    def statement_end(self, start: int, end: int, depth: int = 0) -> int:
        if depth > 64 or start >= end:
            raise UnsupportedChallenge("Invalid statement boundary")
        value = self.value(start)
        if value == "{":
            return self.pairs[start] + 1
        if value == "function" and self.value(start + 2) == "(":
            opening = self.pairs[start + 2] + 1
            if self.value(opening) == "{":
                return self.pairs[opening] + 1
        if value in {"if", "for", "while", "with", "switch"}:
            if self.value(start + 1) != "(":
                raise UnsupportedChallenge("Invalid control statement")
            result = self.statement_end(self.pairs[start + 1] + 1, end, depth + 1)
            if value == "if" and self.value(result) == "else":
                result = self.statement_end(result + 1, end, depth + 1)
            return result
        if self.value(start + 1) == ":":
            return self.statement_end(start + 2, end, depth + 1)
        while start < end:
            if self.tokens[start].kind == "op" and self.value(start) == ";":
                return start + 1
            if self.pairs[start] > start:
                start = self.pairs[start]
            start += 1
        return end


def _canonical(node, params: tuple[str, ...], tables: dict):
    if not node:
        return node
    if node[0] == "id":
        return ("id", f"p{params.index(node[1])}") if node[1] in params else node
    if node[0] == "member":
        try:
            return ("literal", _constant(node, tables))
        except UnsupportedChallenge:
            pass
    return tuple(_canonical(value, params, tables) if isinstance(value, tuple) else value for value in node)


def _helper(source: _Source, function: _Function) -> tuple[str, str]:
    params = function.params
    if not 1 <= len(params) <= 2:
        raise UnsupportedChallenge("Unsupported helper arguments")
    statements = []
    tables = {name: table for name, table in source.tables.items() if source.visible(name, function)}
    for start, end in source.split(function.start, function.end, ";"):
        if start == end:
            continue
        declaration = source.value(start)
        if declaration in {"var", "let", "const", "return"}:
            if declaration == "return" and source.tokens[start + 1].newline:
                raise UnsupportedChallenge("Automatic semicolon after helper return")
            start += 1
        else:
            declaration = "expression"
        statements.append((declaration, _canonical(source.expression(start, end), params, tables)))
    p0, p1 = ("id", "p0"), ("id", "p1")
    def member(obj, key):
        return ("member", obj, ("literal", key))

    def call(name, args):
        return ("call", member(p0, name), args)
    if statements == [("expression", call("reverse", ()))]:
        return "reverse", "undefined"
    if statements == [("return", call("reverse", ()))]:
        return "reverse", "array"
    if len(params) == 2 and statements == [("expression", call("splice", (("literal", 0), p1)))]:
        return "drop", "undefined"
    if len(params) == 2 and statements == [("return", call("slice", (p1,)))]:
        return "drop", "slice"
    if len(params) == 2 and len(statements) == 3:
        declaration, first = statements[0]
        if (declaration not in {"var", "let", "const"} or first[0:2] != ("binary", "=")
                or first[2][0] != "id" or first[2][1] in {"p0", "p1"}):
            raise UnsupportedChallenge("Unsupported swap temporary")
        temporary = first[2]
        zero = member(p0, 0)
        position = ("binary", "%", p1, member(p0, "length"))
        slot = ("member", p0, position)
        expected = [(declaration, ("binary", "=", temporary, zero)),
                    ("expression", ("binary", "=", zero, slot)),
                    ("expression", ("binary", "=", slot, temporary))]
        if statements == expected:
            return "swap", "undefined"
    raise UnsupportedChallenge("Helper body is outside the verified transform language")


def _helpers(source: _Source):
    by_definition = {function.definition: function for function in source.functions}
    for index, token in enumerate(source.tokens):
        if (token.kind != "id" or source.value(index + 1) != "=" or source.value(index + 2) != "{"
                or source.value(index - 1) == "."):
            continue
        end = source.pairs[index + 2]
        entries = source.split(index + 3, end, ",")
        if not 1 <= len(entries) <= 16:
            continue
        methods = {}
        try:
            for start, stop in entries:
                if (source.tokens[start].kind not in {"id", "string"} or source.value(start + 1) != ":"
                        or source.value(start + 2) != "function"):
                    raise UnsupportedChallenge("Nonliteral helper object")
                function = by_definition.get(start + 2)
                if function is None or function.end + 1 != stop:
                    raise UnsupportedChallenge("Unsupported helper function")
                name = source.value(start)
                if name in methods or name in {"__proto__", "constructor", "prototype"}:
                    raise UnsupportedChallenge("Ambiguous helper property")
                methods[name] = _helper(source, function)
            if source.stable(token.value, index):
                source.helpers[token.value] = methods
                source.bindings[token.value] = index
        except (UnsupportedChallenge, RecursionError):
            continue


_INPUT = object()
_UNDEFINED = object()
_REMOVED = object()


@dataclass(slots=True)
class _Array:
    operations: list[SignatureOperation] = field(default_factory=list)
    sealed: bool = False


@dataclass(frozen=True, slots=True)
class _Joined:
    operations: tuple[SignatureOperation, ...]


@dataclass(frozen=True, slots=True)
class _Slice:
    argument: int


class _Machine:
    def __init__(self, source: _Source, function: _Function, arguments: dict):
        self.source, self.function = source, function
        self.names = dict(arguments)
        self.array: _Array | None = None
        self.steps = 0
        self.used_helper = False
        self.locals = set(function.params)
        # var declarations are hoisted even when their dispatcher arm is skipped.
        position = function.start
        while position < function.end:
            if position in source.function_by_definition:
                position = source.function_by_definition[position].end + 1
                continue
            if source.value(position) == "var":
                self.locals.add(source.value(position + 1))
            position += 1

    def step(self):
        self.steps += 1
        if self.steps > 4096:
            raise UnsupportedChallenge("Signature evaluation budget exceeded")

    def truth(self, value):
        if isinstance(value, _Array) or value is _REMOVED:
            return True
        if type(value) not in {int, bool, str} and value is not _UNDEFINED:
            raise UnsupportedChallenge("Condition depends on signature data")
        return value is not _UNDEFINED and bool(value)

    def evaluate(self, node):
        self.step()
        kind = node[0]
        if kind == "literal":
            return node[1]
        if kind == "id":
            name = node[1]
            if name in self.names:
                return self.names[name]
            if name in self.source.tables:
                if not self.source.visible(name, self.function):
                    raise UnsupportedChallenge("String table is outside the signature scope")
                return self.source.tables[name]
            if name in self.source.helpers:
                if not self.source.visible(name, self.function):
                    raise UnsupportedChallenge("Helper is outside the signature scope")
                return self.source.helpers[name]
            if name in {"true", "false"}:
                return name == "true"
            raise UnsupportedChallenge("Unresolved signature identifier")
        if kind == "unary":
            value = self.evaluate(node[2])
            return _constant(("unary", node[1], ("literal", value)), {})
        if kind == "binary":
            operator = node[1]
            if operator == "=":
                if node[2][0] != "id" or node[2][1] not in self.locals:
                    raise UnsupportedChallenge("Signature writes outside local variables")
                result = self.evaluate(node[3])
                if isinstance(result, _Slice):
                    if self.names.get(node[2][1]) is not self.array or self.array is None or self.array.sealed:
                        raise UnsupportedChallenge("Slice must replace its source array")
                    self.array = _Array(list(self.array.operations))
                    self.transform("drop", result.argument)
                    result = self.array
                self.names[node[2][1]] = result
                return result
            left = self.evaluate(node[2])
            if isinstance(left, _Slice):
                raise UnsupportedChallenge("Discarded slice result")
            if operator == "&&":
                return self.evaluate(node[3]) if self.truth(left) else left
            if operator == "||":
                return left if self.truth(left) else self.evaluate(node[3])
            right = self.evaluate(node[3])
            return right if operator == "," else _numeric(operator, left, right)
        if kind == "member":
            value, key = self.evaluate(node[1]), self.evaluate(node[2])
            if type(value) is tuple and type(key) is int and 0 <= key < len(value):
                return value[key]
            if type(value) is dict and isinstance(key, str) and key in value:
                return ("helper", value[key])
            if value is _INPUT and key == "split":
                return ("split",)
            if value is self.array and self.array is not None and key in {"join", "reverse", "splice", "slice"}:
                return (key,)
            raise UnsupportedChallenge("Unsupported signature property access")
        if kind == "call":
            if node[1][0] != "member":
                raise UnsupportedChallenge("Unbound signature method call")
            target = self.evaluate(node[1])
            args = tuple(self.evaluate(arg) for arg in node[2])
            if target == ("split",) and args == ("",) and self.array is None:
                self.array = _Array()
                return self.array
            if target == ("join",) and args == ("",) and self.array is not None and not self.array.sealed:
                self.array.sealed = True
                return _Joined(tuple(self.array.operations))
            if type(target) is tuple and target[0] == "helper":
                operation, return_kind = target[1]
                if not args or args[0] is not self.array or not 1 <= len(args) <= 2:
                    raise UnsupportedChallenge("Helper receives unrelated data")
                if operation != "reverse" and len(args) != 2:
                    raise UnsupportedChallenge("Missing helper argument")
                argument = 0 if operation == "reverse" else args[1]
                self.used_helper = True
                if return_kind == "slice":
                    return _Slice(SignatureOperation(operation, argument).argument)
                self.transform(operation, argument)
                return self.array if return_kind == "array" else _UNDEFINED
            if target == ("reverse",) and not args:
                self.transform("reverse", 0)
                return self.array
            if target == ("splice",) and len(args) == 2 and args[0] == 0:
                self.transform("drop", args[1])
                return _REMOVED
            # Slice returns a new array. Only immediate assignment can preserve it.
            if target == ("slice",) and len(args) == 1:
                return _Slice(SignatureOperation("drop", args[0]).argument)
            raise UnsupportedChallenge("Unsupported signature call")
        raise UnsupportedChallenge("Unsupported signature expression")

    def transform(self, operation: str, argument: int):
        if self.array is None or self.array.sealed:
            raise UnsupportedChallenge("Invalid signature mutation order")
        self.array.operations.append(SignatureOperation(operation, argument))
        if len(self.array.operations) > MAX_OPERATIONS:
            raise UnsupportedChallenge("Signature operation limit exceeded")

    def lazy(self, start: int, end: int):
        source = self.source
        while source.value(start) == "(" and source.pairs[start] == end - 1:
            start, end = start + 1, end - 1
        for operator in (",", "=", "||", "&&"):
            parts = source.split(start, end, operator)
            if len(parts) <= 1:
                continue
            if operator == "=":
                break
            result = self.lazy(*parts[0])
            for left, right in parts[1:]:
                if isinstance(result, _Slice):
                    raise UnsupportedChallenge("Discarded slice result")
                if (operator == "&&" and not self.truth(result)) or (operator == "||" and self.truth(result)):
                    return result
                result = self.lazy(left, right)
            return result
        return self.evaluate(source.expression(start, end))

    def run(self, start: int, end: int):
        source = self.source
        while start < end:
            self.step()
            token = source.value(start)
            if token == ";":
                start += 1
                continue
            if token == "{":
                result = self.run(start + 1, source.pairs[start])
                if result is not None:
                    return result
                start = source.pairs[start] + 1
                continue
            if token == "if":
                if source.value(start + 1) != "(":
                    raise UnsupportedChallenge("Malformed dispatcher condition")
                close = source.pairs[start + 1]
                condition = self.truth(self.lazy(start + 2, close))
                body_start = close + 1
                body_end = source.statement_end(body_start, end)
                following = body_end
                alternate = None
                if source.value(following) == "else":
                    alternate = (following + 1, source.statement_end(following + 1, end))
                    following = alternate[1]
                if condition:
                    result = self.run(body_start, body_end)
                elif alternate:
                    result = self.run(*alternate)
                else:
                    result = None
                if result is not None:
                    return result
                start = following
                continue
            stop = source.statement_end(start, end)
            expression_end = stop - 1 if source.value(stop - 1) == ";" else stop
            if token in {"var", "let", "const"}:
                if token != "var":
                    raise UnsupportedChallenge("Block-scoped signature declarations are unsupported")
                for left, right in source.split(start + 1, expression_end, ","):
                    if source.tokens[left].kind != "id":
                        raise UnsupportedChallenge("Unsupported local declaration")
                    name = source.value(left)
                    self.locals.add(name)
                    if right == left + 1:
                        self.names.setdefault(name, _UNDEFINED)
                    else:
                        self.lazy(left, right)
            elif token == "return":
                if start + 1 >= expression_end or source.tokens[start + 1].newline:
                    raise UnsupportedChallenge("Empty or automatically terminated signature return")
                if any(source.value(i) != ";" for i in range(stop, end)):
                    raise UnsupportedChallenge("Statements follow signature return")
                result = self.lazy(start + 1, expression_end)
                if not isinstance(result, _Joined):
                    raise UnsupportedChallenge("Signature does not return its transformed input")
                return result
            else:
                if isinstance(self.lazy(start, expression_end), _Slice):
                    raise UnsupportedChallenge("Discarded slice result")
            start = stop
        return None


def _arguments(source: _Source, function: _Function):
    if len(function.params) == 1:
        yield {function.params[0]: _INPUT}
        return
    if not function.name:
        return
    count = 0
    for index in source.refs.get(function.name, []):
        if source.value(index + 1) != "(" or source.value(index - 1) in {"function", "."}:
            continue
        closing = source.pairs[index + 1]
        parts = source.split(index + 2, closing, ",")
        if not 1 <= len(parts) <= len(function.params):
            continue
        values = dict.fromkeys(function.params, _UNDEFINED)
        unknown = 0
        for name, (start, end) in zip(function.params, parts):
            try:
                node = source.expression(start, end)
            except UnsupportedChallenge:
                break
            try:
                values[name] = _constant(node, {})
            except UnsupportedChallenge:
                if not any(token.kind == "id" and token.value not in {"true", "false", "null", "undefined"}
                           for token in source.tokens[start:end]):
                    break
                values[name] = _INPUT
                unknown += 1
        else:
            if unknown == 1:
                count += 1
                if count > MAX_CANDIDATES:
                    raise UnsupportedChallenge("Dispatcher call-site limit exceeded")
                yield values


def _check_intrinsics(source: _Source):
    """Reject explicit interference with the standard operations we recognize.

    This does not assert whole-program equivalence for arbitrary JavaScript.
    Standard, unmodified string/array intrinsics are part of the accepted subset.
    """
    if source.refs.get("eval"):
        raise UnsupportedChallenge("Dynamic evaluation prevents signature analysis")
    for index in source.refs.get("Function", []):
        if source.value(index - 1) == "new" or source.value(index + 1) == "(":
            raise UnsupportedChallenge("Dynamic function construction prevents signature analysis")

    def property_at(position):
        if source.value(position) == "." and source.tokens[position + 1].kind == "id":
            return source.value(position + 1), position + 2
        if source.value(position) == "[":
            close = source.pairs[position]
            try:
                value = _constant(source.expression(position + 1, close), source.tables)
            except UnsupportedChallenge:
                value = None
            return value, close + 1
        return None, position

    for name in ("Array", "String"):
        if name in source.parameter_names:
            raise UnsupportedChallenge("Native constructor is shadowed")
        for index in source.refs.get(name, []):
            if source.value(index - 1) in {".", "?."}:
                continue
            if source.value(index + 1) in _ASSIGNMENTS:
                raise UnsupportedChallenge("Native constructor is reassigned")
            key, position = property_at(index + 1)
            if key != "prototype":
                continue
            count = 0
            while source.value(position) in {".", "["}:
                _, position = property_at(position)
                count += 1
            if source.value(position) in _ASSIGNMENTS or source.value(index - 1) in {"delete", "++", "--"}:
                raise UnsupportedChallenge("Native prototype mutation is unsupported")
            left, right = index, position
            while source.value(left - 1) == "(" and source.pairs[left - 1] == right:
                left, right = left - 1, right + 1
            if not count and source.value(left - 1) == "=":
                raise UnsupportedChallenge("Native prototype alias is unsupported")
            if (not count and source.value(position) == "," and position + 1 < len(source.tokens)
                    and source.tokens[position + 1].kind == "string"
                    and source.value(position + 1) in {"split", "join", "reverse", "splice", "slice"}):
                raise UnsupportedChallenge("Native method definition is unsupported")


def _compile_signature(source: str) -> SignaturePlan:
    """Derive a unique signature plan from supported source structures.

    No source code is executed. Multi-purpose dispatchers are specialized only
    using constant arguments observed at call sites in the same source.
    """
    if not isinstance(source, str) or len(source) > MAX_SOURCE:
        raise UnsupportedChallenge("Player source size limit exceeded")
    parsed = _Source(source)
    _check_intrinsics(parsed)
    _helpers(parsed)
    candidates: dict[int, _Function] = {}
    for name in parsed.helpers:
        for index in parsed.refs[name]:
            if parsed.value(index - 1) in {".", "?."} or parsed.value(index + 1) == ":":
                continue
            function = parsed.containing(index)
            if function and function.name:
                candidates[function.definition] = function
    # Small standalone functions can use direct reverse/splice/slice calls.
    for function in parsed.functions:
        if (function.name and len(function.params) == 1 and function.end - function.start <= MAX_EXPRESSION
                and any(parsed.value(index) == "split" for index in range(function.start, function.end))):
            candidates[function.definition] = function
    if len(candidates) > MAX_CANDIDATES:
        raise UnsupportedChallenge("Signature candidate limit exceeded")
    accepted: dict[tuple[int, tuple[SignatureOperation, ...]], _Joined] = {}
    attempts = 0
    for function in candidates.values():
        if function.end - function.start > 65536:
            raise UnsupportedChallenge("Signature function size limit exceeded")
        # Reject rebinding or shadowing the callable being specialized. Forward
        # declarations are harmless; another definition or assignment is not.
        definitions = parsed.function_names.get(function.name, [])
        if len(definitions) != 1 or function.name in parsed.parameter_names:
            continue
        binding = function.definition + 1 if parsed.value(function.definition + 1) == function.name else function.definition - 2
        if not parsed.unconditional(binding):
            continue
        if any(parsed.value(index + 1) in _ASSIGNMENTS and parsed.value(index - 1) not in {".", "?."}
               and index != function.definition - 2 for index in parsed.refs.get(function.name, [])):
            continue
        for arguments in _arguments(parsed, function):
            attempts += 1
            if attempts > MAX_CANDIDATES * 2:
                raise UnsupportedChallenge("Total signature call-site limit exceeded")
            machine = _Machine(parsed, function, arguments)
            try:
                result = machine.run(function.start, function.end)
                if result is not None and result.operations:
                    accepted[(function.definition, result.operations)] = result
            except (UnsupportedChallenge, RecursionError):
                continue
    if len(accepted) != 1:
        raise UnsupportedChallenge("No unique supported signature transform found")
    result = next(iter(accepted.values()))
    return SignaturePlan(result.operations, hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest())


def compile_signature(source: str) -> SignaturePlan:
    """Experimental transform discovery; this does not prove signature-role linkage.

    Never executes supplied source. The caller must establish that a recognized
    transform is actually used for signatures and that the subset's assumptions
    hold. Production extraction does not currently call this function.
    """
    try:
        return _compile_signature(source)
    except RecursionError:
        raise UnsupportedChallenge("JavaScript syntax-tree depth limit exceeded") from None

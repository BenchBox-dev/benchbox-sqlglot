from __future__ import annotations

import re
import typing as t

from sqlglot import exp
from sqlglot.benchbox.division import mark_exact_division
from sqlglot.parsers.postgres import PostgresParser
from sqlglot.tokens import TokenType

_RELATIVE_DAYS = re.compile(r"\s*([+-]?)\s*(\d+)\s+(day|month|year)s?\s*", re.IGNORECASE)
_UNIX_EPOCH_JULIAN_DAY = 2440588


def _is_now(node: exp.Expr) -> bool:
    return isinstance(node, exp.Literal) and node.is_string and node.name.lower() == "now"


def _build_date(args: list[exp.Expr]) -> exp.Expr:
    if args and _is_now(args[0]):
        if len(args) == 1:
            return exp.CurrentDate()
        match = (
            _RELATIVE_DAYS.fullmatch(args[1].name) if len(args) == 2 and args[1].is_string else None
        )
        if match:
            sign, amount, unit = match.groups()
            klass = exp.DateSub if sign == "-" else exp.DateAdd
            return klass(
                this=exp.CurrentDate(),
                expression=exp.Literal.number(amount),
                unit=exp.var(unit.upper()),
            )
    return exp.Date.from_arg_list(args)


def _build_julianday(args: list[exp.Expr]) -> exp.Expr:
    day_number = exp.Add(
        this=exp.DateDiff(
            this=args[0],
            expression=exp.cast(exp.Literal.string("1970-01-01"), exp.DType.DATE),
            unit=exp.var("DAY"),
        ),
        expression=exp.Literal.number(_UNIX_EPOCH_JULIAN_DAY),
    )
    day_number.meta["julianday"] = True
    return exp.Paren(this=day_number)


def _is_julianday(node: exp.Expr) -> bool:
    return bool(node.unnest().meta.get("julianday"))


class NetezzaParser(PostgresParser):
    FUNCTIONS: t.ClassVar = {
        **PostgresParser.FUNCTIONS,
        "DATE": _build_date,
        "JULIANDAY": _build_julianday,
    }

    def _parse_factor(self, parse_mod: bool = True) -> exp.Expr | None:
        start = self._index
        this = super()._parse_factor(parse_mod=parse_mod)
        mark_exact_division(this)
        if (
            isinstance(this, exp.Literal)
            and this.is_number
            and this.name.isdigit()
            and start > 0
            and self._tokens[start - 1].token_type in (TokenType.PLUS, TokenType.DASH)
            and self._curr
            and self._curr.text.upper() in ("DAY", "DAYS")
        ):
            self._advance()
            return exp.Interval(this=exp.Literal.string(this.name), unit=exp.var("DAY"))
        return this

    def _parse_term(self, parse_mod: bool = True) -> exp.Expr | None:
        this = super()._parse_term(parse_mod=parse_mod)
        node = this
        while isinstance(node, (exp.Add, exp.Sub)):
            if (
                isinstance(node, exp.Sub)
                and _is_julianday(node.this)
                and _is_julianday(node.expression)
            ):
                difference = exp.DateDiff(
                    this=node.this.unnest().this.this,
                    expression=node.expression.unnest().this.this,
                    unit=exp.var("DAY"),
                )
                if node is this:
                    return difference
                node.replace(difference)
                break
            node = node.this
        return this

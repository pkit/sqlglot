from __future__ import annotations

import typing as t

from sqlglot import exp, generator, parser, tokens
from sqlglot.dialects.dialect import Dialect, inline_array_sql, rename_func, var_map_sql
from sqlglot.errors import ParseError
from sqlglot.parser import parse_var_map
from sqlglot.tokens import Token, TokenType


def _lower_func(sql: str) -> str:
    index = sql.index("(")
    return sql[:index].lower() + sql[index:]


class ClickHouse(Dialect):
    normalize_functions = None
    null_ordering = "nulls_are_last"

    class Tokenizer(tokens.Tokenizer):
        COMMENTS = ["--", "#", "#!", ("/*", "*/")]
        IDENTIFIERS = ['"', "`"]
        BIT_STRINGS = [("0b", "")]
        HEX_STRINGS = [("0x", ""), ("0X", "")]

        KEYWORDS = {
            **tokens.Tokenizer.KEYWORDS,
            "ASOF": TokenType.ASOF,
            "ATTACH": TokenType.COMMAND,
            "GLOBAL": TokenType.GLOBAL,
            "DATETIME64": TokenType.DATETIME64,
            "FINAL": TokenType.FINAL,
            "FLOAT32": TokenType.FLOAT,
            "FLOAT64": TokenType.DOUBLE,
            "INT8": TokenType.TINYINT,
            "UINT8": TokenType.UTINYINT,
            "INT16": TokenType.SMALLINT,
            "UINT16": TokenType.USMALLINT,
            "INT32": TokenType.INT,
            "UINT32": TokenType.UINT,
            "INT64": TokenType.BIGINT,
            "UINT64": TokenType.UBIGINT,
            "INT128": TokenType.INT128,
            "UINT128": TokenType.UINT128,
            "INT256": TokenType.INT256,
            "UINT256": TokenType.UINT256,
            "TUPLE": TokenType.STRUCT,
        }

    class Parser(parser.Parser):
        FUNCTIONS = {
            **parser.Parser.FUNCTIONS,  # type: ignore
            "MAP": parse_var_map,
            "MATCH": exp.RegexpLike.from_arg_list,
        }

        FUNCTION_PARSERS = {
            **parser.Parser.FUNCTION_PARSERS,
            "QUANTILE": lambda self: self._parse_quantile(),
        }

        FUNCTION_PARSERS.pop("MATCH")

        RANGE_PARSERS = {
            **parser.Parser.RANGE_PARSERS,
            TokenType.GLOBAL: lambda self, this: self._match(TokenType.IN)
            and self._parse_in(this, is_global=True),
        }

        # The PLACEHOLDER entry is popped because 1) it doesn't affect Clickhouse (it corresponds to
        # the postgres-specific JSONBContains parser) and 2) it makes parsing the ternary op simpler.
        COLUMN_OPERATORS = parser.Parser.COLUMN_OPERATORS.copy()
        COLUMN_OPERATORS.pop(TokenType.PLACEHOLDER)

        JOIN_KINDS = {*parser.Parser.JOIN_KINDS, TokenType.ANY, TokenType.ASOF}

        TABLE_ALIAS_TOKENS = {*parser.Parser.TABLE_ALIAS_TOKENS} - {
            TokenType.ANY,
            TokenType.SETTINGS,
            TokenType.FORMAT,
        }

        LOG_DEFAULTS_TO_LN = True

        QUERY_MODIFIER_PARSERS = {
            **parser.Parser.QUERY_MODIFIER_PARSERS,
            "settings": lambda self: self._parse_csv(self._parse_conjunction)
            if self._match(TokenType.SETTINGS)
            else None,
            "format": lambda self: self._parse_id_var() if self._match(TokenType.FORMAT) else None,
        }

        def _parse_expression(self, explicit_alias: bool = False) -> t.Optional[exp.Expression]:
            return self._parse_alias(self._parse_ternary(), explicit=explicit_alias)

        def _parse_ternary(self) -> t.Optional[exp.Expression]:
            this = self._parse_conjunction()

            if self._match(TokenType.PLACEHOLDER):
                return self.expression(
                    exp.If,
                    this=this,
                    true=self._parse_conjunction(),
                    false=self._match(TokenType.COLON) and self._parse_conjunction(),
                )

            return this

        def _parse_in(
            self, this: t.Optional[exp.Expression], is_global: bool = False
        ) -> exp.Expression:
            this = super()._parse_in(this)
            this.set("is_global", is_global)
            return this

        def _parse_table(
            self, schema: bool = False, alias_tokens: t.Optional[t.Collection[TokenType]] = None
        ) -> t.Optional[exp.Expression]:
            this = super()._parse_table(schema=schema, alias_tokens=alias_tokens)

            if self._match(TokenType.FINAL):
                this = self.expression(exp.Final, this=this)

            return this

        def _parse_position(self, haystack_first: bool = False) -> exp.Expression:
            return super()._parse_position(haystack_first=True)

        # https://clickhouse.com/docs/en/sql-reference/statements/select/with/
        def _parse_cte(self) -> exp.Expression:
            index = self._index
            try:
                # WITH <identifier> AS <subquery expression>
                return super()._parse_cte()
            except ParseError:
                # WITH <expression> AS <identifier>
                self._retreat(index)
                statement = self._parse_statement()

                if statement and isinstance(statement.this, exp.Alias):
                    self.raise_error("Expected CTE to have alias")

                return self.expression(exp.CTE, this=statement, alias=statement and statement.this)

        def _parse_join_side_and_kind(
            self,
        ) -> t.Tuple[t.Optional[Token], t.Optional[Token], t.Optional[Token]]:
            return (
                self._match(TokenType.GLOBAL) and self._prev,
                self._match_set(self.JOIN_SIDES) and self._prev,
                self._match_set(self.JOIN_KINDS) and self._prev,
            )

        def _parse_join(self, skip_join_token: bool = False) -> t.Optional[exp.Expression]:
            join = super()._parse_join(skip_join_token)

            if join:
                join.set("global", join.args.pop("natural", None))
            return join

        def _parse_function(
            self, functions: t.Optional[t.Dict[str, t.Callable]] = None, anonymous: bool = False
        ) -> t.Optional[exp.Expression]:
            func = super()._parse_function(functions, anonymous)

            if isinstance(func, exp.Anonymous):
                params = self._parse_func_params(func)

                if params:
                    return self.expression(
                        exp.ParameterizedAgg,
                        this=func.this,
                        expressions=func.expressions,
                        params=params,
                    )

            return func

        def _parse_func_params(
            self, this: t.Optional[exp.Func] = None
        ) -> t.Optional[t.List[t.Optional[exp.Expression]]]:
            if self._match_pair(TokenType.R_PAREN, TokenType.L_PAREN):
                return self._parse_csv(self._parse_lambda)
            if self._match(TokenType.L_PAREN):
                params = self._parse_csv(self._parse_lambda)
                self._match_r_paren(this)
                return params
            return None

        def _parse_quantile(self) -> exp.Quantile:
            this = self._parse_lambda()
            params = self._parse_func_params()
            if params:
                return self.expression(exp.Quantile, this=params[0], quantile=this)
            return self.expression(exp.Quantile, this=this, quantile=exp.Literal.number(0.5))

    class Generator(generator.Generator):
        STRUCT_DELIMITER = ("(", ")")

        TYPE_MAPPING = {
            **generator.Generator.TYPE_MAPPING,  # type: ignore
            exp.DataType.Type.NULLABLE: "Nullable",
            exp.DataType.Type.DATETIME64: "DateTime64",
            exp.DataType.Type.MAP: "Map",
            exp.DataType.Type.ARRAY: "Array",
            exp.DataType.Type.STRUCT: "Tuple",
            exp.DataType.Type.TINYINT: "Int8",
            exp.DataType.Type.UTINYINT: "UInt8",
            exp.DataType.Type.SMALLINT: "Int16",
            exp.DataType.Type.USMALLINT: "UInt16",
            exp.DataType.Type.INT: "Int32",
            exp.DataType.Type.UINT: "UInt32",
            exp.DataType.Type.BIGINT: "Int64",
            exp.DataType.Type.UBIGINT: "UInt64",
            exp.DataType.Type.INT128: "Int128",
            exp.DataType.Type.UINT128: "UInt128",
            exp.DataType.Type.INT256: "Int256",
            exp.DataType.Type.UINT256: "UInt256",
            exp.DataType.Type.FLOAT: "Float32",
            exp.DataType.Type.DOUBLE: "Float64",
        }

        TRANSFORMS = {
            **generator.Generator.TRANSFORMS,  # type: ignore
            exp.Array: inline_array_sql,
            exp.CastToStrType: rename_func("CAST"),
            exp.Final: lambda self, e: f"{self.sql(e, 'this')} FINAL",
            exp.Map: lambda self, e: _lower_func(var_map_sql(self, e)),
            exp.PartitionedByProperty: lambda self, e: f"PARTITION BY {self.sql(e, 'this')}",
            exp.Quantile: lambda self, e: self.func("quantile", e.args.get("quantile"))
            + f"({self.sql(e, 'this')})",
            exp.RegexpLike: lambda self, e: f"match({self.format_args(e.this, e.expression)})",
            exp.StrPosition: lambda self, e: f"position({self.format_args(e.this, e.args.get('substr'), e.args.get('position'))})",
            exp.VarMap: lambda self, e: _lower_func(var_map_sql(self, e)),
        }

        PROPERTIES_LOCATION = {
            **generator.Generator.PROPERTIES_LOCATION,  # type: ignore
            exp.VolatileProperty: exp.Properties.Location.UNSUPPORTED,
            exp.PartitionedByProperty: exp.Properties.Location.POST_SCHEMA,
        }

        JOIN_HINTS = False
        TABLE_HINTS = False
        EXPLICIT_UNION = True
        GROUPINGS_SEP = ""

        def cte_sql(self, expression: exp.CTE) -> str:
            if isinstance(expression.this, exp.Alias):
                return self.sql(expression, "this")

            return super().cte_sql(expression)

        def after_limit_modifiers(self, expression):
            return super().after_limit_modifiers(expression) + [
                self.seg("SETTINGS ") + self.expressions(expression, key="settings", flat=True)
                if expression.args.get("settings")
                else "",
                self.seg("FORMAT ") + self.sql(expression, "format")
                if expression.args.get("format")
                else "",
            ]

        def parameterizedagg_sql(self, expression: exp.Anonymous) -> str:
            params = self.expressions(expression, "params", flat=True)
            return self.func(expression.name, *expression.expressions) + f"({params})"

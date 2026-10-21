"""A small Postgres stand-in for the Supabase query builder used in tests."""

import re
from decimal import Decimal
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Json


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PgResult:
    def __init__(self, data):
        self.data = data


class PgClient:
    def __init__(self, connection):
        self.connection = connection

    def table(self, name):
        return PgQuery(self.connection, name)


class PgQuery:
    def __init__(self, connection, table):
        self.connection = connection
        self.table = _ident(table)
        self.op = None
        self.columns = "*"
        self.filters = []
        self.params = []
        self.payload = None
        self.ordering = None
        self.limit_n = None
        self.on_conflict = None
        self.as_single = False

    def select(self, columns="*"):
        self.op = "select"
        self.columns = columns
        return self

    def insert(self, data):
        self.op = "insert"
        self.payload = data if isinstance(data, list) else [data]
        return self

    def update(self, data):
        self.op = "update"
        self.payload = data
        return self

    def delete(self):
        self.op = "delete"
        return self

    def upsert(self, data, on_conflict=None):
        self.op = "upsert"
        self.payload = data if isinstance(data, list) else [data]
        self.on_conflict = on_conflict
        return self

    def eq(self, column, value):
        self.filters.append(f"{_ident(column)} = %s")
        self.params.append(value)
        return self

    def gte(self, column, value):
        self.filters.append(f"{_ident(column)} >= %s")
        self.params.append(value)
        return self

    def lte(self, column, value):
        self.filters.append(f"{_ident(column)} <= %s")
        self.params.append(value)
        return self

    def order(self, column, desc=False):
        self.ordering = (_ident(column), bool(desc))
        return self

    def limit(self, count):
        self.limit_n = int(count)
        return self

    def single(self):
        self.as_single = True
        return self

    def execute(self):
        if self.op == "select":
            rows = self._fetch(self._select_sql())
        elif self.op == "insert":
            rows = self._write_rows(conflict=None)
        elif self.op == "upsert":
            rows = self._write_rows(conflict=self.on_conflict)
        elif self.op == "update":
            rows = self._update()
        elif self.op == "delete":
            rows = self._fetch(self._delete_sql())
        else:
            raise RuntimeError(f"query has no operation on {self.table}")
        if self.as_single:
            return PgResult(rows[0] if rows else None)
        return PgResult(rows)

    def _select_sql(self):
        sql = f"SELECT {self._column_list()} FROM {self.table}"
        sql += self._where()
        if self.ordering:
            column, desc = self.ordering
            sql += f" ORDER BY {column} {'DESC' if desc else 'ASC'}"
        if self.limit_n is not None:
            sql += f" LIMIT {self.limit_n}"
        return sql, list(self.params)

    def _delete_sql(self):
        return f"DELETE FROM {self.table}{self._where()} RETURNING *", list(self.params)

    def _where(self):
        if not self.filters:
            return ""
        return " WHERE " + " AND ".join(self.filters)

    def _column_list(self):
        if self.columns.strip() == "*":
            return "*"
        return ", ".join(_ident(part.strip()) for part in self.columns.split(","))

    def _update(self):
        payload = self.payload or {}
        if not payload:
            return []
        assignments = []
        params = []
        for key, value in payload.items():
            assignments.append(f"{_ident(key)} = %s")
            params.append(_adapt(value))
        params.extend(self.params)
        sql = (
            f"UPDATE {self.table} SET {', '.join(assignments)}"
            f"{self._where()} RETURNING *"
        )
        return self._fetch((sql, params))

    def _write_rows(self, conflict):
        written = []
        for row in self.payload or []:
            columns = list(row.keys())
            values = [_adapt(row[column]) for column in columns]
            column_sql = ", ".join(_ident(column) for column in columns)
            placeholders = ", ".join(["%s"] * len(columns))
            sql = f"INSERT INTO {self.table} ({column_sql}) VALUES ({placeholders})"
            if conflict:
                keys = [_ident(part.strip()) for part in conflict.split(",")]
                updates = ", ".join(
                    f"{_ident(column)} = EXCLUDED.{_ident(column)}"
                    for column in columns
                    if _ident(column) not in keys
                )
                sql += f" ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}"
            sql += " RETURNING *"
            written.extend(self._fetch((sql, values)))
        return written

    def _fetch(self, statement):
        sql, params = statement
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql, params)
            if cursor.description is None:
                return []
            return [_plain(row) for row in cursor.fetchall()]


def _ident(name):
    if not _IDENT.match(name):
        raise ValueError(f"unsafe sql identifier {name!r}")
    return name


def _adapt(value):
    if isinstance(value, (dict, list)):
        return Json(value)
    return value


def _plain(row):
    plain = {}
    for key, value in row.items():
        if isinstance(value, UUID):
            plain[key] = str(value)
        elif isinstance(value, Decimal):
            plain[key] = float(value)
        elif hasattr(value, "isoformat") and not isinstance(value, str):
            plain[key] = value.isoformat()
        else:
            plain[key] = value
    return plain

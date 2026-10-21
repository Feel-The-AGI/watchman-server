"""Run a SQL file that contains functions, DO blocks, and comments."""

from pathlib import Path


def split_sql(script: str) -> list[str]:
    """Split a script on semicolons that are not inside quotes or dollar bodies."""
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    length = len(script)
    dollar_tag = None
    in_single = False
    in_line_comment = False
    in_block_comment = False

    while i < length:
        char = script[i]
        nxt = script[i + 1] if i + 1 < length else ""

        if in_line_comment:
            buf.append(char)
            if char == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            buf.append(char)
            if char == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue

        if dollar_tag is not None:
            if script.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(char)
            i += 1
            continue

        if in_single:
            buf.append(char)
            if char == "'" and nxt == "'":
                buf.append(nxt)
                i += 2
                continue
            if char == "'":
                in_single = False
            i += 1
            continue

        if char == "-" and nxt == "-":
            in_line_comment = True
            buf.append(char)
            i += 1
            continue

        if char == "/" and nxt == "*":
            in_block_comment = True
            buf.append(char)
            i += 1
            continue

        if char == "'":
            in_single = True
            buf.append(char)
            i += 1
            continue

        if char == "$":
            end = i + 1
            while end < length and (script[end].isalnum() or script[end] == "_"):
                end += 1
            if end < length and script[end] == "$":
                dollar_tag = script[i:end + 1]
                buf.append(dollar_tag)
                i = end + 1
                continue

        if char == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue

        buf.append(char)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def execute_sql_file(connection, path: Path) -> None:
    """Apply one migration file. Statements run outside an implicit transaction."""
    sql = path.read_text(encoding="utf-8")
    if not connection.autocommit:
        connection.rollback()
        connection.autocommit = True
    for statement in split_sql(sql):
        connection.execute(statement)


def migration_paths(root: Path) -> list[Path]:
    return sorted(path for path in root.glob("*.sql") if path.is_file())

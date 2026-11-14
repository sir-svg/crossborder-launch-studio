import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


class Connection:
    def __init__(self, connection, postgres):
        self.connection = connection
        self.postgres = postgres

    def execute(self, statement, args=()):
        if self.postgres:
            statement = statement.replace("?", "%s")
        return self.connection.execute(statement, args)

    def executemany(self, statement, values):
        if self.postgres:
            with self.connection.cursor() as cursor:
                cursor.executemany(statement.replace("?", "%s"), values)
        else:
            self.connection.executemany(statement, values)


class Database:
    def __init__(self, path, url=""):
        self.path = Path(path)
        self.pool = None
        self.integrity_errors = (sqlite3.IntegrityError,)
        if url:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            self.integrity_errors = (psycopg.IntegrityError,)
            self.pool = ConnectionPool(
                url,
                min_size=1,
                max_size=8,
                timeout=5,
                open=False,
                kwargs={"row_factory": dict_row, "connect_timeout": 5},
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def postgres(self):
        return self.pool is not None

    def open(self):
        if self.pool is not None:
            self.pool.open(wait=True, timeout=20)
        else:
            with self.connect() as db:
                db.execute("PRAGMA journal_mode=WAL")

    def close(self):
        if self.pool is not None:
            self.pool.close()

    @contextmanager
    def connect(self, write=False):
        if self.pool is not None:
            with self.pool.connection() as connection:
                yield Connection(connection, True)
        else:
            with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield Connection(connection, False)

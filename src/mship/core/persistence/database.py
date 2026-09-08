from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from sqlite3 import Connection as SQLiteConnection

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError

DB_FILENAME = "mothership.db"
BUSY_TIMEOUT_MS = 5_000
INITIALIZE_LOCK_FILENAME = "mothership.db.initialize.lock"


class DatabaseRevisionError(RuntimeError):
    """The workspace database revision cannot be used by this binary."""


class DatabaseBusyError(RuntimeError):
    """The workspace database remained locked past the bounded busy timeout."""


@contextmanager
def _initialization_lock(state_dir: Path) -> Iterator[None]:
    """Serialize first-time Alembic setup across workspace processes."""
    lock_path = state_dir / INITIALIZE_LOCK_FILENAME
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def install_sqlite_policy(engine: Engine) -> None:
    """Apply Mothership's required SQLite policy to every engine connection."""

    @event.listens_for(engine, "connect")
    def configure_sqlite(
        dbapi_connection: SQLiteConnection,
        _connection_record: object,
    ) -> None:
        previous_autocommit = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        try:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            finally:
                cursor.close()
        finally:
            dbapi_connection.autocommit = previous_autocommit


def make_alembic_config(database_path: Path) -> Config:
    """Build an Alembic config whose scripts come from this installed package."""
    script_location = resources.files("mship.core.persistence.alembic")
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+pysqlite:///{database_path.absolute()}",
    )
    return config


class WorkspaceDatabase:
    """Own the SQLite engine and its transaction policy for one workspace."""

    def __init__(
        self,
        state_dir: Path,
        *,
        database_path: Path | None = None,
    ) -> None:
        self._state_dir = state_dir
        self._database_path = database_path
        self._engine = self._create_engine()

    @property
    def path(self) -> Path:
        return self._database_path or self._state_dir / DB_FILENAME

    def dispose(self) -> None:
        self._engine.dispose()

    def checkpoint(self) -> None:
        """Move committed WAL content into the database before file activation."""
        with self.connect() as connection:
            dbapi_connection = connection.connection.driver_connection
            previous_autocommit = dbapi_connection.autocommit
            dbapi_connection.autocommit = True
            try:
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    cursor.close()
            finally:
                dbapi_connection.autocommit = previous_autocommit

    def _create_engine(self) -> Engine:
        engine = create_engine(
            f"sqlite+pysqlite:///{self.path}",
            connect_args={
                "autocommit": False,
                "timeout": BUSY_TIMEOUT_MS / 1_000,
            },
        )
        install_sqlite_policy(engine)
        return engine

    def initialize(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        current = self.current_revision()
        head = self.head_revision()
        if current is not None and current != head:
            raise DatabaseRevisionError(
                f"workspace database {self.path} is at revision {current!r}; "
                f"this binary requires {head!r}. Stop active writers and run "
                "mship state migrate with a compatible binary"
            )
        if current == head:
            return
        with _initialization_lock(self._state_dir):
            current = self.current_revision()
            if current is not None and current != head:
                raise DatabaseRevisionError(
                    f"workspace database {self.path} is at revision {current!r}; "
                    f"this binary requires {head!r}. Stop active writers and run "
                    "mship state migrate with a compatible binary"
                )
            if current == head:
                return
            with self.connect() as connection:
                config = make_alembic_config(self.path)
                config.attributes["connection"] = connection
                command.upgrade(config, "head")

    def current_revision(self) -> str | None:
        if not self.path.exists():
            return None
        with self.connect() as connection:
            if not inspect(connection).has_table("alembic_version"):
                return None
            return connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()

    def head_revision(self) -> str:
        head = ScriptDirectory.from_config(make_alembic_config(self.path)).get_current_head()
        if head is None:
            raise DatabaseRevisionError("the packaged Alembic history has no head revision")
        return head

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        with self._engine.connect() as connection:
            yield connection

    @contextmanager
    def read(self) -> Iterator[Connection]:
        with self.connect() as connection:
            yield connection

    @contextmanager
    def write(self, *, immediate: bool = False) -> Iterator[Connection]:
        try:
            with self.connect() as connection:
                if immediate:
                    # Python 3.14's PEP 249 mode keeps a transaction open while
                    # autocommit is False. End that empty transaction with SQL
                    # (Connection.commit would immediately open another one)
                    # before acquiring the reserved writer lock.
                    connection.exec_driver_sql("COMMIT")
                    try:
                        connection.exec_driver_sql("BEGIN IMMEDIATE")
                    except OperationalError as error:
                        # The raw COMMIT above ends SQLite's transaction while
                        # SQLAlchemy still tracks its autobegin transaction. If
                        # BEGIN fails (normally at the busy deadline), discard
                        # the divergent connection so context cleanup cannot
                        # mask the real error by rolling back no transaction.
                        connection.invalidate(error)
                        raise
                    try:
                        yield connection
                    except BaseException:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
                else:
                    with connection.begin():
                        yield connection
        except OperationalError as error:
            if "database is locked" not in str(error).lower():
                raise
            raise DatabaseBusyError(
                f"workspace database {self.path} remained locked for "
                f"{BUSY_TIMEOUT_MS}ms; retry the operation"
            ) from error

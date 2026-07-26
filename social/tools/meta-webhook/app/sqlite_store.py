import sqlite3
from pathlib import Path


class SQLiteStore:
    """Small base class for SQLite-backed stores used by the app.

    Keeps path normalization, parent-directory creation and connection opening in
    one place while each concrete store owns its schema and queries.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

    def _ensure_schema(self) -> None:
        raise NotImplementedError

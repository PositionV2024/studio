from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(slots=True)
class ProjectRecord:
    id: str
    name: str
    path: Path
    entry_point: str
    preview_port: int
    last_opened: str


class ProjectLibrary:
    """SQLite-backed project library used by the desktop editor."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    entry_point TEXT NOT NULL,
                    preview_port INTEGER NOT NULL,
                    last_opened TEXT NOT NULL
                )
                """
            )

    def list_projects(self) -> list[ProjectRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM projects ORDER BY last_opened DESC, name COLLATE NOCASE"
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def add_or_update(
        self,
        *,
        name: str,
        path: Path,
        entry_point: str,
        preview_port: int,
    ) -> ProjectRecord:
        canonical = path.expanduser().resolve()
        timestamp = datetime.now(timezone.utc).isoformat()

        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM projects WHERE path = ?",
                (str(canonical),),
            ).fetchone()
            project_id = existing["id"] if existing else uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO projects (
                    id, name, path, entry_point, preview_port, last_opened
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    name = excluded.name,
                    entry_point = excluded.entry_point,
                    preview_port = excluded.preview_port,
                    last_opened = excluded.last_opened
                """,
                (
                    project_id,
                    name,
                    str(canonical),
                    entry_point,
                    int(preview_port),
                    timestamp,
                ),
            )

        return ProjectRecord(
            id=project_id,
            name=name,
            path=canonical,
            entry_point=entry_point,
            preview_port=int(preview_port),
            last_opened=timestamp,
        )

    def touch(self, project_id: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE projects SET last_opened = ? WHERE id = ?",
                (timestamp, project_id),
            )

    def remove(self, project_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def get(self, project_id: str) -> ProjectRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        return self._record_from_row(row) if row else None

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            id=str(row["id"]),
            name=str(row["name"]),
            path=Path(str(row["path"])),
            entry_point=str(row["entry_point"]),
            preview_port=int(row["preview_port"]),
            last_opened=str(row["last_opened"]),
        )

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .models.api.catalog_api_teach_lesson_list_for_teach import (
    TeachLessonListResponse,
)
from .models.api.jw_ws_schedule_table_datum import (
    JwWsScheduleTableDatumResponse,
)
from .sqlite_store import GUESSES_FILENAME, SCHEMA_VERSION


class SQLiteGuessStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()

        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE teacher_section_guesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                semester_id TEXT NOT NULL,
                lesson_id INTEGER NOT NULL,
                lesson_code TEXT,
                teacher_name TEXT NOT NULL,
                teacher_name_en TEXT,
                departmentCode TEXT,
                teacherId INTEGER,
                personId INTEGER,
                match_source TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL
            );

            CREATE INDEX teacher_section_guesses_lesson_idx
                ON teacher_section_guesses(semester_id, lesson_id);
            CREATE INDEX teacher_section_guesses_teacher_idx
                ON teacher_section_guesses(teacher_name, departmentCode);
            """
        )
        self.conn.executemany(
            "INSERT INTO metadata(key, value) VALUES(?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("generated_at", datetime.now(UTC).isoformat()),
                ("filename", GUESSES_FILENAME),
            ],
        )

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def add_teacher_section_guesses(
        self,
        *,
        semester_id: str,
        catalog_lessons: TeachLessonListResponse,
        jw_schedules: Sequence[JwWsScheduleTableDatumResponse] | None,
    ) -> None:
        schedule_people: dict[tuple[int, str], tuple[int | None, int | None]] = {}
        for jw_schedule in jw_schedules or []:
            if not jw_schedule.result:
                continue
            for schedule in jw_schedule.result.scheduleList or []:
                if schedule.lessonId is None or not schedule.personName:
                    continue
                key = (schedule.lessonId, schedule.personName)
                if key not in schedule_people:
                    schedule_people[key] = (schedule.teacherId, schedule.personId)

        rows = []
        for lesson in catalog_lessons.root or []:
            if lesson is None or lesson.id is None:
                continue
            for assignment in lesson.teacherAssignmentList or []:
                if assignment is None or not assignment.cn:
                    continue
                teacher_id, person_id = schedule_people.get(
                    (lesson.id, assignment.cn), (None, None)
                )
                if teacher_id is not None or person_id is not None:
                    match_source = "jw_schedule_personName"
                    confidence = 0.8
                    reason = (
                        "Matched catalog teacher assignment to JW schedule row by "
                        "same lesson id and teacher name."
                    )
                else:
                    match_source = "catalog_lesson_teacherAssignmentList"
                    confidence = 0.6
                    reason = (
                        "Catalog teacher assignment is directly attached to the "
                        "lesson, but no JW teacher/person id was available."
                    )

                rows.append(
                    (
                        semester_id,
                        lesson.id,
                        lesson.code,
                        assignment.cn,
                        assignment.en,
                        assignment.departmentCode,
                        teacher_id,
                        person_id,
                        match_source,
                        confidence,
                        reason,
                    )
                )

        self.conn.executemany(
            """
            INSERT INTO teacher_section_guesses(
                semester_id,
                lesson_id,
                lesson_code,
                teacher_name,
                teacher_name_en,
                departmentCode,
                teacherId,
                personId,
                match_source,
                confidence,
                reason
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

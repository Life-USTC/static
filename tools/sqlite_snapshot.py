from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from src.models import Course, Semester

SCHEMA_VERSION = 2
SNAPSHOT_FILENAME = "life-ustc-static.sqlite"


def _read_json(path: Path):
    return json.loads(path.read_text())


def _load_semesters(build_dir: Path) -> list[Semester]:
    semester_path = build_dir / "curriculum" / "semesters.json"
    semesters = _read_json(semester_path)
    return [Semester.model_validate(item) for item in semesters]


def _iter_semester_courses(build_dir: Path):
    curriculum_root = build_dir / "curriculum"
    api_root = build_dir / "api" / "course"

    for semester_dir in sorted(curriculum_root.iterdir()):
        if not semester_dir.is_dir():
            continue
        if not semester_dir.name.isdigit():
            continue

        course_list_path = semester_dir / "courses.json"
        if not course_list_path.exists():
            continue

        base_courses = _read_json(course_list_path)
        for course_payload in base_courses:
            base_course = Course.model_validate(course_payload)
            detailed_path = api_root / str(base_course.id)
            detailed_course = (
                Course.model_validate(_read_json(detailed_path))
                if detailed_path.exists()
                else base_course
            )
            yield semester_dir.name, detailed_course


def _load_bus_payload(build_dir: Path) -> dict:
    bus_path = build_dir / "bus_data_v3.json"
    return _read_json(bus_path)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE semesters (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            start_date INTEGER NOT NULL,
            end_date INTEGER NOT NULL
        );

        CREATE TABLE courses (
            id INTEGER PRIMARY KEY,
            semester_id TEXT NOT NULL,
            name TEXT NOT NULL,
            course_code TEXT NOT NULL,
            lesson_code TEXT NOT NULL,
            teacher_name TEXT NOT NULL,
            date_time_place_person_text TEXT,
            course_type TEXT,
            course_gradation TEXT NOT NULL,
            course_category TEXT NOT NULL,
            education_type TEXT NOT NULL,
            class_type TEXT NOT NULL,
            open_department TEXT NOT NULL,
            description TEXT NOT NULL,
            credit REAL NOT NULL,
            FOREIGN KEY (semester_id) REFERENCES semesters(id)
        );

        CREATE INDEX courses_semester_idx ON courses(semester_id);
        CREATE INDEX courses_code_idx ON courses(course_code);

        CREATE TABLE course_lectures (
            course_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            start_date INTEGER NOT NULL,
            end_date INTEGER NOT NULL,
            name TEXT NOT NULL,
            location TEXT NOT NULL,
            teacher_name TEXT NOT NULL,
            periods REAL NOT NULL,
            start_index INTEGER NOT NULL,
            end_index INTEGER NOT NULL,
            start_hhmm INTEGER NOT NULL,
            end_hhmm INTEGER NOT NULL,
            PRIMARY KEY (course_id, position),
            FOREIGN KEY (course_id) REFERENCES courses(id)
        );

        CREATE INDEX course_lectures_course_idx ON course_lectures(course_id);

        CREATE TABLE course_exams (
            course_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            start_date INTEGER NOT NULL,
            end_date INTEGER NOT NULL,
            name TEXT NOT NULL,
            location TEXT NOT NULL,
            exam_type TEXT NOT NULL,
            start_hhmm INTEGER NOT NULL,
            end_hhmm INTEGER NOT NULL,
            exam_mode TEXT,
            PRIMARY KEY (course_id, position),
            FOREIGN KEY (course_id) REFERENCES courses(id)
        );

        CREATE INDEX course_exams_course_idx ON course_exams(course_id);

        CREATE TABLE bus_notice (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            message TEXT,
            url TEXT
        );

        CREATE TABLE bus_campuses (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL
        );

        CREATE TABLE bus_routes (
            id INTEGER PRIMARY KEY
        );

        CREATE TABLE bus_route_stops (
            route_id INTEGER NOT NULL,
            stop_order INTEGER NOT NULL,
            campus_id INTEGER NOT NULL,
            PRIMARY KEY (route_id, stop_order),
            FOREIGN KEY (route_id) REFERENCES bus_routes(id),
            FOREIGN KEY (campus_id) REFERENCES bus_campuses(id)
        );

        CREATE TABLE bus_trips (
            day_type TEXT NOT NULL,
            schedule_id INTEGER NOT NULL,
            route_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (day_type, schedule_id, position),
            FOREIGN KEY (route_id) REFERENCES bus_routes(id)
        );

        CREATE TABLE bus_trip_stop_times (
            day_type TEXT NOT NULL,
            schedule_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            stop_order INTEGER NOT NULL,
            campus_id INTEGER NOT NULL,
            departure_time TEXT,
            PRIMARY KEY (day_type, schedule_id, position, stop_order),
            FOREIGN KEY (campus_id) REFERENCES bus_campuses(id)
        );

        CREATE INDEX bus_trips_route_idx ON bus_trips(route_id, day_type);
        """
    )


def export_sqlite_snapshot(build_dir: Path, output_path: Path | None = None) -> Path:
    if output_path is None:
        output_path = build_dir / SNAPSHOT_FILENAME

    semesters = _load_semesters(build_dir)
    courses = list(_iter_semester_courses(build_dir))
    bus_payload = _load_bus_payload(build_dir)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    generated_at = datetime.now(UTC).isoformat()
    repo_sha = os.getenv("GITHUB_SHA", "").strip() or None

    with sqlite3.connect(output_path) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = FULL")
        _create_schema(conn)

        metadata_items = {
            "schema_version": str(SCHEMA_VERSION),
            "generated_at": generated_at,
            "semester_count": str(len(semesters)),
            "course_count": str(len(courses)),
        }
        if repo_sha is not None:
            metadata_items["github_sha"] = repo_sha

        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES(?, ?)",
            metadata_items.items(),
        )

        conn.executemany(
            "INSERT INTO semesters(id, name, start_date, end_date) VALUES(?, ?, ?, ?)",
            (
                (semester.id, semester.name, semester.startDate, semester.endDate)
                for semester in semesters
            ),
        )

        conn.executemany(
            """
            INSERT INTO courses(
                id,
                semester_id,
                name,
                course_code,
                lesson_code,
                teacher_name,
                date_time_place_person_text,
                course_type,
                course_gradation,
                course_category,
                education_type,
                class_type,
                open_department,
                description,
                credit
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    course.id,
                    semester_id,
                    course.name,
                    course.courseCode,
                    course.lessonCode,
                    course.teacherName,
                    course.dateTimePlacePersonText,
                    course.courseType,
                    course.courseGradation,
                    course.courseCategory,
                    course.educationType,
                    course.classType,
                    course.openDepartment,
                    course.description,
                    course.credit,
                )
                for semester_id, course in courses
            ),
        )

        conn.executemany(
            """
            INSERT INTO course_lectures(
                course_id,
                position,
                start_date,
                end_date,
                name,
                location,
                teacher_name,
                periods,
                start_index,
                end_index,
                start_hhmm,
                end_hhmm
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    course.id,
                    position,
                    lecture.startDate,
                    lecture.endDate,
                    lecture.name,
                    lecture.location,
                    lecture.teacherName,
                    lecture.periods,
                    lecture.startIndex,
                    lecture.endIndex,
                    lecture.startHHMM,
                    lecture.endHHMM,
                )
                for _, course in courses
                for position, lecture in enumerate(course.lectures)
            ),
        )

        conn.executemany(
            """
            INSERT INTO course_exams(
                course_id,
                position,
                start_date,
                end_date,
                name,
                location,
                exam_type,
                start_hhmm,
                end_hhmm,
                exam_mode
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    course.id,
                    position,
                    exam.startDate,
                    exam.endDate,
                    exam.name,
                    exam.location,
                    exam.examType,
                    exam.startHHMM,
                    exam.endHHMM,
                    exam.examMode,
                )
                for _, course in courses
                for position, exam in enumerate(course.exams)
            ),
        )

        conn.execute(
            "INSERT INTO bus_notice(id, message, url) VALUES(1, ?, ?)",
            (
                bus_payload.get("message", {}).get("message"),
                bus_payload.get("message", {}).get("url"),
            ),
        )

        conn.executemany(
            """
            INSERT INTO bus_campuses(id, name, latitude, longitude)
            VALUES(?, ?, ?, ?)
            """,
            (
                (
                    campus["id"],
                    campus["name"],
                    campus["latitude"],
                    campus["longitude"],
                )
                for campus in bus_payload["campuses"]
            ),
        )

        conn.executemany(
            "INSERT INTO bus_routes(id) VALUES(?)",
            ((route["id"],) for route in bus_payload["routes"]),
        )

        conn.executemany(
            """
            INSERT INTO bus_route_stops(route_id, stop_order, campus_id)
            VALUES(?, ?, ?)
            """,
            (
                (route["id"], stop_order, campus["id"])
                for route in bus_payload["routes"]
                for stop_order, campus in enumerate(route["campuses"])
            ),
        )

        for day_type, key in (
            ("weekday", "weekday_routes"),
            ("weekend", "weekend_routes"),
        ):
            conn.executemany(
                """
                INSERT INTO bus_trips(day_type, schedule_id, route_id, position)
                VALUES(?, ?, ?, ?)
                """,
                (
                    (
                        day_type,
                        schedule["id"],
                        schedule["route"]["id"],
                        position,
                    )
                    for schedule in bus_payload[key]
                    for position, _ in enumerate(schedule["time"])
                ),
            )

            conn.executemany(
                """
                INSERT INTO bus_trip_stop_times(
                    day_type,
                    schedule_id,
                    position,
                    stop_order,
                    campus_id,
                    departure_time
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        day_type,
                        schedule["id"],
                        position,
                        stop_order,
                        schedule["route"]["campuses"][stop_order]["id"],
                        departure_time,
                    )
                    for schedule in bus_payload[key]
                    for position, trip in enumerate(schedule["time"])
                    for stop_order, departure_time in enumerate(trip)
                ),
            )

    return output_path

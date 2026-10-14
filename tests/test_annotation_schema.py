"""Exercise the annotation migration against an isolated PostgreSQL 14 cluster.

These tests never use application settings, environment DSNs, or a running
database. PostgreSQL's binaries and the Python test dependencies are the only
external prerequisites.
"""

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations/001_aerial_annotations.sql"
POSTGRES_BIN = Path("/usr/lib/postgresql/14/bin")
LABELS = [
    {
        "field": "road.surface.material",
        "state": "observed",
        "values": ["沥青"],
        "scope": "frame",
    }
]


def test_annotation_migration_is_available():
    assert MIGRATION.is_file(), f"Missing annotation migration: {MIGRATION}"


def run_postgres_binary(name, *arguments, check=True):
    return subprocess.run(
        [str(POSTGRES_BIN / name), *map(str, arguments)],
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture(scope="module")
def database():
    """Own a temporary server, including cleanup when setup or a test fails."""
    assert MIGRATION.is_file(), f"Missing annotation migration: {MIGRATION}"
    for name in ("initdb", "pg_ctl"):
        if not (POSTGRES_BIN / name).is_file():
            pytest.skip(f"PostgreSQL 14 test prerequisite missing: {POSTGRES_BIN / name}")

    with tempfile.TemporaryDirectory(prefix="annotation-pg-") as temporary:
        temporary = Path(temporary)
        data = temporary / "data"
        socket = temporary / "socket"
        socket.mkdir()
        connection_options = {
            "host": str(socket),
            "port": 55432,
            "user": "annotation_test_superuser",
            "dbname": "aerial_db",
            "connect_timeout": 5,
        }
        try:
            run_postgres_binary(
                "initdb", "-D", data, "-U", connection_options["user"],
                "--auth-local=trust", "--auth-host=reject", "--encoding=UTF8",
                "--no-locale",
            )
            run_postgres_binary(
                "pg_ctl", "-D", data, "-l", temporary / "postgres.log",
                "-o", f"-k {socket} -p 55432 -c listen_addresses=''", "-w", "start",
            )
            with psycopg.connect(
                **{**connection_options, "dbname": "postgres"}, autocommit=True
            ) as administrator:
                administrator.execute("CREATE ROLE ad4che NOLOGIN")
                administrator.execute("CREATE ROLE reader NOLOGIN")
                administrator.execute("CREATE DATABASE aerial_db OWNER ad4che")

            with psycopg.connect(**connection_options, autocommit=True) as setup:
                setup.execute("CREATE SCHEMA aerial AUTHORIZATION ad4che")
                setup.execute("SET ROLE ad4che")
                setup.execute("GRANT USAGE ON SCHEMA aerial TO reader")
                # Existing owner defaults must not accidentally grant writes on
                # the annotation tables when this migration creates them.
                setup.execute("""
                    ALTER DEFAULT PRIVILEGES FOR ROLE ad4che IN SCHEMA aerial
                    GRANT INSERT, UPDATE, DELETE ON TABLES TO reader
                """)
                setup.execute("""
                    CREATE TABLE aerial.videos (
                        sha256 BYTEA PRIMARY KEY,
                        duration_seconds NUMERIC,
                        deleted_at TIMESTAMPTZ
                    )
                """)
                for migration in sorted(MIGRATION.parent.glob('*.sql')):
                    setup.execute(migration.read_text(encoding="utf-8"))
            yield connection_options
        finally:
            if (data / "PG_VERSION").exists():
                run_postgres_binary("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop", check=False)


@pytest.fixture
def connection(database):
    with psycopg.connect(**database, row_factory=dict_row) as connection:
        connection.execute("SET ROLE ad4che")
        try:
            yield connection
        finally:
            connection.rollback()


def insert_video(connection, sha256=None):
    sha256 = sha256 if sha256 is not None else uuid4().bytes * 2
    connection.execute(
        "INSERT INTO aerial.videos (sha256, duration_seconds) VALUES (%s, 10)",
        (sha256,),
    )
    return sha256


def insert_batch(connection, **values):
    values = {"source_directory": f"/annotation-test/{uuid4()}", **values}
    columns = sql.SQL(", ").join(map(sql.Identifier, values))
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in values)
    return connection.execute(
        sql.SQL("INSERT INTO aerial.annotation_batches ({}) VALUES ({}) RETURNING *").format(
            columns, placeholders
        ),
        list(values.values()),
    ).fetchone()


def update_batch(connection, batch_id, **values):
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = %s").format(sql.Identifier(column)) for column in values
    )
    return connection.execute(
        sql.SQL("UPDATE aerial.annotation_batches SET {} WHERE batch_id = %s RETURNING *").format(assignments),
        [*values.values(), batch_id],
    ).fetchone()


def add_membership(connection, batch_id, sha256):
    connection.execute(
        "INSERT INTO aerial.annotation_batch_videos (batch_id, video_sha256) VALUES (%s, %s)",
        (batch_id, sha256),
    )


def ready_batch(connection, **overrides):
    sha256 = insert_video(connection)
    batch = insert_batch(
        connection,
        **{
            "representative_video_sha256": sha256,
            "frame_timestamp_ms": 0,
            "frame_path": "/frames/representative.jpg",
            "labels": Jsonb(LABELS),
            "validation_passed": True,
            **overrides,
        },
    )
    add_membership(connection, batch["batch_id"], sha256)
    return batch


def approve_batch(connection, batch_id, **overrides):
    return update_batch(
        connection, batch_id,
        **{
            "review_status": "approved",
            "reviewer_name": "测试审核员",
            "reviewed_at": datetime.now(timezone.utc),
            **overrides,
        },
    )


def test_migration_adds_only_two_tables_owned_by_existing_owner(connection):
    tables = connection.execute("""
        SELECT n.nspname AS schema_name, c.relname, r.rolname AS owner
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_roles AS r ON r.oid = c.relowner
        WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
          AND c.relkind IN ('r', 'p')
    """).fetchall()
    assert {f"{row['schema_name']}.{row['relname']}": row["owner"] for row in tables} == {
        "aerial.videos": "ad4che",
        "aerial.annotation_batches": "ad4che",
        "aerial.annotation_batch_videos": "ad4che",
    }


def test_new_batch_defaults_to_pending_without_evidence(connection):
    batch = insert_batch(connection)
    assert isinstance(batch["batch_id"], UUID)
    assert batch["review_status"] == "pending"
    assert batch["validation_passed"] is False
    assert batch["validation_errors"] == []
    assert batch["labels"] == []
    assert batch["frame_roi"] == {}
    assert batch["supplemental_frames"] == []
    assert batch["schema_version"] == "aerial_annotation_v1"
    assert batch["label_revision"] == 1
    for field in (
        "location_name", "representative_video_sha256", "frame_timestamp_ms",
        "frame_path", "selection_reason", "raw_model_response", "model_name",
        "prompt_version", "reviewer_name", "reviewed_at", "review_note",
    ):
        assert batch[field] is None, field
    assert batch["created_at"].tzinfo is not None
    assert batch["updated_at"].tzinfo is not None


def test_source_directory_identifies_one_batch(connection):
    insert_batch(connection, source_directory="/source/site-a")
    with pytest.raises(psycopg.errors.UniqueViolation), connection.transaction():
        insert_batch(connection, source_directory="/source/site-a")


@pytest.mark.parametrize("field", ["source_directory", "schema_version", "validation_passed"])
def test_required_batch_values_reject_sql_null(connection, field):
    with pytest.raises(psycopg.errors.NotNullViolation), connection.transaction():
        insert_batch(connection, **{field: None})


def test_membership_requires_an_existing_video(connection):
    batch = insert_batch(connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), connection.transaction():
        add_membership(connection, batch["batch_id"], b"x" * 32)
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_membership_requires_an_existing_batch(connection):
    sha256 = insert_video(connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), connection.transaction():
        add_membership(connection, uuid4(), sha256)
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_video_belongs_to_only_one_batch(connection):
    sha256 = insert_video(connection)
    first, second = insert_batch(connection), insert_batch(connection)
    add_membership(connection, first["batch_id"], sha256)
    with pytest.raises(psycopg.errors.UniqueViolation), connection.transaction():
        add_membership(connection, second["batch_id"], sha256)


def test_membership_cannot_be_duplicated(connection):
    sha256 = insert_video(connection)
    batch = insert_batch(connection)
    add_membership(connection, batch["batch_id"], sha256)
    with pytest.raises(psycopg.errors.UniqueViolation), connection.transaction():
        add_membership(connection, batch["batch_id"], sha256)


def test_deleting_batch_cascades_membership_and_preserves_video(connection):
    batch = ready_batch(connection)
    connection.execute("DELETE FROM aerial.annotation_batches WHERE batch_id = %s", (batch["batch_id"],))
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert connection.execute(
        "SELECT count(*) AS count FROM aerial.annotation_batch_videos WHERE batch_id = %s",
        (batch["batch_id"],),
    ).fetchone()["count"] == 0
    assert connection.execute(
        "SELECT sha256 FROM aerial.videos WHERE sha256 = %s", (batch["representative_video_sha256"],)
    ).fetchone() is not None


def test_deleting_a_member_video_is_restricted(connection):
    batch = ready_batch(connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), connection.transaction():
        connection.execute(
            "DELETE FROM aerial.videos WHERE sha256 = %s", (batch["representative_video_sha256"],)
        )


@pytest.mark.parametrize("length", [0, 31, 33])
@pytest.mark.parametrize("target", ["representative", "membership"])
def test_annotation_video_hashes_require_32_bytes(connection, length, target):
    sha256 = insert_video(connection, b"x" * length)
    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        if target == "representative":
            insert_batch(
                connection, representative_video_sha256=sha256,
                frame_timestamp_ms=0, frame_path="/frames/a.jpg",
            )
        else:
            batch = insert_batch(connection)
            add_membership(connection, batch["batch_id"], sha256)


def test_representative_membership_can_be_created_after_batch(connection):
    batch = ready_batch(connection)
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert connection.execute(
        "SELECT video_sha256 FROM aerial.annotation_batch_videos WHERE batch_id = %s",
        (batch["batch_id"],),
    ).fetchone()["video_sha256"] == batch["representative_video_sha256"]


def test_representative_video_must_belong_to_the_same_batch(connection):
    sha256 = insert_video(connection)
    other = insert_batch(connection)
    add_membership(connection, other["batch_id"], sha256)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), connection.transaction():
        insert_batch(
            connection, representative_video_sha256=sha256,
            frame_timestamp_ms=0, frame_path="/frames/a.jpg",
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_representative_membership_cannot_be_removed(connection):
    batch = ready_batch(connection)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), connection.transaction():
        connection.execute(
            "DELETE FROM aerial.annotation_batch_videos WHERE batch_id = %s", (batch["batch_id"],)
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.parametrize("present", [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, 0, 1), (0, 1, 1)])
def test_representative_source_timestamp_and_path_are_all_present_or_all_null(connection, present):
    sha256 = insert_video(connection)
    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        insert_batch(
            connection,
            representative_video_sha256=sha256 if present[0] else None,
            frame_timestamp_ms=0 if present[1] else None,
            frame_path="/frames/a.jpg" if present[2] else None,
        )


def test_frame_timestamp_cannot_be_negative(connection):
    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        ready_batch(connection, frame_timestamp_ms=-1)


@pytest.mark.parametrize("field, value", [
    ("frame_roi", []), ("frame_roi", None),
    ("supplemental_frames", {}), ("supplemental_frames", None),
    ("labels", {}), ("labels", None),
    ("validation_errors", {}), ("validation_errors", None),
])
def test_json_evidence_has_the_required_container_shape(connection, field, value):
    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        insert_batch(connection, **{field: Jsonb(value)})


@pytest.mark.parametrize("revision", [0, -1])
def test_label_revision_must_be_positive(connection, revision):
    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        insert_batch(connection, label_revision=revision)


@pytest.mark.parametrize("status", ["draft", "APPROVED", ""])
def test_review_status_accepts_only_supported_states(connection, status):
    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        insert_batch(connection, review_status=status)


def test_batch_can_be_approved_after_evidence_and_validation_are_ready(connection):
    batch = ready_batch(connection)
    approved = approve_batch(connection, batch["batch_id"])
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert approved["review_status"] == "approved"
    assert approved["validation_passed"] is True
    assert approved["reviewer_name"] == "测试审核员"
    assert approved["reviewed_at"] is not None
    assert approved["label_revision"] == 1


@pytest.mark.parametrize("missing", [
    "frame", "validation_passed", "validation_errors", "labels",
    "reviewer_name", "reviewed_at",
])
def test_approval_requires_complete_validated_evidence_and_reviewer(connection, missing):
    if missing == "frame":
        batch = insert_batch(connection, labels=Jsonb(LABELS), validation_passed=True)
    else:
        batch_overrides = {
            "validation_passed": {"validation_passed": False},
            "validation_errors": {"validation_errors": Jsonb(["uncertain road surface"])},
            "labels": {"labels": Jsonb([])},
        }.get(missing, {})
        batch = ready_batch(connection, **batch_overrides)
    review_overrides = {missing: None} if missing in ("reviewer_name", "reviewed_at") else {}
    with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
        approve_batch(connection, batch["batch_id"], **review_overrides)


@pytest.mark.parametrize("field", [
    "source_directory", "location_name", "representative_video_sha256",
    "frame_timestamp_ms", "frame_path", "frame_roi", "supplemental_frames",
    "labels", "schema_version",
])
def test_changing_reviewed_evidence_invalidates_approval_and_advances_revision(connection, field):
    batch = ready_batch(connection)
    alternative_sha256 = insert_video(connection)
    add_membership(connection, batch["batch_id"], alternative_sha256)
    approved = approve_batch(connection, batch["batch_id"])
    values = {
        "source_directory": "/annotation-test/renamed",
        "location_name": "测试地点",
        "representative_video_sha256": alternative_sha256,
        "frame_timestamp_ms": 1,
        "frame_path": "/frames/replacement.jpg",
        "frame_roi": Jsonb({"x": 0, "y": 0, "width": 200, "height": 100}),
        "supplemental_frames": Jsonb([{"frame_path": "/frames/supplemental.jpg"}]),
        "labels": Jsonb([]),
        "schema_version": "aerial_annotation_v2",
    }
    changed = update_batch(connection, batch["batch_id"], **{field: values[field]})
    assert changed["review_status"] == "pending"
    assert changed["validation_passed"] is False
    assert changed["reviewer_name"] is None
    assert changed["reviewed_at"] is None
    assert changed["label_revision"] == approved["label_revision"] + 1
    assert changed["updated_at"] > approved["updated_at"]
    assert changed["created_at"] == approved["created_at"]


def test_model_metadata_update_preserves_approval_and_refreshes_update_time(connection):
    batch = ready_batch(connection)
    approved = approve_batch(connection, batch["batch_id"])
    changed = update_batch(
        connection, batch["batch_id"], raw_model_response="saved raw response",
        model_name="test-model", prompt_version="test-prompt-v2",
    )
    assert changed["review_status"] == "approved"
    assert changed["validation_passed"] is True
    assert changed["reviewer_name"] == approved["reviewer_name"]
    assert changed["reviewed_at"] == approved["reviewed_at"]
    assert changed["label_revision"] == approved["label_revision"]
    assert changed["updated_at"] > approved["updated_at"]


def test_saving_unchanged_labels_preserves_review_and_refreshes_update_time(connection):
    batch = ready_batch(connection)
    approved = approve_batch(connection, batch["batch_id"])
    changed = update_batch(connection, batch["batch_id"], labels=Jsonb(LABELS))
    assert changed["review_status"] == "approved"
    assert changed["validation_passed"] is True
    assert changed["label_revision"] == approved["label_revision"]
    assert changed["updated_at"] > approved["updated_at"]


def test_reader_can_select_batches_and_memberships(connection):
    batch = ready_batch(connection)
    connection.execute("SET ROLE reader")
    assert connection.execute(
        "SELECT labels FROM aerial.annotation_batches WHERE batch_id = %s", (batch["batch_id"],)
    ).fetchone()["labels"] == LABELS
    assert connection.execute(
        "SELECT video_sha256 FROM aerial.annotation_batch_videos WHERE batch_id = %s", (batch["batch_id"],)
    ).fetchone()["video_sha256"] == batch["representative_video_sha256"]


@pytest.mark.parametrize("table", ["annotation_batches", "annotation_batch_videos"])
@pytest.mark.parametrize("operation", ["INSERT", "UPDATE", "DELETE", "TRUNCATE"])
def test_reader_cannot_modify_annotation_data(connection, table, operation):
    batch = ready_batch(connection)
    statements = {
        ("annotation_batches", "INSERT"): (
            "INSERT INTO aerial.annotation_batches (source_directory) VALUES (%s)", ("/reader-attempt",)
        ),
        ("annotation_batch_videos", "INSERT"): (
            "INSERT INTO aerial.annotation_batch_videos (batch_id, video_sha256) VALUES (%s, %s)",
            (batch["batch_id"], batch["representative_video_sha256"]),
        ),
    }
    statement, parameters = statements.get((table, operation), (None, None))
    if statement is None:
        statement = {
            "UPDATE": sql.SQL("UPDATE aerial.{} SET batch_id = batch_id").format(sql.Identifier(table)),
            "DELETE": sql.SQL("DELETE FROM aerial.{}").format(sql.Identifier(table)),
            "TRUNCATE": sql.SQL("TRUNCATE aerial.{}").format(sql.Identifier(table)),
        }[operation]
    connection.execute("SET ROLE reader")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
        connection.execute(statement, parameters)


def test_rerunning_migration_fails_without_changing_existing_rows(connection, database):
    batch = ready_batch(connection)
    batch = approve_batch(connection, batch["batch_id"])
    connection.commit()

    def snapshot():
        return {
            table: connection.execute(
                sql.SQL("SELECT to_jsonb(t) AS row FROM aerial.{} AS t ORDER BY to_jsonb(t)::text").format(sql.Identifier(table))
            ).fetchall()
            for table in ("videos", "annotation_batches", "annotation_batch_videos")
        }

    try:
        before = snapshot()
        with psycopg.connect(**database, autocommit=True) as rerun:
            rerun.execute("SET ROLE ad4che")
            with pytest.raises(psycopg.Error):
                rerun.execute(MIGRATION.read_text(encoding="utf-8"))
            rerun.execute("ROLLBACK")
        assert snapshot() == before
    finally:
        connection.execute("DELETE FROM aerial.annotation_batches WHERE batch_id = %s", (batch["batch_id"],))
        connection.execute("DELETE FROM aerial.videos WHERE sha256 = %s", (batch["representative_video_sha256"],))
        connection.commit()

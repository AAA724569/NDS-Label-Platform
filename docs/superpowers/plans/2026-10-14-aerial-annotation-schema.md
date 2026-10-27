# Aerial Annotation Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the two approved annotation tables in aerial_db and retain a tested migration.

**Architecture:** A batch row contains frame evidence and reviewed JSONB labels; a relation table connects existing videos. A deferred foreign key ensures the representative frame belongs to the batch. Execute DDL only with an existing authorized maintenance connection.

**Tech Stack:** PostgreSQL 14, SQL, Python/pytest/psycopg for isolated integration tests.

**Spec:** ../specs/2026-10-14-aerial-annotation-schema.md

## Global Constraints

- Exactly two new tables: aerial.annotation_batches and aerial.annotation_batch_videos.
- Existing aerial.videos remains structurally and semantically unchanged.
- reader receives SELECT only; no new role or authentication changes.
- Raw model responses remain TEXT; arrays/object JSONB shapes are checked.
- No model calls, population job, frontend adaptation, or Metabase account changes in this task.

## Review Focus

- A representative source video outside its batch must fail at transaction commit.
- Unknown/invalid JSON shapes cannot be mistaken for approved labels.
- Changing approved labels or frame evidence must require another review.
- Reapplying a migration must leave existing tables/data unchanged after failure.
- Read-only users must see both tables without gaining write permissions.

### Task 1: Migration and database contract

**Files:** migrations/001_aerial_annotations.sql; tests/test_annotation_schema.py; docs/aerial-annotation-storage.md.

**Interfaces:** migration consumes existing aerial schema, videos(sha256 BYTEA primary key) and reader role; produces the two tables and audit trigger.

- [x] Write real PostgreSQL tests and observe a missing-migration failure.
- [x] Implement the transactional SQL migration, constraints, comments and SELECT grants.
- [x] Run the entire test suite against isolated PostgreSQL 14.
- [x] Write storage usage examples and review the migration independently.

### Task 2: Apply and verify on the server

**Interfaces:** consumes tested SQL and an authorized schema-owner connection; produces empty live tables accessible to reader.

- [x] Locate a legitimate maintenance connection; request its configuration path if unavailable.
- [x] Verify target database/schema owner and capture current video count before applying.
- [x] Apply the reviewed migration once, then verify tables, permissions and unchanged video count with reader.
- [x] Record verified deployment status, commit only this task's files and report any remaining connection blocker accurately.

## Execution record

- User's explicit execution request and owner connection authorized immediate implementation; no repeated approval gate was added.
- Initial RED: migration absent. Permission RED: existing owner default grants allowed reader INSERT.
- Fixed default permission leakage with explicit revocation plus a pre-commit effective privilege guard.
- Full isolated test suite: 70 passed in 2.74s; independent SQL reviewer found no other blocker.
- Live migration committed successfully: two empty tables owned by ad4che, reader SELECT only.
- Existing video counts before/after: 26,179 total / 22,594 active. Reader UPDATE/DELETE denied on both tables.
- No source-video modifications, model calls, batch generation or frontend/Metabase changes.

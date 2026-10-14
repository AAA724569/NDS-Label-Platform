# Batch Annotation and Live Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run resumable single-frame per-folder annotation and display progress on the campus LAN statistics page.

**Architecture:** PG source inventory supplies grouped jobs; private local frame selection supplies one image to DeepSeek Flash. Existing two annotation tables persist work state and candidate labels. Independent reader dashboard fragment refreshes every10 seconds.

**Tech Stack:** PostgreSQL14, Python urllib/psycopg/Pillow/Shapely/FFmpeg, Streamlit1.65, systemd user service.

**Spec:** ../specs/2026-10-14-annotation-worker.md

## Global Constraints

- Only existing PostgreSQL source and two annotation tables; no old SQLite or old results.
- One representative image per folder by default; scope frame, review pending.
- Secrets outside Git with mode0600; endpoint https://api.deepseek.com only.
- Reader remains SELECT only; no raw paths/GPS/hash/raw response/unreviewed frames on report.
- Single worker, bounded retries and no automatic paid retry of invalid label outputs.

## Review Focus

- Mixed/unknown durations and inaccessible source folders must be visible without fabricated data.
- Resume must not double charge completed work or overwrite manually approved labels.
- Label changes trigger validation reset, so result saving needs a second validation update in the same transaction.
- Operational failures/review states are separate; model candidates never automatically approved.
- Report must not leak raw API errors or private source information.

### Task 1: Schema and normalized label contract

- [x] Add processing_info migration and real PostgreSQL state tests.
- [x] Implement fixed taxonomy/prompt/validator and functional tests (delegated).

### Task 2: Worker

- [x] Build PG-only discovery, grouping, candidate selection and resumable work states.
- [x] Implement official image API request, raw-result cache and strict atomic saving.
- [x] Test representative selection, failure handling, review-safe retries and migration compatibility.

### Task 3: Report and deployment

- [x] Add safe10-second reader report to existing statistics page (delegated).
- [x] Review changes, run relevant suites, apply migration and private service configuration.
- [x] Discover jobs, process one real batch, then launch enabled queue service.
- [x] Restart dashboard, check LAN HTTP/WebSocket and verify live progress increases.

## Decisions and evidence

- User explicitly requested implementation/start; existing skill gates did not introduce repeat approval requests.
- Parent folders retained as instructed, even for long/multi-date groups; report flags grouping uncertainty, labels remain frame observations.
- Existing authorized ad4che maintenance connection used for writing only annotation tables; reader handles original-video reads and report queries.
- Default raw-response cache is private. Versioned prompt/image cache and immutable image paths preserve past evidence and reduce duplicate calls.
- Reviewer findings fixed with five RED→GREEN regressions: attempt cap, usable-source fallback, evidence-file immutability, human changes and review terminalization.
- First v1 live result exposed absence-value ambiguity; v2 prompt + unknown-reason enforcement passed real27-field output.
- Worker suite193 passes; report suite72 passes; realPG has2324 jobs and11 skipped groups, service enabled/active, candidate counts increasing.
- v3 provides explicit English state examples and parent/subtype mapping; existing failed outputs retained for manual review, no broad automatic paid requeue.

-- Migration 008: persist recoverable weekly report facts (PRD 18).
--
-- A weekly baseline-apply commits the run state in one transaction and then
-- writes the sanitized Markdown/JSON report to the filesystem. If a report
-- write fails after the commit, PRD section 18 requires: retain the SQLite
-- truth, mark the report recovery required, and allow deterministic
-- regeneration.
--
-- This table persists the fully-rendered, sanitized report texts and the
-- observation timestamp inside the same transaction as the run state. A
-- report whose file write has not yet succeeded carries report_written = 0
-- ("report recovery required"). A later phase regenerates the files
-- byte-for-byte from these stored texts and flips report_written to 1.

CREATE TABLE IF NOT EXISTS weekly_reports (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    report_md TEXT NOT NULL,
    report_json TEXT NOT NULL,
    report_md_path TEXT,
    report_json_path TEXT,
    report_written INTEGER NOT NULL DEFAULT 0 CHECK(report_written IN (0, 1)),
    observation_generated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id)
);

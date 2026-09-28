-- Migration 004: persist recoverable history-sync report facts (PRD 18).
--
-- A history sync-apply commits the import in one transaction and then writes
-- the sanitized Markdown/JSON report to the filesystem. If a report write
-- fails after the commit, PRD section 18 requires: retain the SQLite truth,
-- mark the report recovery required, and allow deterministic regeneration.
--
-- This table persists the fully-rendered, sanitized report texts (and their
-- intended file paths) inside the same transaction as the import. A report
-- whose file write has not yet succeeded carries report_written = 0
-- ("report recovery required"). A later sync phase regenerates the files
-- byte-for-byte from these stored texts and flips report_written to 1, so
-- the successful import summary is never lost to a later zero-unknown no-op.
--
-- report_key is deterministic per scan proposal ("apply:<proposal checksum>")
-- so regeneration is idempotent and a later no-op re-apply cannot overwrite
-- the record of the successful import.

CREATE TABLE IF NOT EXISTS history_sync_reports (
    id INTEGER PRIMARY KEY,
    report_key TEXT NOT NULL UNIQUE,
    phase TEXT NOT NULL,
    proposal_checksum TEXT,
    payload_json TEXT NOT NULL,
    report_md TEXT NOT NULL,
    report_written INTEGER NOT NULL DEFAULT 0 CHECK(report_written IN (0, 1)),
    report_md_path TEXT,
    report_json_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

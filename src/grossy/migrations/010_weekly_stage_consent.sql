-- FK_OFF
-- Migration 010: stage consent and basket-review barrier (Task 16).
--
-- Adds the `awaiting-basket-review` lifecycle state and creates the
-- `run_stage_consent` table for durable run-scoped stage authorization.
-- The planner_runs table is rebuilt because SQLite does not support
-- ALTER TABLE ... DROP CONSTRAINT or CHECK modification.

-- Rebuild planner_runs with the updated lifecycle CHECK constraint.
CREATE TABLE planner_runs_new (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    seed TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK(lifecycle IN (
        'planning', 'recurring-apply', 'awaiting-basket-review',
        'awaiting-recipe-selection', 'recipe-apply',
        'awaiting-user-verification', 'completed',
        'needs-attention', 'partial-failure', 'abandoned'
    )),
    history_sync_status TEXT CHECK(history_sync_status IS NULL OR history_sync_status IN ('pending', 'success', 'skipped', 'failed')),
    budget_eur_cents INTEGER CHECK(budget_eur_cents IS NULL OR budget_eur_cents >= 0),
    report_md_path TEXT,
    report_json_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO planner_runs_new SELECT * FROM planner_runs;
DROP TABLE planner_runs;
ALTER TABLE planner_runs_new RENAME TO planner_runs;

-- Recreate the one-active-run triggers (dropped with the old table).
CREATE TRIGGER IF NOT EXISTS trg_one_active_run
    BEFORE INSERT ON planner_runs
    WHEN NEW.lifecycle NOT IN ('completed', 'abandoned')
BEGIN
    SELECT RAISE(ABORT, 'one_active_run_violation: an active weekly run already exists')
    WHERE EXISTS (
        SELECT 1 FROM planner_runs WHERE lifecycle NOT IN ('completed', 'abandoned')
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_one_active_run_update
    BEFORE UPDATE OF lifecycle ON planner_runs
    WHEN NEW.lifecycle NOT IN ('completed', 'abandoned')
         AND OLD.lifecycle IN ('completed', 'abandoned')
BEGIN
    SELECT RAISE(ABORT, 'one_active_run_violation: an active weekly run already exists')
    WHERE EXISTS (
        SELECT 1 FROM planner_runs WHERE id != OLD.id AND lifecycle NOT IN ('completed', 'abandoned')
    );
END;

-- Durable run-scoped stage consent (Task 16, PRD FR-114..FR-128 deltas).
-- One row per (run, stage); status transitions granted → revoked are
-- append-only evidence (no deletion).
CREATE TABLE IF NOT EXISTS run_stage_consent (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    stage TEXT NOT NULL CHECK(stage IN ('recurring', 'recipe')),
    status TEXT NOT NULL DEFAULT 'granted' CHECK(status IN ('granted', 'revoked')),
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    rationale TEXT,
    UNIQUE(run_id, stage)
);

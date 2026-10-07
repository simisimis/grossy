-- Append-only, run/revision-bound ingredient previews; preserve frozen plans.
CREATE TABLE run_recipe_mappings (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    recipe_revision_id INTEGER NOT NULL REFERENCES recipe_revisions(id),
    proposal_checksum TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    mapping_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, recipe_revision_id, proposal_checksum)
);
CREATE TRIGGER recipe_mapping_immutable_update BEFORE UPDATE ON run_recipe_mappings
BEGIN SELECT RAISE(ABORT, 'recipe mappings are immutable'); END;
CREATE TRIGGER recipe_mapping_immutable_delete BEFORE DELETE ON run_recipe_mappings
BEGIN SELECT RAISE(ABORT, 'recipe mappings are immutable'); END;

-- Immutable run-derived candidates, policy, projected matches and seen history.
CREATE TABLE run_recipe_plans (
    run_id INTEGER PRIMARY KEY REFERENCES planner_runs(id),
    seed TEXT NOT NULL,
    proposal_checksum TEXT NOT NULL,
    proposal_generated_at TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    exclusions_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE run_recipe_seen (
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    first_shown_at TEXT NOT NULL,
    PRIMARY KEY (run_id, recipe_id)
);
CREATE TRIGGER recipe_plan_immutable_update BEFORE UPDATE ON run_recipe_plans
BEGIN SELECT RAISE(ABORT, 'recipe plan snapshots are immutable'); END;
CREATE TRIGGER recipe_plan_immutable_delete BEFORE DELETE ON run_recipe_plans
BEGIN SELECT RAISE(ABORT, 'recipe plan snapshots are immutable'); END;
CREATE TRIGGER recipe_seen_immutable_update BEFORE UPDATE ON run_recipe_seen
BEGIN SELECT RAISE(ABORT, 'recipe seen history is immutable'); END;
CREATE TRIGGER recipe_seen_immutable_delete BEFORE DELETE ON run_recipe_seen
BEGIN SELECT RAISE(ABORT, 'recipe seen history is immutable'); END;

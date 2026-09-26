-- Grossy initial schema (PRD section 12)
-- All timestamps are UTC ISO 8601. Currency uses integer minor units (cents).

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    predefined_status TEXT CHECK(predefined_status IS NULL OR predefined_status IN ('breakfast', 'lunch')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    retailer TEXT NOT NULL DEFAULT 'ab',
    retailer_product_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    name TEXT NOT NULL,
    brand TEXT,
    package_amount REAL,
    package_unit TEXT,
    package_count INTEGER,
    package_display TEXT,
    unit_basis TEXT,
    category_id INTEGER REFERENCES categories(id),
    first_observed_date TEXT,
    last_observed_date TEXT,
    purchase_order_count INTEGER NOT NULL DEFAULT 0 CHECK(purchase_order_count >= 0),
    typical_quantity INTEGER NOT NULL DEFAULT 1 CHECK(typical_quantity > 0),
    blacklist_state TEXT NOT NULL DEFAULT 'active' CHECK(blacklist_state IN ('active', 'blacklisted')),
    observation_state TEXT NOT NULL DEFAULT 'active' CHECK(observation_state IN ('active', 'retired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(retailer, retailer_product_id)
);

CREATE TABLE IF NOT EXISTS source_orders (
    id INTEGER PRIMARY KEY,
    retailer TEXT NOT NULL DEFAULT 'ab',
    source_order_id TEXT NOT NULL,
    displayed_label TEXT NOT NULL,
    order_date TEXT NOT NULL,
    import_timestamp TEXT NOT NULL,
    displayed_product_count INTEGER,
    stabilized_product_count INTEGER,
    diagnostics_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(retailer, source_order_id)
);

CREATE TABLE IF NOT EXISTS source_order_products (
    id INTEGER PRIMARY KEY,
    source_order_id INTEGER NOT NULL REFERENCES source_orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    created_at TEXT NOT NULL,
    UNIQUE(source_order_id, product_id)
);

CREATE TABLE IF NOT EXISTS product_families (
    id INTEGER PRIMARY KEY,
    family_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    primary_product_id INTEGER REFERENCES products(id),
    recurrence_weeks INTEGER CHECK(recurrence_weeks IS NULL OR recurrence_weeks > 0),
    recurrence_source TEXT,
    recurrence_confidence TEXT CHECK(recurrence_confidence IS NULL OR recurrence_confidence IN ('low', 'medium', 'high')),
    last_purchase_date TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    blacklist_state TEXT NOT NULL DEFAULT 'active' CHECK(blacklist_state IN ('active', 'blacklisted')),
    category_id INTEGER REFERENCES categories(id),
    confidence TEXT CHECK(confidence IS NULL OR confidence IN ('low', 'medium', 'high')),
    rationale TEXT,
    manual_override_flags TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- At most one primary per family (PRD 12.1.7)
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_primary_per_family
    ON product_families(primary_product_id)
    WHERE primary_product_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS family_members (
    id INTEGER PRIMARY KEY,
    family_id INTEGER NOT NULL REFERENCES product_families(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    role TEXT NOT NULL CHECK(role IN ('primary', 'alternative')),
    approval_state TEXT NOT NULL DEFAULT 'approved' CHECK(approval_state IN ('pending', 'approved', 'rejected')),
    provenance TEXT,
    confidence TEXT CHECK(confidence IS NULL OR confidence IN ('low', 'medium', 'high')),
    created_at TEXT NOT NULL,
    UNIQUE(family_id, product_id)
);

-- A product may belong to at most one active family (PRD 12.1.6)
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_family_per_product
    ON family_members(product_id);

CREATE TABLE IF NOT EXISTS manual_overrides (
    id INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('product', 'family', 'category')),
    target_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    field_value TEXT NOT NULL,
    rationale TEXT,
    created_at TEXT NOT NULL,
    superseded_at TEXT
);

CREATE TABLE IF NOT EXISTS blacklist_events (
    id INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('product', 'family')),
    target_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('blacklist', 'unblacklist')),
    rationale TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY,
    recipe_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    reference_url TEXT,
    source_name TEXT,
    source_tier TEXT,
    cuisine TEXT,
    protein_class TEXT CHECK(protein_class IS NULL OR protein_class IN ('meat', 'fish', 'poultry', 'vegetarian')),
    total_time_minutes INTEGER CHECK(total_time_minutes IS NULL OR total_time_minutes > 0),
    current_revision_id INTEGER,
    base_weight REAL NOT NULL DEFAULT 1.0 CHECK(base_weight > 0),
    is_favorite INTEGER NOT NULL DEFAULT 0 CHECK(is_favorite IN (0, 1)),
    is_regular INTEGER NOT NULL DEFAULT 1 CHECK(is_regular IN (0, 1)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipe_revisions (
    id INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    revision_number INTEGER NOT NULL,
    source_name TEXT,
    reference_url TEXT,
    cuisine TEXT,
    servings REAL NOT NULL DEFAULT 3.0 CHECK(servings > 0),
    total_time_minutes INTEGER CHECK(total_time_minutes IS NULL OR total_time_minutes > 0),
    steps_json TEXT NOT NULL,
    provenance TEXT,
    accepted_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(recipe_id, revision_number)
);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id INTEGER PRIMARY KEY,
    revision_id INTEGER NOT NULL REFERENCES recipe_revisions(id),
    ingredient_name TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount > 0),
    unit TEXT NOT NULL,
    pantry_eligible INTEGER NOT NULL DEFAULT 0 CHECK(pantry_eligible IN (0, 1)),
    indivisible INTEGER NOT NULL DEFAULT 0 CHECK(indivisible IN (0, 1)),
    package_unit TEXT,
    package_size REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS recipe_appliance_requirements (
    id INTEGER PRIMARY KEY,
    revision_id INTEGER NOT NULL REFERENCES recipe_revisions(id),
    appliance_type TEXT NOT NULL,
    capability TEXT NOT NULL,
    model_constraint TEXT,
    capacity_constraint REAL,
    capacity_unit TEXT
);

CREATE TABLE IF NOT EXISTS recipe_variants (
    id INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    linked_recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    variant_type TEXT NOT NULL CHECK(variant_type IN ('air_fryer', 'thermomix_native', 'other')),
    created_at TEXT NOT NULL,
    UNIQUE(recipe_id, linked_recipe_id, variant_type)
);

CREATE TABLE IF NOT EXISTS recipe_events (
    id INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    event_type TEXT NOT NULL CHECK(event_type IN ('suggested', 'selected', 'rerolled', 'favorite', 'regular', 'weight_changed', 'enabled', 'disabled')),
    run_id TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS planner_runs (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    seed TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK(lifecycle IN (
        'planning', 'recurring-apply', 'awaiting-recipe-selection',
        'recipe-apply', 'awaiting-user-verification', 'completed',
        'needs-attention', 'partial-failure', 'abandoned'
    )),
    history_sync_status TEXT CHECK(history_sync_status IS NULL OR history_sync_status IN ('pending', 'success', 'skipped', 'failed')),
    budget_eur_cents INTEGER CHECK(budget_eur_cents IS NULL OR budget_eur_cents >= 0),
    report_md_path TEXT,
    report_json_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- At most one active (non-terminal) weekly run (PRD 12.1.8).
-- Enforced by trigger: a second non-terminal run insertion is rejected.
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

CREATE TABLE IF NOT EXISTS run_cart_baselines (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    price_cents INTEGER CHECK(price_cents IS NULL OR price_cents >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, product_id)
);

CREATE TABLE IF NOT EXISTS run_recurring_items (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    family_id INTEGER REFERENCES product_families(id),
    category TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('yaml', 'learned', 'both')),
    intended_quantity INTEGER NOT NULL CHECK(intended_quantity > 0),
    credited_quantity INTEGER NOT NULL DEFAULT 0 CHECK(credited_quantity >= 0),
    missing_delta INTEGER NOT NULL DEFAULT 0 CHECK(missing_delta >= 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'added', 'skipped', 'blocked', 'failed')),
    rationale TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_recipe_slots (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    slot_name TEXT NOT NULL CHECK(slot_name IN ('recipe1', 'recipe2', 'recipe3', 'recipe4')),
    protein_class TEXT NOT NULL CHECK(protein_class IN ('meat', 'fish', 'poultry', 'vegetarian')),
    recipe_revision_id INTEGER REFERENCES recipe_revisions(id),
    eligibility_result TEXT CHECK(eligibility_result IS NULL OR eligibility_result IN ('eligible', 'ineligible', 'empty')),
    empty_reason TEXT,
    seed TEXT,
    current_option_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, slot_name)
);

CREATE TABLE IF NOT EXISTS run_rerolls (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    slot_name TEXT NOT NULL CHECK(slot_name IN ('recipe1', 'recipe2', 'recipe3', 'recipe4')),
    reroll_number INTEGER NOT NULL CHECK(reroll_number > 0),
    candidate_scope TEXT NOT NULL,
    previous_recipe_id INTEGER,
    new_recipe_id INTEGER,
    result TEXT NOT NULL CHECK(result IN ('success', 'no_candidate', 'failed')),
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, slot_name, reroll_number)
);

CREATE TABLE IF NOT EXISTS run_recipe_selections (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    arg_order INTEGER NOT NULL CHECK(arg_order > 0),
    slot_name TEXT NOT NULL CHECK(slot_name IN ('recipe1', 'recipe2', 'recipe3', 'recipe4')),
    dinner_label TEXT NOT NULL,
    recipe_id INTEGER REFERENCES recipes(id),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, arg_order)
);

CREATE TABLE IF NOT EXISTS run_ingredient_targets (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    selection_id INTEGER NOT NULL REFERENCES run_recipe_selections(id),
    ingredient_name TEXT NOT NULL,
    scaled_target REAL NOT NULL CHECK(scaled_target > 0),
    unit TEXT NOT NULL,
    pantry_credit REAL NOT NULL DEFAULT 0 CHECK(pantry_credit >= 0),
    cart_credit REAL NOT NULL DEFAULT 0 CHECK(cart_credit >= 0),
    retail_package_target REAL,
    retail_package_unit TEXT,
    match_product_id INTEGER REFERENCES products(id),
    match_approval TEXT CHECK(match_approval IS NULL OR match_approval IN ('exact', 'substituted', 'blocked')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_mutation_manifests (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    stage TEXT NOT NULL CHECK(stage IN ('recurring', 'recipe', 'removal')),
    manifest_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_mutation_observations (
    id INTEGER PRIMARY KEY,
    manifest_id INTEGER NOT NULL REFERENCES run_mutation_manifests(id),
    observed_json TEXT NOT NULL,
    discrepancies_json TEXT,
    failure_class TEXT CHECK(failure_class IS NULL OR failure_class IN ('none', 'partial', 'total')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_removals (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    authorized_quantity INTEGER NOT NULL CHECK(authorized_quantity > 0),
    run_attributed_quantity INTEGER NOT NULL CHECK(run_attributed_quantity >= 0),
    preexisting_quantity INTEGER NOT NULL DEFAULT 0 CHECK(preexisting_quantity >= 0),
    include_preexisting INTEGER NOT NULL DEFAULT 0 CHECK(include_preexisting IN (0, 1)),
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0, 1)),
    observed_result TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES planner_runs(id),
    event_type TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY,
    export_path TEXT,
    schema_version INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    exported_at TEXT NOT NULL,
    notes TEXT
);

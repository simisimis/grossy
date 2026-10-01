-- Migration 006: re-key recipe_variants from recipe identity to revision
-- (PRD FR-79, FR-80; task 07).
--
-- 001 created recipe_variants keyed by recipe_id (the recipe's integer row
-- id). Task 07 stores variant links against the declaring revision so a
-- historical revision snapshot retains the exact variant relationships of
-- that revision (FR-79, FR-80). 001 is an applied, checksummed migration and
-- must never be rewritten (PRD 12.2.6: a checksum mismatch must stop
-- startup), so the conversion is delivered as this forward migration and a
-- database that already applied 001 upgrades in place (PRD 12.2.10).
--
-- Conversion:
--   1. Pre-check: verify every existing variant row can be mapped to a
--      resolvable revision. The resolution rule is:
--        a. Use recipes.current_revision_id ONLY IF the referenced revision
--           exists and belongs to the declaring recipe (001 has no FK or
--           ownership constraint on this bare INTEGER).
--        b. Otherwise fall back to the recipe's highest revision_number.
--      If neither resolves (no valid current_revision_id AND no rows in
--      recipe_revisions for that recipe), the migration fails transactionally
--      before any destructive operation (PRD 12.2.9 prohibits unauthorized
--      destructive migrations).
--   2. recipe_variants is renamed aside.
--   3. The replacement table is created keyed by revision_id.
--   4. Existing rows are copied using the identical resolution logic.
--   5. The old table is dropped.
--
-- Old UNIQUE(recipe_id, linked_recipe_id, variant_type) cannot collide with
-- new UNIQUE(revision_id, linked_recipe_id, variant_type): a revision belongs
-- to exactly one recipe, so two distinct old recipe_id values can never map
-- to the same revision_id.
--
-- The whole script runs in one transaction (PRD 12.2.5); a failure rolls
-- back and leaves the prior database usable (PRD 12.2.7, 12.2.8).

-- Pre-check: fail if any variant row references a recipe with no resolvable
-- revision. The resolution rule validates current_revision_id ownership
-- (the revision must exist and belong to the declaring recipe) and falls
-- back to the recipe's highest revision_number. If neither yields a
-- revision, the migration aborts before any destructive operation (PRD 12.2.9).
CREATE TEMPORARY TABLE _mig006_precheck (
    orphan_count INTEGER CHECK(orphan_count = 0)
);
INSERT INTO _mig006_precheck
SELECT COUNT(*)
FROM recipe_variants v
JOIN recipes p ON p.id = v.recipe_id
WHERE COALESCE(
    CASE
        WHEN p.current_revision_id IS NOT NULL
             AND EXISTS (
                 SELECT 1 FROM recipe_revisions r
                 WHERE r.id = p.current_revision_id
                   AND r.recipe_id = p.id
             )
        THEN p.current_revision_id
    END,
    (SELECT r.id FROM recipe_revisions r
     WHERE r.recipe_id = p.id
     ORDER BY r.revision_number DESC
     LIMIT 1)
) IS NULL;
DROP TABLE _mig006_precheck;

ALTER TABLE recipe_variants RENAME TO recipe_variants_old;

CREATE TABLE recipe_variants (
    id INTEGER PRIMARY KEY,
    revision_id INTEGER NOT NULL REFERENCES recipe_revisions(id),
    linked_recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    variant_type TEXT NOT NULL CHECK(variant_type IN ('air_fryer', 'thermomix_native', 'other')),
    created_at TEXT NOT NULL,
    UNIQUE(revision_id, linked_recipe_id, variant_type)
);

INSERT INTO recipe_variants (id, revision_id, linked_recipe_id, variant_type, created_at)
SELECT v.id,
       COALESCE(
           CASE
               WHEN p.current_revision_id IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM recipe_revisions r
                        WHERE r.id = p.current_revision_id
                          AND r.recipe_id = p.id
                    )
               THEN p.current_revision_id
           END,
           (SELECT r.id FROM recipe_revisions r
            WHERE r.recipe_id = p.id
            ORDER BY r.revision_number DESC
            LIMIT 1)
       ),
       v.linked_recipe_id,
       v.variant_type,
       v.created_at
FROM recipe_variants_old v
JOIN recipes p ON p.id = v.recipe_id;

DROP TABLE recipe_variants_old;

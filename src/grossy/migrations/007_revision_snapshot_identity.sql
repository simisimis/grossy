-- Migration 007: add identity fields to recipe_revisions for historical
-- snapshot recoverability (PRD FR-79, FR-80; task 08).
--
-- 001 created recipe_revisions without title, source_tier, or protein_class.
-- These fields live on the mutable recipes row and are updated by
-- insert_revision_for_existing_recipe on refresh. Without them stored on the
-- revision, a historical revision cannot recover the title/source-tier/
-- protein-class that was current when it was accepted.
--
-- The new columns are added nullable (existing rows have no known historical
-- value distinct from the current recipe state). They are backfilled with the
-- current recipe values, which is the best available information: for
-- revisions created before this migration, the recipe's current identity is
-- the only record. New revisions created after this migration always store
-- the exact values at creation time.
--
-- This is a pure additive migration: no existing data is modified or removed,
-- no destructive operation is performed (PRD 12.2.9).

ALTER TABLE recipe_revisions ADD COLUMN title TEXT;
ALTER TABLE recipe_revisions ADD COLUMN source_tier TEXT;
ALTER TABLE recipe_revisions ADD COLUMN protein_class TEXT;

-- Backfill from the owning recipe's current identity fields. This is the best
-- available approximation for pre-existing revisions; new revisions will
-- always store exact values going forward.
UPDATE recipe_revisions
SET title = p.title,
    source_tier = p.source_tier,
    protein_class = p.protein_class
FROM recipes p
WHERE recipe_revisions.recipe_id = p.id;

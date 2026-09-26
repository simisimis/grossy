-- Migration 002: one primary member per family (review finding for Task 02).
--
-- 001 already guarantees that a single product is the primary member of at most
-- one family (idx_one_family_per_product on family_members(product_id) and
-- idx_one_primary_per_family on product_families(primary_product_id)). But
-- nothing stops TWO different products both being members of the SAME family
-- with role = 'primary'. That is the exact "one primary member per family"
-- case (PRD 12.1.7) that was unenforced, and it also let
-- product_families.primary_product_id drift out of sync with family_members
-- (e.g. after the last primary member is deleted or demoted to 'alternative').
--
-- This migration:
--   1. Adds a partial unique index so a family has at most one primary member.
--   2. Keeps product_families.primary_product_id consistent with
--      family_members on insert / update / delete.
--
-- It is additive: no existing tables, columns, or applied-migration records
-- are modified, so 001's stored checksum is preserved.

-- At most one primary member per family. family_id references
-- product_families.id (the index target exists, so this is valid now).
CREATE UNIQUE INDEX IF NOT EXISTS family_members_one_primary_per_family
    ON family_members (family_id) WHERE role = 'primary';

-- Recompute product_families.primary_product_id after a member is inserted.
-- AFTER (not BEFORE) so the new row is visible to the recompute.
CREATE TRIGGER IF NOT EXISTS family_members_after_insert
AFTER INSERT ON family_members
FOR EACH ROW
BEGIN
    UPDATE product_families
    SET primary_product_id = (
        SELECT m.product_id FROM family_members m
        WHERE m.family_id = NEW.family_id AND m.role = 'primary'
        LIMIT 1
    )
    WHERE id = NEW.family_id;
END;

-- Recompute after a member is deleted. The row is already gone, so a deleted
-- primary member no longer contributes (the field falls back to another
-- primary, or NULL when it was the last one).
CREATE TRIGGER IF NOT EXISTS family_members_after_delete
AFTER DELETE ON family_members
FOR EACH ROW
BEGIN
    UPDATE product_families
    SET primary_product_id = (
        SELECT m.product_id FROM family_members m
        WHERE m.family_id = OLD.family_id AND m.role = 'primary'
        LIMIT 1
    )
    WHERE id = OLD.family_id;
END;

-- Recompute after a member's role or family changes. Covers both promotion to
-- 'primary' and demotion to 'alternative'.
CREATE TRIGGER IF NOT EXISTS family_members_after_update
AFTER UPDATE ON family_members
FOR EACH ROW
BEGIN
    UPDATE product_families
    SET primary_product_id = (
        SELECT m.product_id FROM family_members m
        WHERE m.family_id = OLD.family_id AND m.role = 'primary'
        LIMIT 1
    )
    WHERE id = OLD.family_id;
    UPDATE product_families
    SET primary_product_id = (
        SELECT m.product_id FROM family_members m
        WHERE m.family_id = NEW.family_id AND m.role = 'primary'
        LIMIT 1
    )
    WHERE id = NEW.family_id;
END;

-- product_families.primary_product_id is derived from family_members (the
-- single source of truth). A direct UPDATE that sets it to a value disagreeing
-- with the actual primary member is rejected (PRD 12.1.7).
CREATE TRIGGER IF NOT EXISTS product_families_before_update_primary
BEFORE UPDATE OF primary_product_id ON product_families
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'primary_product_id is derived from family_members; change the member role instead')
    WHERE NEW.primary_product_id IS NOT (
        SELECT m.product_id FROM family_members m
        WHERE m.family_id = OLD.id AND m.role = 'primary'
        LIMIT 1
    );
END;

-- Reconcile existing rows where primary_product_id disagrees with the actual
-- primary member in family_members (upgrade from schema version 1 where this
-- invariant was not enforced). The BEFORE UPDATE trigger allows this because
-- the new value matches the primary member.
UPDATE product_families
SET primary_product_id = (
    SELECT m.product_id FROM family_members m
    WHERE m.family_id = product_families.id AND m.role = 'primary'
    LIMIT 1
)
WHERE primary_product_id IS NOT (
    SELECT m.product_id FROM family_members m
    WHERE m.family_id = product_families.id AND m.role = 'primary'
    LIMIT 1
);

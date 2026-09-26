-- Migration 003: reject direct product_families INSERT with non-NULL primary_product_id.
--
-- Migration 002 added triggers that keep product_families.primary_product_id
-- consistent with family_members on INSERT/UPDATE/DELETE of members. But the
-- BEFORE UPDATE trigger only guards UPDATE. A product_families row can still
-- be inserted with a non-NULL primary_product_id that has no corresponding
-- family_members row with role 'primary'. Since family_members is the source
-- of truth (PRD 12.1.7), the correct flow is:
--   1. Create the family with primary_product_id = NULL.
--   2. Insert the primary member; the AFTER INSERT trigger populates the field.
--
-- This migration adds a BEFORE INSERT trigger that rejects any attempt to set
-- a non-NULL primary_product_id directly, forcing the correct flow.

CREATE TRIGGER IF NOT EXISTS product_families_before_insert_primary
BEFORE INSERT ON product_families
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'primary_product_id is derived from family_members; insert the family with NULL primary, then add the primary member')
    WHERE NEW.primary_product_id IS NOT NULL;
END;

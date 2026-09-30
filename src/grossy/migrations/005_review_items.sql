-- Migration 005: persist reviewable learned-state assignments (PRD FR-37, FR-56, FR-28).
--
-- Task 05 learns product categories, product families, and recurrence from
-- synchronized completed orders. Two classes of inference are only *tentative*
-- and must remain reviewable rather than being silently committed:
--
--   1. a product observed with no category heading has no normalized category
--      to assign; it is left unassigned (category_id NULL) and flagged for
--      review instead of being dumped into a forbidden `misc` category (FR-37);
--   2. a family whose recurrence rests on a single dated occurrence is a
--      guarded, low-confidence category estimate and is not auto-added; it is
--      flagged for review (FR-51, FR-54).
--
-- These review items are first-class, persistent, and idempotent (upserted on
-- every inference pass) so a later manual correction (task 06) can resolve
-- them and the sanitized sync report can surface "links that require review"
-- (FR-28). This table is additive: no existing table, column, trigger, or
-- applied-migration record is modified, so prior checksums are preserved.

CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('category', 'recurrence', 'family_link')),
    target_type TEXT NOT NULL CHECK(target_type IN ('product', 'family')),
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(kind, target_type, target_id)
);

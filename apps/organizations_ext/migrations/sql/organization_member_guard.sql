-- Guard: an organization that has had members can never return to zero
-- members while it still exists and is not soft-deleted.
--
-- A live organization with no members is a broken, unrecoverable state:
-- nobody can administer it, and SSO auto-join would hand ownership to the
-- first user who authenticates through a linked identity provider. Python
-- guards the normal paths (owner checks on member removal and account
-- deletion); this trigger is the database-level backstop for any code path
-- that slips past them. Erroring loudly is preferred over allowing it.
--
-- Organizations may legitimately START with zero members: an admin can
-- create an organization (and its social app) before any user exists, and
-- the first SSO login claims it as owner. The trigger only fires when
-- member rows are deleted or moved, which presupposes members existed.
--
-- Pending invites (rows with user IS NULL) count as members here: an org
-- holding only an invite is still recoverable through invite acceptance,
-- and counting every row keeps the check minimal.
--
-- Deferred to commit so that transactions which delete and re-add members
-- pass, and so hard organization deletion (member rows and the org row
-- removed in the same transaction) passes. Soft-deleted organizations are
-- exempt: async cleanup deletes members while the org row still exists.
--
-- Test-suite note: Django's plain TestCase runs SET CONSTRAINTS ALL
-- IMMEDIATE at teardown, so a test that strands a live organization at zero
-- members errors there (at teardown, not at the offending line) even though
-- the test transaction is rolled back.

CREATE OR REPLACE FUNCTION organizations_ext_org_member_guard() RETURNS trigger AS $$
DECLARE
    live_org_id integer;
BEGIN
    -- Lock the organization row before counting members: two transactions
    -- concurrently deleting an org's last two members would otherwise each
    -- pass the check at commit, since neither snapshot sees the other's
    -- uncommitted delete. The second committer blocks on the lock, then
    -- re-checks with a snapshot that includes the first. A transaction whose
    -- member deletions span several organizations locks their rows in
    -- trigger-queue order, so a cross-order deadlock is possible in theory;
    -- that still fails loudly, which is the point of this guard.
    SELECT o.id INTO live_org_id
    FROM organizations_ext_organization o
    WHERE o.id = OLD.organization_id AND NOT o.is_deleted
    FOR NO KEY UPDATE;

    IF live_org_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM organizations_ext_organizationuser ou
        WHERE ou.organization_id = live_org_id
    ) THEN
        RAISE EXCEPTION
            'organization % cannot be left without members; delete the organization instead',
            OLD.organization_id
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'organization_has_members';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS organization_has_members ON organizations_ext_organizationuser;

CREATE CONSTRAINT TRIGGER organization_has_members
    AFTER DELETE OR UPDATE OF organization_id ON organizations_ext_organizationuser
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION organizations_ext_org_member_guard();

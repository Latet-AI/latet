-- A token added to an API key keeps its permissions in its row even when they are scan, mask and unmask (Ruling T13c:
-- latet.server.grants.stored makes 'mask,scan,unmask'), so that NULL stays the row of the tokens that serve agents
-- alone (a person's computer's, an API key's first, every token made before permissions existed), and an added token,
-- whatever it may do, acts through the applications API alone (Ruling T13b). The column takes that form too, and no
-- other than grants.stored makes.
ALTER TABLE tokens DROP CONSTRAINT tokens_permissions_check;
ALTER TABLE tokens ADD CONSTRAINT tokens_permissions_check CHECK (
    permissions IS NULL OR permissions IN (
        'scan', 'unmask', 'scan,unmask', 'mask,scan', 'mask,scan,unmask',
        'forget', 'forget,scan', 'forget,unmask', 'forget,scan,unmask', 'forget,mask,scan', 'forget,mask,scan,unmask'
    )
);

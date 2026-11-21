-- Latet 1.0.0, the final security review's wider option: renamed is true once an account's user name was changed by
-- anything but an administrator's link (SCIM's rename), and no link or sign-in confirmed it since. Made while nobody
-- had signed in to the account, the change dropped its claim (linked_by), so no first sign-in takes it; either way,
-- `latet server people link --all` lists it apart rather than linking it again by that user name. An administrator's
-- link (`latet server people link`), or a sign-in that brings that user name, sets it back to false.
ALTER TABLE accounts ADD COLUMN renamed BOOLEAN NOT NULL DEFAULT FALSE;

# Security policy

Latet handles secrets and personal data, on your machine and on a Latet server, so reports about its
security matter a lot.

## Reporting a vulnerability

Report it privately, with the **Report a vulnerability** button in this repository's **Security**
tab. If you cannot use it, write to [info@latet.ai](mailto:info@latet.ai) with "Security" in the
subject. Please do not open a public issue, pull request or discussion about it.

Say what you found, how to reproduce it, and what it lets someone do. You get an answer within five
working days, and a fix in a new release once the problem is confirmed. The release notes credit you,
unless you ask them not to.

## Supported versions

Fixes go into the latest release. Older releases are not patched: upgrade with `latet upgrade`, and
run the latest release on every instance of a Latet server.

## Scope

In scope, for example:

- a value Latet detects that still reaches the provider in clear;
- a surrogate restored outside the session that was shown it;
- the daemon, the plugin bridge or the dashboard reachable from a web page or from another machine;
- credentials written to a log, the history or the vault;
- the vault or the dashboard history readable without the root key;
- the tutorial's sandbox reaching a provider, or your own settings, vault or history;
- on a Latet server: one identity reaching another identity's data, settings or conversations;
- on a Latet server: a space, a dashboard or the administration reached without their credentials, or
  a refused, revoked or expired credential that still gets through;
- on a Latet server: a value, a token or a key in clear where the server keeps it sealed (in
  PostgreSQL or Valkey), or the master key derived from what they hold;
- on a Latet server: a record forged or moved in PostgreSQL or Valkey, by someone who does not hold
  the master key, that the server accepts (a credential, a session, a message between instances, an
  entry of the administration log that still verifies);
- on a Latet server: one identity making the server unusable for the others;
- on a Latet server: a request sent to an address its settings refuse, such as the server's own
  network.

Out of scope:

- what the documentation states Latet does not protect, such as values its detectors do not
  recognize, text in an image that the optional text recognition does not read, PDFs and other
  documents, and the surfaces it does not cover. See
  [What is and isn't protected](https://docs.latet.ai/concepts/protection-scope/) and the
  [threat model](https://docs.latet.ai/security/);
- on a Latet server, the risks it accepts: the names and email addresses of identities, which are in
  clear in PostgreSQL; the rule, the counter and the time of a vault entry, which are in clear there
  too, beside its sealed value and fake; and what someone who can write to the database, without the
  master key, can still do there: write back an older sealed row, which opens again in its own place;
  write back the row of a credential copied before it was deleted (a revoked or rotated token, a used
  one-time code, a revoked administration token), which holds again; set an identity's `disabled`
  back, which enables it again; and swap the keyed hashes of two entries of one identity's vault,
  which makes two of its values share one fake.

---
title: Locked rules
description: A policy your company sets on its Latet server, which no person and no API key can go below.
---

With Latet Enterprise a company sets a **floor** on its [Latet server](../../getting-started/server/): settings no
person and no API key can go below. Each person keeps their own settings and can always add protection. A change that
would go below the floor is refused, saying the setting is set by the company; it is refused, not held for approval,
since the person cannot approve it. A change is refused only for what it itself lowers: settings that were already
below the floor do not stop a change to something else.

## The policy file

The policy is a rules file, the format `latet rules export` writes (see
[Export and import](../../concepts/export-import/)), so a company can start from an export:

```yaml
latet_rules: 1
exceptions: company
profile: max
terms:
  - {value: Project Falcon, type: PROJECT}
  - {value: Northwind Traders, type: CLIENT}
policies: {IBAN: redact}
ocr: auto
```

**What it says is locked, and what it leaves out stays each person's.** With this file nobody can choose a profile
below `max` or stop reading the text in images, Project Falcon and Northwind Traders are always masked, IBANs are
always redacted, and only the company's exceptions count (it sets none); anyone can still add terms, recognizers and
stricter values of their own. To lock a setting at Latet's default, write it in the file.

- **The profile brings what comes with it:** with `profile: max`, the ID numbers of every country, private IP
  addresses, strict data mode and max's rule for images are locked too.
- **Lists are a minimum:** the terms, recognizers, field rules, personal-data types, countries and phone regions in
  the policy, and the files and web addresses that strict data mode covers, are always in force, and each person's
  are added to them. A term, recognizer or field rule of a type the policy allows (`policies: {CLIENT: allow}` and a
  term typed `CLIENT`) is the company's own exception instead: it is not added to anyone's settings, and a person who
  masks that value by a term or a recognizer of their own keeps it masked.
- **The company's recognizers and field rules are used beside each person's:** a person's own recognizer or field
  rule is never put out of use by them. Where a person's recognizer has the name or the type of one of the company's,
  the company's is used under a name or a type of its own; where a person's field rule has the name of one of the
  company's, the two are merged, and the field is masked.
- **Guidance for the local reviewer** in the policy is always given to it, before the person's own.
- **In every project too:** Latet stays on in every project, and a project's own settings never take it below the
  floor.
- **New request members:** `unknown_members: refuse` refuses, for every person and API key, a request member no request
  profile names yet, as Latet did before 0.15.0, a text or a list of texts outside a provider's item still passing on a
  request Latet only checks (Claude Code, Bedrock, Vertex) once the final check has read it ([New request
  members](../../getting-started/upgrading-to-0-11/#new-request-members)); `unknown_members: check` locks nothing, since
  anyone may refuse them.

`latet policy check policy.yaml` reads a file before it is put in place: what it locks, what it leaves to each person,
whose exceptions count, and what in it could not be read. A setting with a mistake in it, or written with no value
(`ocr:` alone), is not read, so it locks nothing. The command makes no connection, and exits with code 0 when all is
well, 1 when something in the file is not read, and 2 when the file cannot be read at all.

## Exceptions

Values and patterns never to mask, secret rules turned off, and what keeps values in clear (a recognizer whose policy
is `allow`, `allow` for a type in `policies`, a field rule with `mask: false`) are the exceptions. The policy's
`exceptions` key says whose count:

- **`personal`** (the default): each person may still mark a value **Not sensitive** for themselves, and keeps their
  own `allow` choices, except for the types of the company's terms that it masks.
- **`company`**: only the exceptions in the policy count. A person's own are refused when they are added, and not used
  where they were there before: their values and patterns never to mask, their secret rules turned off, their
  recognizers that leave what they find in clear, their `allow` for a type the company does not allow, and their field
  rules with `mask: false`. A field rule of their own of a type the company allows (`policies: {URL: allow}` in the
  policy, and a field rule of theirs typed `URL`) is an exception too, since the field's values would be left in clear,
  strict data mode's masking of them included: it is refused when it is added, it is not used where it was there
  before, and **Rules** says how many of theirs are left out so. Where such a type is masked in one of their projects,
  the field rule is kept, and its type is masked wherever it would have left the field in clear. A term of their own of
  such a type is kept, and saved when it is added: it never leaves in clear
  anything the company masks (a company term, what a company recognizer or field rule finds, personal data) nor an
  address's domain, wherever the masking would mask it without their terms. Between their own terms the longest match
  wins, as without a policy. Their identity (their name, user names, e-mail addresses and host names), of a type the
  company allows, is kept the same way: it never leaves in clear what the masking would mask without it. Where their
  account is linked to the company's identity provider (SCIM, or single sign-on) and the company lets names (`PERSON`)
  through, their name is the one the provider gives where their own settings let names through too, in each of their
  projects; otherwise their own name stays in use, and is masked wherever names are masked. Either way, changing it
  from their settings is refused; a computer only fills it in where none is set.

The company's terms of a type it does not allow stay masked, as the company wrote them. They are locked in
the masking itself: a term is masked whatever value never to mask, pattern, recognizer or `allow` policy a person sets,
and nobody can mark one **Not sensitive**. Where a person has allowed a longer text that holds a term, the rest of it
stays as they allowed, and the term is masked. A value of `exceptions` Latet cannot read, or none (`exceptions:`
alone), counts as `company`, the stricter, and `latet policy check` says so.

With `personal`, only the company's terms are protected that way. A person's own exceptions can still leave in clear
what a company recognizer or a company field rule finds: a longer text they allowed that holds it, or a term, a
recognizer or a field rule of theirs of a type they allow. To rule that out, use `exceptions: company`.

Request exceptions (the [exact exceptions](../../getting-started/upgrading-to-0-11/#approving-an-exact-exception) that
let a header, a query parameter or a body member through) follow the same key. With `personal` a person's are their own:
they approve them as without a policy, and an API key's are decided as its other changes are. With `company` they are
the company's: whoever asks for one proposes it, and it waits for the security team on **Company approvals**, whoever
decides on the rest of their changes; their own approval, or that of an API key's controllers, grants none. That holds
for those approved before the policy said `company` too, permanent ones included: from the moment the server applies the
policy they grant nothing, the dashboard shows each as *Not in force: your company's security team decides on request
exceptions, and did not approve this one*, and `latet server status` lists it as `forbidden` among the request
exceptions that grant nothing now, once its space has applied the policy: a space not open on any instance since the
policy changed shows its exceptions there as they were until it is next built, while its requests are already judged by
the new policy. Asked for anew, one waits for the security team; they count again whenever the company no longer decides
on them: when the policy goes back to `personal` or is removed, when the license is removed, or when a license that does
not list locked rules is installed. Without central approvals in the license nobody can approve one: a request exception
asked for waits for an approval that can only reject it (whoever asked can withdraw it, and their own approval is
refused, saying that the security team decides on it), and it grants nothing. So that people can have request exceptions
under `company`, the license needs `approvals`; without it, `company` still locks every other exception. The
administration's save of an API key's settings still approves the exceptions it adds. Under either value an exception
that waits grants nothing and is never refused as going below the policy: what the policy judges is its approval, when
it comes.

## On a Latet server

With [Latet on a server](../../getting-started/server/), an administrator sets the policy from any computer that has
the server's administration token:

```sh
latet policy check policy.yaml
latet server policy set policy.yaml
```

`latet server policy set` reads the file first, and changes nothing if it cannot be read. What in the file it cannot
read is said before anything is sent, and it asks first: a setting with a mistake in it is left out whole, so one
malformed term leaves every term of the file out of the policy. It also asks before it sets a policy that locks fewer
settings than the server's, that lets each person add exceptions again where only the company's counted, or that
replaces a policy it cannot read here; at a terminal of your own it asks, and otherwise `--yes` says you agreed. It sets
the file in place of the policy it compared it with: when someone changes the server's policy in between, on the
**Policy** page or with another `latet server policy set`, the server refuses the set, and the command reads the policy
again, compares again and asks again. An AI agent is refused: setting or removing a policy is for a person. The
administration API's `PUT` refuses a file with a setting left out whole (400, naming the setting), unless the request
also says `"accept_notices": true`. A `PUT` that also names `"etag"`, the policy it replaces as the `GET` gave it
(`null` when none is stored), or a `DELETE` whose body names so the policy it removes, is refused with 409 when
that is no longer the policy stored.

Every person and every API key on the server is raised to the policy: each instance raises every space as soon as it
is told, and looks again every 10 seconds, so an instance that missed the message has the policy within 10 seconds.
The server keeps it in its database, its text sealed with the master key (the terms in it are values), and records each
change in the administration log, with the settings it locks and never a value. `latet server policy show` says when
and by whom it was set, what it sets and whether it is in force, and `latet server policy show --file policy.yaml`
writes its text back, to edit it. `latet server policy remove` takes it away, after asking: it removes the policy
it read, and when someone changes the server's policy in between, the server refuses the removal, and the command
reads the policy again and asks again.

With `roles` in the license, people of the security role set it from the dashboard too
([Sign-in, groups and roles](../sign-in/)): on the **Policy** page they edit the policy's text, then **Save** it or
**Remove** it. A text the server cannot read is refused with the reason, and the policy in force stays. A save names
the policy it replaces: if someone changed the policy since its text was opened, it is refused, and the page shows the
policy as it is now. The policy's text holds the company's terms and values, so while the server asks for the company
sign-in (`company_changes: sso`, the default once single sign-on is set up), reading it and changing it take a session
opened with the company account. Each change is in the administration log, by the person who made it.

**A policy applies on each instance as it is upgraded.** An instance of an earlier release, or one without the
Enterprise Edition, applies no policy. Set the policy once every instance runs this release, or set it again then:
`latet server policy set` and the administration API's `PUT` warn, saying how many live instances do not apply it. See
[Upgrading and adding instances](../../concepts/server-operations/#upgrading-and-adding-instances).

While the policy locks terms (any term in it of a type it does not allow) or says `exceptions: company`, the server
reads each text of an [applications API](../../reference/apps-api/) call on its own, as Latet 0.10 did, where it
otherwise reads a call's texts together: masking stays the same, and a call of many texts takes longer. Synced terms
do the same while any is masked ([Term sync](../term-sync/)).

The shared settings (`latet server config`, `latet server rules`) stay as an administrator sets them: what in them is
below the policy is raised in every space, as a person's own settings are. Setting an API key's settings below the
policy is refused: `latet server config --key ... set` exits with code 2.

## What people see

- **In the dashboard**, a setting the company sets shows a lock and **Set by your company** in **Rules** and in a
  project's rules. A person is told what is locked and how many terms, exceptions and rules there are, for example
  that 2 terms of the company stay masked; never the terms, nor any other value of the company's. On a server,
  **Policy** is for the security role, who read and change the policy's text there, and for auditors, who read what it
  locks; while the license has no `roles`, everyone reads what it locks. Administrators read the policy with
  `latet server policy show`.
- **A refused change names the setting:** `Set by your company, not changed: profile: from max to balanced`. The
  dashboard answers with a conflict (409) and shows the refusal where the change was made, and in the terminal the
  command exits with code 2. A single change that lowers the settings of more than 20 project folders is refused as a
  whole: change fewer folders at a time.
- **Your own settings stay as you set them:** what is below the floor is raised while in use, and your settings keep
  what you wrote, so they apply as written if the policy is removed.
- **`latet server status`** has a line for the policy: when it was set (in UTC), and a warning when it is not in force
  or cannot be read.

## The license

The policy is in force with a Latet Enterprise license that includes locked rules ([Enterprise Edition](../)).
Protection never drops: when the license expires, goes over its users, is paused, or a trial ends, the policy stays in
force exactly as it is. Setting or removing a server's policy needs a license that can still manage the Enterprise
settings: once a trial ends the policy is frozen, and `latet server policy show` still shows it, until a license is
installed.

The policy stops being in force in two cases only: when the license is removed, and when a license that does not list
locked rules is installed. `latet server license remove` and `latet server license install` say so and ask first, and
refuse an AI agent; the server's policy is kept, and it is in force again with a license that lists locked rules.

## The limits

- **Only on a Latet server:** the Enterprise Edition is a server's. Latet on a computer without a server is the
  Community Edition, whatever is installed on it, and it has no policy.
- **A computer connected to a Latet server** is masked by the server, under the server's policy.
- **An instance that cannot read the policy:** a policy that is stored but cannot be read (made by a newer release, or
  sealed with another master key) leaves an instance that read a policy before applying the one it read last: its
  spaces stay on that older floor, which may be lower than the new one, and `latet server status` says the new policy
  cannot be read. An instance that never read a policy, or that has not read the license yet, serves no person and no
  API key until it can: the request is refused with a 503 that says to try again. `latet server policy remove` mends a
  policy that cannot be read; setting it again with a release every instance runs mends it too.
- **The database is trusted:** whoever can write to the server's PostgreSQL can delete the `ee_policy` row, or put an
  older one back. The floor then falls, or goes back to the older policy, on every instance, and no entry of the
  administration log says so. The log's `policy.changed` entries, each with the etag of the policy set, are the record
  to compare the policy in force with (`latet server policy show`); keep that database's write access to the server
  alone.
- **The text in images:** a policy that locks `terms` but not `ocr` and `images` leaves a person free to turn off the
  reading of the text in images, and a company term inside an image then goes unread. Lock `ocr: auto` and `images` too
  for the terms to cover images.
- **An agent pointed straight at an AI provider** is masked by nobody. A server is the company's control point only
  when its network lets the providers be reached from the server alone ([Latet on a server](../../concepts/server/)).

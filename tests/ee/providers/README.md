# Latet Enterprise against real providers

These tests run a Latet server with the Enterprise Edition (two instances on the test PostgreSQL and Valkey) against
real programs in Docker, set up as the documentation says:

- `test_keycloak.py`: single sign-on with Keycloak (`enterprise/sign-in`): groups, roles, a person disabled in
  Keycloak, a sign-in begun on one instance and finished on the other, and a wrong client secret.
- `test_openldap.py`: term sync from OpenLDAP (`enterprise/term-sync`) over LDAPS and STARTTLS, with a read-only
  service account, a term taken out of the directory, plain LDAP and a wrong certificate authority.
- `test_keycloak_ldap.py`: Keycloak in front of the OpenLDAP directory (`enterprise/sign-in`, "Keycloak in front of
  Active Directory", tried with OpenLDAP): LDAP user federation over LDAPS with a group mapper, and sign-ins with the
  directory's passwords and groups. Its two containers share a Docker network of their own, `latet-prov-net`, removed
  with them.
- `test_prometheus.py`: the three term sync alerts of `enterprise/term-sync`, read from the page itself, loaded in
  Prometheus with Server operations' scrape job `latet`, against an instance whose master key lacks the terms' version.

They are opt-in: they run only with `LATET_PROVIDER_TESTS=1`, and are skipped otherwise, in the default suite and in
CI too.

## The images

Pull them by hand first. The tests never pull an image: a test whose image is missing is skipped and says which.

```sh
docker pull quay.io/keycloak/keycloak:26.8.2
docker pull prom/prometheus:v3.16.0
docker pull debian:trixie-slim
```

`debian:trixie-slim` is the base of the OpenLDAP image, which `test_openldap.py` builds from `openldap/Dockerfile` as
`latet-prov-openldap:test`, with `slapd` and `ldap-utils` from Debian's own packages (the build downloads them). Remove
it afterwards with `docker image rm latet-prov-openldap:test`.

## Running them

With the test services up (`uv run python scripts/server-services.py up`; `uv run python scripts/server-services.py
env` prints the variables the tests read):

```sh
LATET_PROVIDER_TESTS=1 uv run pytest tests/ee/providers
```

Latet is given each provider at `127.0.0.1`. `LATET_PROVIDER_HOST=localhost` gives it at `localhost` instead: Windows
resolves that name to `::1` first, where the containers' ports, published on 127.0.0.1 alone, refuse the connection,
and Latet reaches the provider at 127.0.0.1, the next address its lookup checked.

`LATET_PROMETHEUS_PAGE_FOR=1` makes `test_prometheus.py` wait for `LatetTermsNeverLoaded` to fire with the page's own
`for: 5m`, about seven minutes, rather than with a copy of the rules whose only change is `for: 30s`.

Each container is named `latet-prov-<name>`, publishes its ports on 127.0.0.1 alone, and is removed with its volumes
when its tests end. Nothing secret is kept here: the realm file (`keycloak/realm.json`) and the directory
(`openldap/directory.ldif`) hold no password, and the passwords, the client secret, the certificate authority, the
directory's certificate, the metrics token and the master keys are made at each run.

Prometheus reaches the instances' metrics sockets, which listen on 127.0.0.1, at `host.docker.internal`, as Docker
Desktop gives that name to containers: Docker Desktop's own process opens those connections on this computer's loopback.
The test was run with Docker Desktop on Windows; with Docker Engine on Linux, `host.docker.internal` leads to the
bridge's address (`--add-host=host.docker.internal:host-gateway`), where these sockets do not listen.

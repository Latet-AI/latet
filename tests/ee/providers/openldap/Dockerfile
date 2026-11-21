# OpenLDAP from Debian's own packages, for the term sync tests against a real directory (tests/ee/providers/README.md).
# The configuration, the entries and the TLS files are copied into /setup by the test before the container starts:
# nothing secret is in the image.
FROM debian:trixie-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends slapd ldap-utils \
    && rm -rf /var/lib/apt/lists/* /etc/ldap/slapd.d/* /var/lib/ldap/*

COPY start.sh /usr/local/bin/latet-ldap-start

EXPOSE 389 636
ENTRYPOINT ["/bin/sh", "/usr/local/bin/latet-ldap-start"]

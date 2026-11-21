#!/bin/sh
# Builds slapd's configuration and directory from /setup (config.ldif, directory.ldif, tls/), then runs slapd in the
# foreground on ldap:// (STARTTLS), ldaps:// and ldapi:// (root's own socket, for the test's changes).
set -eu
install -d -o openldap -g openldap -m 0750 /etc/ldap/slapd.d /var/lib/ldap /etc/ldap/tls
install -d -o openldap -g openldap -m 0755 /run/slapd
install -o openldap -g openldap -m 0644 /setup/tls/ca.pem /setup/tls/server.pem /etc/ldap/tls/
install -o openldap -g openldap -m 0600 /setup/tls/server-key.pem /etc/ldap/tls/
slapadd -n 0 -F /etc/ldap/slapd.d -l /setup/config.ldif
slapadd -n 1 -F /etc/ldap/slapd.d -l /setup/directory.ldif
chown -R openldap:openldap /etc/ldap/slapd.d /var/lib/ldap
rm -rf /setup
exec slapd -d 256 -u openldap -g openldap -F /etc/ldap/slapd.d -h "ldap:/// ldaps:/// ldapi:///"

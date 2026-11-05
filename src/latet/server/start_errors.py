"""What stops a Latet server's start, in fixed words that name no value: the schema, the database, the master key,
Valkey, a socket, the certificate, a setting refused. `latet server run` prints them for one instance, and a worker of
`latet server run --workers` writes them in the server's log (``latet.server.workers``), as the server's log writes them
when it is given one (``latet.server.logs``). They are kept here, in a module that imports nothing but Python's own, so
that naming them never fails where the server's own modules cannot be imported (numpy, psycopg or redis missing or
broken): the modules that raise them import them from here, and each place one is made is read for its words
(tests/unit/test_logging_calls.py)."""


class SchemaMismatch(RuntimeError):
    """The database's schema is not this Latet's: an instance refuses to start, and says what to run."""


class DatabaseError(RuntimeError):
    """PostgreSQL could not be reached, or refused or failed what a start asked of it. Its words name the driver's error
    by its type and never quote it: libpq's words hold hosts and users, and an address it cannot read."""


class MasterKeyError(RuntimeError):
    """No master key, or not one: the server says how to give it."""


class ValkeyUnavailable(RuntimeError):
    """Valkey refused the instance, or did not answer in time, at its start: the instance does not start. Its words
    name the setting and the driver's error by its type, never the address or its password."""


class CannotListen(RuntimeError):
    """A socket of the instance, its own or the metrics', cannot be bound: the instance does not start. Its words name
    the settings and why, never the address."""


class CertificateUnusable(ValueError):
    """The instance's certificate and key cannot be used: the instance does not start. Its words name the settings and
    the error's type, never what the files hold."""


class SettingRefused(ValueError):
    """A setting the server refuses as it starts (the database's address, a secret's file, the metrics token's file,
    the text recognitions' bounds): the instance does not start. Its words name the setting, and counts, never what it
    holds."""

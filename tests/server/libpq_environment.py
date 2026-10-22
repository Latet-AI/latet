"""The environment of a computer that has been used with psql and gone wrong: a hostile value for every environment
variable that libpq reads for one of its connection options. None of it may reach the connection of a Latet server,
whose address is the whole of where it connects and as whom. (The variables that libpq reads for no option, such as
PGTZ and PGSYSCONFDIR, are not here.)"""

from pathlib import Path

from psycopg import pq

# One value for each option, never libpq's own default for it. The paths are made in the test's folder.
VALUES = {
    "user": "env-user", "password": "env-pw", "channel_binding": "disable", "connect_timeout": "777",
    "dbname": "env-db", "host": "127.0.0.3", "hostaddr": "127.0.0.2", "port": "1", "client_encoding": "LATIN1",
    "options": "-c search_path=env", "application_name": "env-app", "sslmode": "disable", "sslnegotiation": "direct",
    "sslcompression": "1", "sslcertmode": "require", "sslsni": "0", "requirepeer": "env-peer",
    "require_auth": "scram-sha-256", "min_protocol_version": "3.2", "max_protocol_version": "3.2",
    "ssl_min_protocol_version": "TLSv1", "ssl_max_protocol_version": "TLSv1.2", "krbsrvname": "env-krb",
    "gsslib": "gssapi", "gssdelegation": "1", "target_session_attrs": "read-write", "load_balance_hosts": "random",
}  # fmt: skip
FILES = ("passfile", "sslcert", "sslkey", "sslrootcert", "sslcrl")  # files of the test's folder
GSS = ("prefer", "disable")  # gssencmode: the first of them that is not this libpq's own default
PASSWORD_IN_THE_FILE = "pw-from-the-password-file"


def options() -> dict[str, pq.ConninfoOption]:
    """Every option of this libpq that has an environment variable, by its keyword."""
    return {o.keyword.decode(): o for o in pq.Conninfo.get_defaults() if o.envvar}


def hostile(folder: Path) -> dict[str, str]:
    """``{variable: value}`` for every option of this libpq that has a variable, PGSERVICE included. Fails for an
    option that has no value here: a libpq with a new variable needs one before its tests can say it is kept out."""
    made: dict[str, str] = {}
    for keyword, option in options().items():
        compiled = option.compiled.decode() if option.compiled is not None else None
        if keyword == "service":
            value = "latet-env-service"
        elif keyword == "gssencmode":
            value = next(candidate for candidate in GSS if candidate != compiled)
        elif keyword == "sslcrldir":
            (folder / "crls").mkdir(exist_ok=True)
            value = str(folder / "crls")
        elif keyword in FILES:
            path = folder / f"env-{keyword}"
            path.write_bytes(f"*:*:*:*:{PASSWORD_IN_THE_FILE}\n".encode())
            path.chmod(0o600)  # libpq ignores a password file that others can read
            value = str(path)
        else:
            value = VALUES[keyword]  # a KeyError names the option that has none
        assert value != compiled, f"{keyword}: a hostile value is not libpq's own default"
        made[option.envvar.decode()] = value
    return made

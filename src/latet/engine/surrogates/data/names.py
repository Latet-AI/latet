"""Word lists for realistic surrogates: Italian and English names, towns and street words, fictional companies.

A fake person, user name, street or town is drawn from the lists of its original's language (``ITALIAN`` or
``ENGLISH``, which ``generators.language`` picks): an Italian name gets an Italian one, an English name an
English one, and an address keeps the shape of its country.
"""

from dataclasses import dataclass

ITALIAN_FIRST_NAMES = (
    "Luca",
    "Marco",
    "Giulia",
    "Francesca",
    "Alessandro",
    "Chiara",
    "Matteo",
    "Sara",
    "Andrea",
    "Elena",
    "Davide",
    "Valentina",
    "Simone",
    "Martina",
    "Federico",
    "Laura",
    "Stefano",
    "Silvia",
    "Paolo",
    "Anna",
    "Giorgio",
    "Elisa",
    "Roberto",
    "Marta",
    "Fabio",
    "Alessia",
    "Riccardo",
    "Ilaria",
    "Daniele",
    "Serena",
    "Lorenzo",
    "Beatrice",
    "Tommaso",
    "Camilla",
    "Nicola",
    "Federica",
    "Emanuele",
    "Roberta",
    "Pietro",
    "Monica",
    "Mario",
    "Giuseppe",
    "Antonio",
    "Maria",
    "Lucia",
)

ENGLISH_FIRST_NAMES = (
    "James",
    "Olivia",
    "William",
    "Emma",
    "Oliver",
    "Sophia",
    "Henry",
    "Amelia",
    "Lucas",
    "Isabella",
    "Benjamin",
    "Mia",
    "Samuel",
    "Charlotte",
    "Daniel",
    "Harper",
    "Jack",
    "Evelyn",
    "Thomas",
    "Abigail",
    "Michael",
    "Emily",
    "David",
    "Grace",
    "Joseph",
    "Chloe",
    "Ethan",
    "Lily",
    "Noah",
    "Hannah",
    "Laura",
    "Sarah",
    "John",
    "Mary",
    "Paul",
)

FIRST_NAMES = ITALIAN_FIRST_NAMES + ENGLISH_FIRST_NAMES

ITALIAN_LAST_NAMES = (
    "Bianchi",
    "Romano",
    "Colombo",
    "Ricci",
    "Marino",
    "Greco",
    "Bruno",
    "Gallo",
    "Conti",
    "DeLuca",
    "Mancini",
    "Costa",
    "Giordano",
    "Rizzo",
    "Lombardi",
    "Moretti",
    "Barbieri",
    "Fontana",
    "Santoro",
    "Mariani",
    "Rinaldi",
    "Caruso",
    "Ferrara",
    "Galli",
    "Martini",
    "Leone",
    "Longo",
    "Gentile",
    "Martinelli",
    "Vitale",
    "Serra",
    "Coppola",
    "DeSantis",
    "Marchetti",
    "Parisi",
    "Villa",
    "Conte",
    "Ferri",
    "Fabbri",
    "Bianco",
    "Rossi",
    "Russo",
    "Esposito",
    "Ferrari",
)

ENGLISH_LAST_NAMES = (
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Miller",
    "Davis",
    "Wilson",
    "Anderson",
    "Taylor",
    "Thomas",
    "Moore",
    "Martin",
    "Jackson",
    "Thompson",
    "White",
    "Harris",
    "Clark",
    "Lewis",
    "Walker",
    "Hall",
    "Young",
    "Allen",
    "King",
    "Wright",
    "Scott",
    "Green",
    "Baker",
    "Adams",
    "Nelson",
    "Bennett",
    "Carter",
    "Mitchell",
    "Turner",
)

LAST_NAMES = ITALIAN_LAST_NAMES + ENGLISH_LAST_NAMES

COMPANIES = (
    "Contoso",
    "Fabrikam",
    "Northwind",
    "Tailspin",
    "Litware",
    "Proseware",
    "Woodgrove",
    "Coho",
    "Lucerne",
    "Wingtip",
    "Adatum",
    "Alpine",
    "Trey",
    "Margie",
    "Humongous",
    "Wideworld",
    "Southridge",
    "Blueyonder",
    "Graphicdesign",
    "Relecloud",
    "Fourthcoffee",
    "Lamna",
    "Tasmanian",
    "Consolidated",
    "Liberty",
    "Woodford",
)

# Towns for city surrogates: real, common, unremarkable (a collision with the text is rejected).
ITALIAN_CITIES = (
    "Lecco",
    "Rovigo",
    "Cremona",
    "Pavia",
    "Arezzo",
    "Lucca",
    "Asti",
    "Teramo",
    "Matera",
    "Sondrio",
    "Biella",
    "Rieti",
)

ENGLISH_CITIES = (
    "Bristol",
    "Leeds",
    "Dayton",
    "Tulsa",
    "Reading",
    "Derby",
    "Exeter",
    "Salem",
    "Akron",
    "Fresno",
    "Boise",
    "Peoria",
)

CITIES = ITALIAN_CITIES + ENGLISH_CITIES

# State and province codes, as an address writes them: "Springfield, IL", "00186 Roma RM".
US_STATES = (
    *("AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY"),
    *("LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND"),
    *("OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"),
)
IT_PROVINCES = (
    *("AG", "AL", "AN", "AO", "AP", "AQ", "AR", "AT", "AV", "BA", "BG", "BI", "BL", "BN", "BO", "BR", "BS"),
    *("BT", "BZ", "CA", "CB", "CE", "CH", "CL", "CN", "CO", "CR", "CS", "CT", "CZ", "EN", "FC", "FE", "FG"),
    *("FI", "FM", "FR", "GE", "GO", "GR", "IM", "IS", "KR", "LC", "LE", "LI", "LO", "LT", "LU", "MB", "MC"),
    *("ME", "MI", "MN", "MO", "MS", "MT", "NA", "NO", "NU", "OR", "PA", "PC", "PD", "PE", "PG", "PI", "PN"),
    *("PO", "PR", "PT", "PU", "PV", "PZ", "RA", "RC", "RE", "RG", "RI", "RM", "RN", "RO", "SA", "SI", "SO"),
    *("SP", "SR", "SS", "SU", "SV", "TA", "TE", "TN", "TO", "TP", "TR", "TS", "TV", "UD", "VA", "VB", "VC"),
    *("VE", "VI", "VR", "VT", "VV"),
)

# Words naming the kind of street: kept as they are, so the address keeps its shape and language.
ITALIAN_STREET_TYPES = frozenset(
    "via viale piazza piazzale corso largo vicolo strada contrada borgo lungomare lungotevere località".split()  # noqa: SIM905
)
ENGLISH_STREET_TYPES = frozenset(
    """street st road rd avenue ave lane ln drive dr boulevard blvd way place pl court ct square sq
    terrace""".split()  # noqa: SIM905
)
STREET_TYPES = (
    ITALIAN_STREET_TYPES
    | ENGLISH_STREET_TYPES
    | frozenset(
        """straße strasse str weg platz allee gasse rue chemin impasse quai calle avenida plaza paseo
        camino""".split()  # noqa: SIM905
    )
)
APOSTROPHES = "'" + chr(0x2019)  # straight and typographic apostrophes, as in D'Amico
# What never begins or ends a name: an apostrophe, a dash, and every quote mark around one (console.table's 'Ann Ross',
# “Ann Ross”, «Ann Ross»).
NAME_EDGES = (
    APOSTROPHES
    + "-"
    + '"'
    + "\N{LEFT SINGLE QUOTATION MARK}\N{LEFT DOUBLE QUOTATION MARK}"
    + (
        "\N{RIGHT DOUBLE QUOTATION MARK}\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}"
        "\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}"
    )
)
# Particles inside street names ("Piazza della Signoria"), kept too.
STREET_PARTICLES = frozenset("de del della dei di da la le du des van von der of the".split())  # noqa: SIM905
# The details of an address after its street ("scala C", "Apt 4B"): kept, as the street type is.
ADDRESS_WORDS = frozenset(
    "scala sc interno int piano palazzina edificio apt apartment flat suite ste unit floor fl building bldg".split()  # noqa: SIM905
)


@dataclass(frozen=True)
class Names:
    """The lists a fake of one language is drawn from."""

    first: tuple[str, ...]
    last: tuple[str, ...]
    cities: tuple[str, ...]
    regions: tuple[str, ...]
    street_types: frozenset[str]

    def known(self) -> frozenset[str]:
        """Its names and towns, in lower case: an original among them is of this language."""
        return frozenset(w.lower() for w in (*self.first, *self.last, *self.cities))


ITALIAN = Names(ITALIAN_FIRST_NAMES, ITALIAN_LAST_NAMES, ITALIAN_CITIES, IT_PROVINCES, ITALIAN_STREET_TYPES)
ENGLISH = Names(ENGLISH_FIRST_NAMES, ENGLISH_LAST_NAMES, ENGLISH_CITIES, US_STATES, ENGLISH_STREET_TYPES)

# Public mailbox providers: the domain identifies nobody, so it is kept as is.
PUBLIC_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "hotmail.it",
        "live.com",
        "live.it",
        "msn.com",
        "yahoo.com",
        "yahoo.it",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "gmx.com",
        "gmx.net",
        "gmx.de",
        "web.de",
        "libero.it",
        "virgilio.it",
        "tiscali.it",
        "alice.it",
        "tim.it",
        "tin.it",
        "fastwebnet.it",
        "email.it",
        "inwind.it",
        "zoho.com",
        "yandex.com",
        "mail.com",
        "fastmail.com",
        "hey.com",
        "tutanota.com",
        "tuta.io",
    }
)

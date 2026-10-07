"""Central configuration for the OrseBusiness prospect pipeline (dry-run tooling)."""

USER_AGENT = (
    "OrseBusinessProspectBot/0.1 "
    "(+https://orsebusiness.vercel.app/nl; contact: orsebusiness@hotmail.com)"
)

# Politeness limits for fetching business websites.
REQUEST_TIMEOUT_S = 10
MAX_BYTES_PER_PAGE = 1_500_000
MAX_PAGES_PER_SITE = 4          # homepage + up to 3 internal pages
MAX_LEGAL_PAGES = 2             # extra legal/company-information pages, only when no clear legal form was found
DELAY_BETWEEN_REQUESTS_S = 1.2  # sequential; never parallel against one host

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# OSM craft tag -> our service category.
CRAFT_CATEGORY = {
    "plumber": "plumber",
    "electrician": "electrician",
    "hvac": "hvac",
    "heating_engineer": "hvac",
}

# Only generic role mailboxes are ever used. Anything else (likely a person) is ignored.
ROLE_LOCAL_PARTS = {
    "info", "contact", "service", "planning", "administratie", "kantoor", "mail",
    "offerte", "offertes", "verkoop", "support", "receptie", "algemeen", "hallo",
    "hello", "werkplaats", "storing", "storingen", "sales", "office", "post",
    "klantenservice", "afspraak", "afspraken", "installatie", "techniek",
}

# Known online-booking tools (competitors / already solved).
BOOKING_TOOL_MARKERS = [
    "calendly.com", "cal.com/", "salonized", "planyo", "simplybook", "timify",
    "acuityscheduling", "setmore", "bookingkit", "youcanbook.me", "afspraakplanner",
    "onlineafspraken.nl", "zoho.com/bookings", "booking-widget", "tidycal",
]
BOOKING_PHRASES = [
    "online afspraak maken", "online een afspraak", "plan direct", "boek online",
    "online boeken", "direct inplannen", "afspraak inplannen", "kies een tijdstip",
    "kies een datum", "plan zelf", "online plannen",
]

EMERGENCY_PHRASES = ["24/7", "24 uur per dag", "24 uur bereikbaar", "spoedservice",
                     "spoed", "storingsdienst", "dag en nacht"]

QUALIFY_THRESHOLD = 60

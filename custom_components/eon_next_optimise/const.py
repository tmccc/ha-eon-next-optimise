"""Fixed production endpoints and conservative read-only polling defaults."""

from datetime import timedelta

DOMAIN = "eon_next_optimise"
NAME = "E.ON Next Optimise"
GRAPHQL_URL = "https://backend.production.eon-next.amber-international.com.au/graphql"
REGION = "eu-west-2"
USER_POOL_ID = "eu-west-2_qbjjnayWT"
CLIENT_ID = "5tjc4igm29bcmsal7eridtuii6"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_SITE_IDS = "site_ids"
SECTIONS = ("prices", "live", "plan", "status", "ev", "ev_plan", "ev_history")
DEFAULT_INTERVALS = {
    "prices": 300,
    "live": 60,
    "plan": 300,
    "status": 900,
    "ev": 60,
    "ev_plan": 300,
    "ev_history": 900,
}
MIN_INTERVALS = {
    "prices": 60,
    "live": 30,
    "plan": 60,
    "status": 60,
    "ev": 60,
    "ev_plan": 60,
    "ev_history": 300,
}
MAX_INTERVAL = 3600
MAX_AGES = {
    "prices": timedelta(minutes=15),
    "live": timedelta(minutes=3),
    "plan": timedelta(minutes=15),
    "status": timedelta(minutes=45),
    "ev": timedelta(minutes=3),
    "ev_plan": timedelta(minutes=15),
    "ev_history": timedelta(minutes=45),
}
MAX_AGE = MAX_AGES["prices"]
UPDATE_INTERVAL = timedelta(seconds=DEFAULT_INTERVALS["prices"])
HTTP_TIMEOUT = 30
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TOKEN_REFRESH_MARGIN = 90
MAX_RETRY_AFTER = 3600

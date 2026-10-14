"""Accept the pilot's fake Mozilla account in MLPA, so Firefox needs no real sign-in.

The ``mlpa`` service in ``docker-compose.yml`` puts this directory on MLPA's
PYTHONPATH and sets ``PILOT_FXA_TOKEN`` and ``PILOT_FXA_UID``; ``REAL_ACCOUNT=1``
turns it off. The token is the one ``run.sh up`` caches in the
pilot profile's ``signedInUser.json``; MLPA's FxA check accepts exactly that
token as that account and verifies every other token against Mozilla accounts
as usual. MLPA's own code is unchanged.
"""

import os

_TOKEN = os.environ.get("PILOT_FXA_TOKEN")
_UID = os.environ.get("PILOT_FXA_UID")

if _TOKEN and _UID and os.environ.get("REAL_ACCOUNT") != "1":
    try:
        import fxa.oauth
    except ImportError:  # a process in this environment that never verifies tokens
        pass
    else:
        _verify_token = fxa.oauth.Client.verify_token

        def verify_token(self, token, scope=None, include_verification_source=False):
            if token != _TOKEN:
                return _verify_token(self, token, scope=scope, include_verification_source=include_verification_source)
            profile = {
                "user": _UID,
                "client_id": "5882386c6d801776",
                "scope": ["profile:uid", "https://identity.mozilla.com/apps/smartwindow"],
                "generation": 0,
            }
            if include_verification_source:
                profile["verification_source"] = "pilot"
            return profile

        fxa.oauth.Client.verify_token = verify_token

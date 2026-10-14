// Added by `./run.sh up` unless REAL_ACCOUNT=1: the profile is signed in to
// a fake Mozilla account (signedInUser.json), and these point Firefox's account
// servers at a closed port. Firefox would otherwise check the fake session
// against Mozilla accounts, get told the account does not exist, and sign out.
user_pref("identity.fxaccounts.autoconfig.uri", "");
user_pref("identity.fxaccounts.remote.root", "http://127.0.0.1:9/");
user_pref("identity.fxaccounts.auth.uri", "http://127.0.0.1:9/v1");
user_pref("identity.fxaccounts.remote.oauth.uri", "http://127.0.0.1:9/v1");
user_pref("identity.fxaccounts.remote.profile.uri", "http://127.0.0.1:9/v1");
user_pref("identity.fxaccounts.remote.pairing.uri", "ws://127.0.0.1:9");
user_pref("identity.sync.tokenserver.uri", "http://127.0.0.1:9/1.0/sync/1.5");
user_pref("browser.smartwindow.firstrun.hasCompleted", true);

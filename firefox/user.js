// Smart Window against the local pilot MLPA (./run.sh up).
// `./run.sh up` copies this into a fresh profile signed in to a fake
// Mozilla account; with REAL_ACCOUNT=1, sign in to a real one instead (Smart
// Window sends its FxA token to MLPA).
user_pref("browser.smartwindow.enabled", true);
user_pref("browser.smartwindow.tos.consentTime", 1759600000);
user_pref("browser.smartwindow.endpoint", "http://127.0.0.1:8080/v1");
// Needs the Firefox branch smartwindow-mlpa-pilot-endpoint; without it an
// overridden endpoint turns the answers (citations) path off.
user_pref("browser.smartwindow.endpoint.isMLPA", true);
user_pref("browser.smartwindow.searchQuery.endpointURL", "http://127.0.0.1:8080/v1/search");
user_pref("browser.smartwindow.searchTheWebAnswers", true);
user_pref("browser.smartwindow.log", "Debug");
// Chat on Mistral (choice "3", mistral-small-2603), the one chat model the rig
// serves while Vertex is left out. "1" and "2" are Gemini and Qwen on Vertex.
user_pref("browser.smartwindow.firstrun.modelChoice", "3");

# Contributor instructions

Keep changes focused. Preserve account isolation, shared history and the loopback-only HTTP boundary. Never commit credentials, native user histories, runtime caches or private diagnostics. Verify behavior in a temporary HOME with synthetic credentials, never by removing a real account, requesting models or redeeming credits.

Run `python3 -m unittest discover -p 'test_*.py' -q` and `node --check web/*.js` individually. Account labels use the shared validator and discovery helpers; do not reintroduce A–H/A–B limits. Preserve identity-specific unknown credit outcomes across removal and re-addition. Public source changes remain GPL-3.0-only with upstream attribution.

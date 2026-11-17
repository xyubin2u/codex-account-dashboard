"""Reproducible patches against the pinned 0.8.10 cx runtime (no setup/install)."""
from pathlib import Path
import sys
p=Path(sys.argv[1]); text=p.read_text()
def replace(old,new):
 global text
 if text.count(old)!=1: raise SystemExit('Patch anchor count mismatch: '+old[:80])
 text=text.replace(old,new,1)
replace('function accountDisplayName(account) {', 'function accountDisplayName(account) {\n  if (process.env.CX_LOCAL_SAFE_RESUME === "1") return account.name;')
replace('  if (accountAccessTokenExpired(auth)) {', '  if (process.env.CX_LOCAL_SAFE_RESUME !== "1" && accountAccessTokenExpired(auth)) {')
replace('function rawUsedPercent(bucket) {', '''function fiveHourQuotaBucket(limit) {
  return [limit?.primary, limit?.secondary].find(
    bucket => rateLimitWindowDurationMins(bucket) === 300
  ) || null;
}

function rawUsedPercent(bucket) {''')
replace('  const weekly = weeklyQuotaBucket(result.limits);\n  if (!weekly) {', '''  const shortWindow = fiveHourQuotaBucket(result.limits);
  if (shortWindow && rawUsedPercent(shortWindow) >= 100) return "5h>=100%";
  const weekly = weeklyQuotaBucket(result.limits);
  if (!weekly) {''')
replace('    console.error(`  weekly ${remaining}  reset ${formatResetTime(rawQuotaResetMs(weekly))}`);', '''    const shortWindow = fiveHourQuotaBucket(result.limits);
    const shortUsed = rawUsedPercent(shortWindow);
    console.error(`  5h     ${formatProgressPercent(Number.isFinite(shortUsed) ? 100 - shortUsed : null)}  reset ${formatResetTime(rawQuotaResetMs(shortWindow))}`);
    console.error(`  weekly ${remaining}  reset ${formatResetTime(rawQuotaResetMs(weekly))}`);''')
replace('      if (stat.mtimeMs < minMtimeMs) {', '''      if (options.sessionId && readSessionMeta(target).id !== options.sessionId) continue;
      if (stat.mtimeMs < minMtimeMs) {''')
replace('  const retryAnalysis = retryTargetFromSession(analysis, sessionState);', '''  const retryAnalysis = options.sessionId
    ? { ...analysis, resumeTargetArgs: [options.sessionId] }
    : retryTargetFromSession(analysis, sessionState);''')
replace('function runCodexMonitored(account, commandArgs, logDir) {', '''// Local integration: only this process's log or an explicit UUID identifies its
// conversation. Shared sessions' newest mtime is never evidence of ownership.
function ownedSessionId(logFile, codexArgs) {
  let log = "";
  try { log = fs.readFileSync(logFile, "utf8"); } catch {}
  const ids = new Set();
  for (const match of log.matchAll(/session_loop\\{thread_id[=:]"?([0-9a-f-]{36})/gi)) {
    if (isUuidLike(match[1])) ids.add(match[1]);
  }
  if (ids.size === 1) return [...ids][0];
  if (ids.size > 1) return null;
  const analysis = analyzeCodexArgs(codexArgs);
  const explicit = analysis.resumeTargetArgs?.[0];
  return isUuidLike(explicit) ? explicit : null;
}

function runCodexMonitored(account, commandArgs, logDir) {''')
replace('      resolve(result);\n    }\n\n    function scanLog()', '''      result.sessionId = ownedSessionId(logFile, commandArgs);
      resolve(result);
    }

    child.on("error", () => finish({ code: 1, rateLimited: false }));

    function scanLog()''')
replace('if (!settled && !child.killed) {', 'if (!settled && child.exitCode === null && child.signalCode === null) {')
replace('                }, 3000);', '                }, 5000).unref();')
replace('    const retryPlan = retryPlanAfterRateLimit(codexArgs, selected.account.home, { minMtimeMs: runStartedAt });', '''    if (process.env.CX_LOCAL_SAFE_RESUME === "1" && !result.sessionId) {
      console.error("[cx-auto] Cannot identify this process's exact session; stopped without resuming another window. Use codex-switch AUTO <session-id>.");
      return 1;
    }
    const retryPlan = retryPlanAfterRateLimit(codexArgs, selected.account.home, {
      minMtimeMs: result.sessionId ? 0 : runStartedAt,
      sessionId: result.sessionId,
    });
    if (process.env.CX_LOCAL_SAFE_RESUME === "1" && !retryPlan.session) {
      console.error("[cx-auto] Exact session history is unavailable; automatic replay stopped.");
      return 1;
    }''')
replace('  accountAccessTokenExpired,\n', '  ownedSessionId,\n  fiveHourQuotaBucket,\n  accountAccessTokenExpired,\n')
p.write_text(text)

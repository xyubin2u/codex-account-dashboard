"""Apply after the five existing private-runtime patch scripts."""
from pathlib import Path
import sys

p = Path(sys.argv[1])
text = p.read_text()


def replace(old, new):
    global text
    if text.count(old) != 1:
        raise SystemExit('Capacity patch anchor mismatch: '+old[:90])
    text = text.replace(old, new, 1)


replace('const { spawn } = require("child_process");', 'const { spawn, spawnSync } = require("child_process");')
replace('function logChunkHasUsageLimit(chunk) {', '''const modelCapacityReasonPattern = /^Selected model is at capacity(?:[.!]|$)/i;

function isModelCapacityLogLine(line) {
  const trimmed = line.trim();
  if (trimmed.includes("ToolCall:") || trimmed.includes("codex_core::stream_events_utils") || nonAccountRateLimitPattern.test(trimmed)) return false;
  if (/^■\\s*/.test(trimmed)) return modelCapacityReasonPattern.test(trimmed.replace(/^■\\s*/, ""));
  const error = trimmed.match(/\\bTurn error:\\s*(.*)$/i);
  return Boolean(error && logLevelLinePattern.test(trimmed) && modelCapacityReasonPattern.test(error[1]));
}

function logChunkHasUsageLimit(chunk) {''')
replace('  if (ids.size > 1) return null;', '''  if (ids.size > 1) {
    // Subagents share this log. Resolve only IDs observed here and require a
    // single persisted CLI root; multiple roots remain ambiguous and stop.
    const base = process.env.CODEX_MULTI_BASE;
    if (!base) return null;
    const owner = spawnSync("python3", [path.resolve(__dirname, "../../..", "session_owner.py"), base, JSON.stringify([...ids])],
      { encoding: "utf8", timeout: 3000 });
    const id = owner.status === 0 ? owner.stdout.trim() : "";
    return isUuidLike(id) && ids.has(id) ? id : null;
  }''')
replace('            if (rateLimitReasonPattern.test(message) || /usage_limit_reached/.test(message)) rateLimited = true;', '''            if (rateLimitReasonPattern.test(message) || /usage_limit_reached/.test(message)) rateLimited = true;
            if (!capacityLimited && modelCapacityReasonPattern.test(message)) {
              capacityLimited = true;
              console.error("\\n[cx-auto] model capacity unavailable; switching account (quota not marked exhausted)...");
            }''')
replace('    let rateLimited = false;', '    let rateLimited = false;\n    let capacityLimited = false;')
replace('      result.sessionId = jsonSessionId || ownedSessionId(logFile, commandArgs);', '      result.capacityLimited = capacityLimited;\n      result.sessionId = jsonSessionId || ownedSessionId(logFile, commandArgs);')
replace('while (offset < stat.size && !rateLimited)', 'while (offset < stat.size && !rateLimited && !capacityLimited)')
replace('''            if (lines.some(isUsageLimitLogLine) || isUsageLimitLogLine(pendingLogLine)) {
              rateLimited = true;
              console.error("\\n[cx-auto] usage limit detected; switching account...");''', '''            const usageHit = lines.some(isUsageLimitLogLine) || isUsageLimitLogLine(pendingLogLine);
            const capacityHit = lines.some(isModelCapacityLogLine) || isModelCapacityLogLine(pendingLogLine);
            if (usageHit || capacityHit) {
              rateLimited = usageHit;
              capacityLimited = !usageHit && capacityHit;
              console.error(capacityLimited
                ? "\\n[cx-auto] model capacity unavailable; switching account (quota not marked exhausted)..."
                : "\\n[cx-auto] usage limit detected; switching account...");''')
replace('    if (!result.rateLimited) {', '    if (!result.rateLimited && !result.capacityLimited) {')
replace('The previous account hit a usage limit before completing the turn;', 'The previous attempt was interrupted before completing the turn;')
replace('  isUsageLimitLogLine,\n', '  isUsageLimitLogLine,\n  isModelCapacityLogLine,\n')
p.write_text(text)

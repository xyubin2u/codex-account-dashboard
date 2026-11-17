"""Reapply AUTO diagnostic and terminal cleanup after the capacity patch."""
from pathlib import Path
import sys
p = Path(sys.argv[1])
text = p.read_text()
old = 'function restoreTerminal() {\n  if (process.stdout.isTTY) {\n    process.stdout.write("\\x1b[?1049l\\x1b[?25h\\x1b[0m\\n");\n  }\n}\n'
new = 'function restoreTerminal() {\n  if (process.stdout.isTTY) {\n    process.stdout.write("\\x1b[?1000l\\x1b[?1002l\\x1b[?1003l\\x1b[?1006l\\x1b[?1015l\\x1b[?1049l\\x1b[?25h\\x1b[0m\\n");\n  }\n}\n'
if text.count(old) != 1: raise SystemExit("AUTO diagnostic patch anchor mismatch")
text = text.replace(old, new, 1)
old = '  const { autoMaxSwitches } = getRuntimeConfig();\n  const exhausted = new Set();\n  let codexArgs = [...initialArgs];\n  let pendingRetrySession = null;\n'
new = '  const { autoMaxSwitches } = getRuntimeConfig();\n  const exhausted = new Set();\n  const attemptedReasons = new Map();\n  let codexArgs = [...initialArgs];\n  let pendingRetrySession = null;\n'
if text.count(old) != 1: raise SystemExit("AUTO diagnostic patch anchor mismatch")
text = text.replace(old, new, 1)
old = '    if (!selected || !isUsable(selected)) {\n      printStatus(results);\n      console.error("[cx-auto] All candidate accounts are exhausted or unavailable.");\n      return 1;\n    }\n'
new = '    if (!selected || !isUsable(selected)) {\n      printStatus(results);\n      console.error("[cx-auto] No remaining untried candidate accounts are available.");\n      if (attemptedReasons.size) {\n        console.error("[cx-auto] Accounts skipped for this run: " + [...attemptedReasons].map(([name, reason]) => `${name}: ${reason}`).join(", "));\n        console.error("[cx-auto] A new AUTO run checks all accounts again. To continue the same session, use codex-switch auto <session-id>.");\n      }\n      return 1;\n    }\n'
if text.count(old) != 1: raise SystemExit("AUTO diagnostic patch anchor mismatch")
text = text.replace(old, new, 1)
old = '    restoreTerminal();\n    exhausted.add(selected.account.name);\n    switches += 1;\n\n'
new = '    restoreTerminal();\n    exhausted.add(selected.account.name);\n    attemptedReasons.set(selected.account.name, result.capacityLimited ? "model capacity" : "usage limit");\n    switches += 1;\n\n'
if text.count(old) != 1: raise SystemExit("AUTO diagnostic patch anchor mismatch")
text = text.replace(old, new, 1)
old = '\nmodule.exports = {\n  ownedSessionId,\n  fiveHourQuotaBucket,\n'
new = '\nmodule.exports = {\n  restoreTerminal,\n  ownedSessionId,\n  fiveHourQuotaBucket,\n'
if text.count(old) != 1: raise SystemExit("AUTO diagnostic patch anchor mismatch")
text = text.replace(old, new, 1)
p.write_text(text)

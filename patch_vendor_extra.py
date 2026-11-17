from pathlib import Path
import sys
p=Path(sys.argv[1]); s=p.read_text()
def patch(a,b):
 global s
 assert s.count(a)==1,a[:60]
 s=s.replace(a,b,1)
patch('let activeChild = null;', 'let activeChild = null;\nconst probeChildren = new Set();')
patch('    let buffer = "";\n    let stderr = "";', '    probeChildren.add(child);\n    child.on("exit", () => probeChildren.delete(child));\n    let buffer = "";\n    let stderr = "";')
patch('      if (activeChild && !activeChild.killed) {', '      for (const probe of probeChildren) { if (!probe.killed) probe.kill(signal); }\n      if (activeChild && !activeChild.killed) {')
patch('      if (attempt < limitRetries && isRetryableRateLimitError(error)) {','      if (!terminating && attempt < limitRetries && isRetryableRateLimitError(error)) {')
patch('    const results = await probeAccounts(exhausted);\n    const selected = selectResult(results);','    const results = await probeAccounts(exhausted);\n    if (terminating) return 130;\n    const selected = selectResult(results);')
patch('    const child = spawn("codex", commandArgs, { env, stdio: "inherit" });\n    activeChild = child;\n\n    const logFile', '''    const captureJson = analyzeCodexArgs(commandArgs).mode === "exec" && hasOption(commandArgs, "--json");
    const child = spawn("codex", commandArgs, { env, stdio: captureJson ? ["inherit", "pipe", "inherit"] : "inherit" });
    activeChild = child;
    let jsonBuffer = "";
    let jsonSessionId = null;
    if (captureJson) {
      child.stdout.on("data", chunk => {
        process.stdout.write(chunk);
        jsonBuffer += chunk.toString();
        const lines = jsonBuffer.split(/\\r?\\n/);
        jsonBuffer = lines.pop() || "";
        for (const line of lines) {
          let event;
          try { event = JSON.parse(line); } catch { continue; }
          if (event.type === "thread.started" && isUuidLike(event.thread_id)) jsonSessionId = event.thread_id;
          if (event.type === "error" || event.type === "turn.failed") {
            const message = event.message || event.error?.message || "";
            if (rateLimitReasonPattern.test(message) || /usage_limit_reached/.test(message)) rateLimited = true;
          }
        }
      });
    }

    const logFile''')
patch('      result.sessionId = ownedSessionId(logFile, commandArgs);','      result.sessionId = jsonSessionId || ownedSessionId(logFile, commandArgs);')
patch('    child.on("exit", (code, signal) => {\n      scanLog();','    child.on("close", (code, signal) => {\n      scanLog();')
p.write_text(s)

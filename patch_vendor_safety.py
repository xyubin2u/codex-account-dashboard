from pathlib import Path
import sys
p=Path(sys.argv[1]);s=p.read_text()
def patch(a,b):
 global s
 assert s.count(a)==1,a[:60]
 s=s.replace(a,b,1)
patch('const execAnalyzeValueOptions = new Set([...execResumeValueOptions, "--color"]);','const execAnalyzeValueOptions = new Set([...execResumeValueOptions, "--color", "--sandbox", "-s", "--ask-for-approval", "-a", "--cd", "-C"]);')
patch('  "--strict-config",\n  "-c",','  "--strict-config",\n  "--config",\n  "-c",')
patch('    arg === "--dangerously-bypass-approvals-and-sandbox" ||','    arg === "--full-auto" ||\n    arg === "--dangerously-bypass-approvals-and-sandbox" ||')
patch('function analyzeCodexArgs(args) {','''function analyzeCodexArgs(args) {
  // Codex exec accepts these global policies after its subcommand. Move them
  // before exec/resume so retries cannot drop them or misread their values.
  const initial = splitLeadingOptions(args, 0, globalValueOptions);
  if (["exec", "e", "resume"].includes(args[initial.index])) {
    const moved = [];
    const rest = [];
    const valueNames = new Set(["--sandbox", "-s", "--ask-for-approval", "-a", "--cd", "-C", "--config", "-c"]);
    const flagNames = new Set(["--full-auto"]);
    const body = args.slice(initial.index + 1);
    for (let i = 0; i < body.length; i++) {
      const arg = body[i];
      if (arg === "--") { rest.push(...body.slice(i)); break; }
      if (!arg.startsWith("-")) { rest.push(arg); continue; }
      const name = optionName(arg);
      if (valueNames.has(name) || flagNames.has(name)) {
        moved.push(arg);
        if (valueNames.has(name) && !arg.includes("=") && body[i + 1]) moved.push(body[++i]);
      } else {
        rest.push(arg);
        if (execAnalyzeValueOptions.has(name) && !arg.includes("=") && body[i + 1]) rest.push(body[++i]);
      }
    }
    if (moved.length) args = [...initial.options, ...moved, args[initial.index], ...rest];
  }''')
patch('function hasSafetyOverride(args) {\n  return args.some', 'function hasSafetyOverride(args) {\n  if (["sandbox_mode", "approval_policy"].some(key => hasConfigAssignment(args, key))) return true;\n  return args.some')
p.write_text(s)

"""Apply after idle thresholds: bind AUTO to its actual native process session."""
import hashlib
from pathlib import Path
import sys

p=Path(sys.argv[1])
source=p.read_text()
expected='fea44dead06802ac89135bf4cc5bd086a4728d2d73ec08dff128b027000dd201'
if hashlib.sha256(p.read_bytes()).hexdigest()!=expected:
    raise SystemExit('Process session patch source hash mismatch; refusing unexpected runtime')

old=r"""    const startedAt = Date.now();
    const env = { ...process.env, CODEX_HOME: account.home, CODEX_TRUST_ALL: "0" };
    delete env.CODEX_THREAD_ID;

    const captureJson = analyzeCodexArgs(commandArgs).mode === "exec" && hasOption(commandArgs, "--json");
    const child = spawn("codex", commandArgs, { env, stdio: captureJson ? ["inherit", "pipe", "inherit"] : "inherit" });
"""
new=r"""    const startedAt = Date.now();
    const env = { ...process.env, CODEX_HOME: account.home, CODEX_TRUST_ALL: "0" };
    delete env.CODEX_THREAD_ID;
    const proofFile = path.join(logDir, 'session-owner.json');
    const proofNonce = require('crypto').randomUUID();
    env.CX_LOCAL_SESSION_PROOF = proofFile;
    env.CX_LOCAL_SESSION_NONCE = proofNonce;
    env.CX_LOCAL_SESSION_KIND = analyzeCodexArgs(commandArgs).mode === 'exec' ? 'exec' : 'cli';

    const captureJson = analyzeCodexArgs(commandArgs).mode === "exec" && hasOption(commandArgs, "--json");
    const child = spawn("codex", commandArgs, { env, stdio: captureJson ? ["inherit", "pipe", "inherit"] : "inherit" });
"""
if source.count(old)!=1:
    raise SystemExit('Process session patch anchor mismatch')
source=source.replace(old,new,1)

old=r"""    let ownerLogStamp;
    let cachedOwner = null;

    function activity() {
      let owner = jsonSessionId;
      if (!owner) {
        let stamp = null;
        try { const s = fs.statSync(logFile); stamp = `${s.dev}:${s.ino}:${s.size}:${s.mtimeMs}`; } catch {}
"""
new=r"""    let ownerLogStamp;
    let cachedOwner = null;

    function processOwnership() {
      try {
        const selectedProof = fs.existsSync(proofFile+'.final') ? proofFile+'.final' : proofFile;
        const stat = fs.lstatSync(selectedProof);
        if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 8192) throw new Error('invalid proof');
        const proof = JSON.parse(fs.readFileSync(selectedProof, 'utf8'));
        if (proof.version !== 1 || proof.nonce !== proofNonce || proof.bridge_pid !== child.pid) throw new Error('foreign proof');
        if (proof.status === 'unique' && isUuidLike(proof.session_id)) {
          if (jsonSessionId && jsonSessionId !== proof.session_id) return {status:'blocked',reason:'json_process_conflict'};
          return proof;
        }
        return proof;
      } catch (error) {
        return error.code === 'ENOENT' ? null : {status:'blocked',reason:'invalid_process_proof'};
      }
    }

    function exactSession() {
      const proof = processOwnership();
      if (proof?.status === 'unique') return proof.session_id;
      if (proof?.status === 'blocked') return null;
      if (proof?.status === 'none') return jsonSessionId;
      return jsonSessionId || ownedSessionId(logFile, commandArgs);
    }

    function activity() {
      const proof = processOwnership();
      let owner = proof?.status === 'unique' ? proof.session_id : jsonSessionId;
      if (proof?.status === 'blocked') owner = null;
      if (!owner) {
        let stamp = null;
        try { const s = fs.statSync(logFile); stamp = `${s.dev}:${s.ino}:${s.size}:${s.mtimeMs}`; } catch {}
"""
if source.count(old)!=1:
    raise SystemExit('Process session patch anchor mismatch')
source=source.replace(old,new,1)

old=r"""          ownerLogStamp = stamp;
          cachedOwner = ownedSessionId(logFile, commandArgs);
        }
        owner = cachedOwner;
      }
      if (owner !== activitySession) {
        activitySession = owner;
"""
new=r"""          ownerLogStamp = stamp;
          cachedOwner = ownedSessionId(logFile, commandArgs);
        }
        owner = proof && ['blocked', 'none'].includes(proof.status) ? null : cachedOwner;
      }
      if (owner !== activitySession) {
        activitySession = owner;
"""
if source.count(old)!=1:
    raise SystemExit('Process session patch anchor mismatch')
source=source.replace(old,new,1)

old=r"""      activeChild = null;
      result.capacityLimited = capacityLimited;
      result.thresholdLimited = thresholdLimited;
      result.sessionId = jsonSessionId || ownedSessionId(logFile, commandArgs);
      resolve(result);
    }

"""
new=r"""      activeChild = null;
      result.capacityLimited = capacityLimited;
      result.thresholdLimited = thresholdLimited;
      result.sessionId = exactSession();
      result.ownership = processOwnership();
      resolve(result);
    }

"""
if source.count(old)!=1:
    raise SystemExit('Process session patch anchor mismatch')
source=source.replace(old,new,1)

old=r"""    }

    if (process.env.CX_LOCAL_SAFE_RESUME === "1" && !result.sessionId) {
      console.error("[cx-auto] Cannot identify this process's exact session; stopped without resuming another window. Use codex-switch AUTO <session-id>.");
      return 1;
    }
"""
new=r"""    }

    if (process.env.CX_LOCAL_SAFE_RESUME === "1" && !result.sessionId) {
      try {
        const directory = path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), '.local', 'state'),
          'codex-switch', 'auto-session-diagnostics');
        fs.mkdirSync(directory, {recursive:true,mode:0o700});
        const file = path.join(directory, `${Date.now()}-${process.pid}.json`);
        fs.writeFileSync(file, JSON.stringify({version:1, reason:result.ownership?.reason || 'no_exact_session_evidence',
          root_count:result.ownership?.root_count || 0, child_count:result.ownership?.child_count || 0,
          stopped_account:selected.account.name, model_capacity:result.capacityLimited,
          threshold:result.thresholdLimited}, null, 2)+'\n', {mode:0o600,flag:'wx'});
        console.error(`[cx-auto] Session ownership diagnostic: ${file}`);
      } catch {}
      console.error("[cx-auto] Cannot identify this process's exact session; stopped without resuming another window. Use codex-switch AUTO <session-id>.");
      return 1;
    }
"""
if source.count(old)!=1:
    raise SystemExit('Process session patch anchor mismatch')
source=source.replace(old,new,1)

if hashlib.sha256(source.encode()).hexdigest()!='630f029451b97004054735079a41ccb861554fa0c26ad8d3338d4d360cb4b91b':
    raise SystemExit('Process session patch output hash mismatch')
p.write_text(source)

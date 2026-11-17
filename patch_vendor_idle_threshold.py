"""Apply after threshold hard-stop: defer idle sessions until their next task."""
import hashlib
from pathlib import Path
import sys

p=Path(sys.argv[1])
source=p.read_text()
expected='3a15df076b2df31d25507ddd89eaada294225f67a2c4aee9ae13cd110de22728'
if hashlib.sha256(p.read_bytes()).hexdigest()!=expected:
    raise SystemExit('Idle threshold patch source hash mismatch; refusing unexpected runtime')

old=r"""}

function latestSessionTurnState(accountHome, options = {}) {
  const file = findLatestSessionFile(accountHome, options);
  if (!file) {
    return { file: null, incomplete: false };
  }
"""
new=r"""}

function latestSessionTurnState(accountHome, options = {}) {
  const file = options.file || findLatestSessionFile(accountHome, options);
  if (!file) {
    return { file: null, incomplete: false };
  }
"""
if source.count(old)!=1:
    raise SystemExit('Idle threshold patch anchor mismatch')
source=source.replace(old,new,1)

old=r"""  return isUuidLike(explicit) ? explicit : null;
}

function runCodexMonitored(account, commandArgs, logDir, excludedNames = new Set()) {
  return new Promise((resolve) => {
    const env = { ...process.env, CODEX_HOME: account.home, CODEX_TRUST_ALL: "0" };
    delete env.CODEX_THREAD_ID;

"""
new=r"""  return isUuidLike(explicit) ? explicit : null;
}

// Lifecycle observation is independent of retry analysis: completed or aborted
// turns are idle, and only this process's exact session may change that state.
class LocalTurnActivity {
  constructor(startedAt = Date.now()) {
    this.startedAt = startedAt;
    this.generation = 0;
    this.reset(null);
  }

  reset(file) {
    this.file = file;
    this.identity = null;
    this.offset = 0;
    this.pending = Buffer.alloc(0);
    this.discarding = false;
    this.oversized = false;
    this.state = 'unknown';
    this.ready = false;
    this.turnId = null;
    this.mtime = null;
    this.ctime = null;
    this.generation += 1;
  }

  event(event) {
    const payload = event?.payload;
    if (!payload || typeof payload !== 'object') return;
    if (event.type === 'event_msg' && payload.type === 'task_started') {
      // A resumed transcript can end with a historical interrupted turn. It
      // isn't running in the new process until a fresh task starts here.
      const at = Date.parse(event.timestamp || '');
      this.state = Number.isFinite(at) && at < this.startedAt ? 'idle' : 'active';
      this.ready = false;
      this.turnId = payload.turn_id || null;
      this.generation += 1;
    } else if (event.type === 'event_msg' && ['task_complete', 'turn_aborted'].includes(payload.type)) {
      if (this.turnId && payload.turn_id && this.turnId !== payload.turn_id) return;
      this.state = 'idle';
      this.ready = false;
    } else if (this.state === 'active' &&
      ((event.type === 'event_msg' && ['user_message', 'agent_message'].includes(payload.type)) ||
       (event.type === 'response_item' &&
        ((payload.type === 'message' && ['user', 'assistant'].includes(payload.role)) ||
         ['function_call', 'custom_tool_call', 'local_shell_call'].includes(payload.type))))) {
      // Don't interrupt in the gap between task_started and persistence of the
      // submitted input: the existing exact-session retry needs that input.
      this.ready = true;
    }
  }

  read(file, sessionId) {
    if (file !== this.file || sessionId !== this.sessionId) {
      this.reset(file);
      this.sessionId = sessionId;
    }
    if (!file) return this.snapshot();
    let fd;
    try {
      fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
      const stat = fs.fstatSync(fd);
      const identity = `${stat.dev}:${stat.ino}`;
      if (!stat.isFile()) throw new Error('Not a transcript');
      if (identity !== this.identity || stat.size < this.offset ||
          (stat.size === this.offset && this.mtime !== null &&
           (stat.mtimeMs !== this.mtime || stat.ctimeMs !== this.ctime))) {
        this.reset(file);
        const header = Buffer.alloc(1024*1024);
        const count = fs.readSync(fd, header, 0, header.length, 0);
        const meta = JSON.parse(header.subarray(0, count).toString('utf8').split('\n', 1)[0]);
        if (!sessionId || meta?.type !== 'session_meta' || meta?.payload?.id !== sessionId) throw new Error('Wrong transcript');
        this.identity = identity;
        // Lifecycle events near the tail are enough to prove idle. If no
        // complete event is found, remain unknown and retain the hard stop.
        this.offset = Math.max(0, stat.size - 1024*1024);
        this.discarding = this.offset > 0;
      }
      const budget = Math.min(stat.size-this.offset, 4*1024*1024);
      let remaining = budget;
      while (remaining > 0) {
        const buffer = Buffer.alloc(Math.min(remaining, 128*1024));
        const count = fs.readSync(fd, buffer, 0, buffer.length, this.offset);
        if (!count) break;
        this.offset += count;
        remaining -= count;
        const chunk = Buffer.concat([this.pending, buffer.subarray(0, count)]);
        let start = 0;
        for (let end = chunk.indexOf(10); end !== -1; end = chunk.indexOf(10, start)) {
          const line = chunk.subarray(start, end);
          if (!this.discarding && line.length <= 1024*1024) {
            try { this.event(JSON.parse(line.toString('utf8'))); } catch {}
          } else if ((this.oversized || line.length > 1024*1024) && this.state === 'active' && !this.ready) {
            // A large user record can exceed the incremental buffer. Only
            // after its newline is persisted, reuse exact-file retry parsing
            // once to prove that the current turn has recoverable content.
            const saved = latestSessionTurnState(null, {file});
            this.ready = Boolean(saved.incomplete && (saved.sawUser || saved.sawAssistant || saved.sawWork));
          }
          this.discarding = false;
          this.oversized = false;
          start = end+1;
        }
        this.pending = chunk.subarray(start);
        if (this.pending.length > 1024*1024) {
          this.pending = Buffer.alloc(0);
          this.discarding = true;
          this.oversized = true;
        }
      }
      const after = fs.statSync(file);
      if (after.dev !== stat.dev || after.ino !== stat.ino || after.size < this.offset) throw new Error('Transcript replaced');
      this.mtime = stat.mtimeMs;
      this.ctime = stat.ctimeMs;
      // Don't trust an old idle marker while newer input is partially read.
      if (this.offset < after.size || this.pending.length || this.discarding) return this.snapshot('unknown');
      return this.snapshot();
    } catch {
      this.reset(file);
      return this.snapshot();
    } finally {
      if (fd !== undefined) fs.closeSync(fd);
    }
  }

  snapshot(state = this.state) {
    return { state, ready: this.ready, generation: this.generation,
      deferThreshold: this.state === 'idle' || (this.state === 'active' && !this.ready) };
  }
}

function runCodexMonitored(account, commandArgs, logDir, excludedNames = new Set()) {
  return new Promise((resolve) => {
    const startedAt = Date.now();
    const env = { ...process.env, CODEX_HOME: account.home, CODEX_TRUST_ALL: "0" };
    delete env.CODEX_THREAD_ID;

"""
if source.count(old)!=1:
    raise SystemExit('Idle threshold patch anchor mismatch')
source=source.replace(old,new,1)

old=r"""    let thresholdProbeBusy = false;
    let thresholdLastProbe = Date.now();
    let settled = false;

    function finish(result) {
      if (settled) {
"""
new=r"""    let thresholdProbeBusy = false;
    let thresholdLastProbe = Date.now();
    let settled = false;
    const turnActivity = new LocalTurnActivity(startedAt);
    let activitySession = null;
    let activityFile = null;
    let activityLookupAt = 0;
    let checkedGeneration = null;
    let ownerLogStamp;
    let cachedOwner = null;

    function activity() {
      let owner = jsonSessionId;
      if (!owner) {
        let stamp = null;
        try { const s = fs.statSync(logFile); stamp = `${s.dev}:${s.ino}:${s.size}:${s.mtimeMs}`; } catch {}
        if (stamp !== ownerLogStamp) {
          ownerLogStamp = stamp;
          cachedOwner = ownedSessionId(logFile, commandArgs);
        }
        owner = cachedOwner;
      }
      if (owner !== activitySession) {
        activitySession = owner;
        activityFile = null;
        activityLookupAt = 0;
      }
      if (activityFile && !fs.existsSync(activityFile)) activityFile = null;
      if (owner && !activityFile && Date.now()-activityLookupAt >= 1000) {
        activityLookupAt = Date.now();
        const base = process.env.CODEX_MULTI_BASE;
        if (base) {
          const resolved = spawnSync('python3', [path.resolve(__dirname, '../../..', 'session_owner.py'), base, '--rollout', owner],
            {encoding:'utf8',timeout:3000});
          if (resolved.status === 0) activityFile = resolved.stdout.trim() || null;
        }
        activityFile ||= findLatestSessionFile(account.home, {sessionId:owner});
      }
      return turnActivity.read(activityFile, owner);
    }

    function finish(result) {
      if (settled) {
"""
if source.count(old)!=1:
    raise SystemExit('Idle threshold patch anchor mismatch')
source=source.replace(old,new,1)

old=r"""      const thresholds = localThresholds();
      const threshold = (thresholds?.[account.name] || 0) || (thresholds?.__weekly?.[account.name] || 0);
      if (settled || terminating || rateLimited || capacityLimited || thresholdLimited || thresholdProbeBusy || !threshold) return;
      const testInterval = Number(process.env.CX_LOCAL_THRESHOLD_TEST_INTERVAL_MS);
      const interval = Number.isFinite(testInterval) && testInterval >= 100 ? testInterval : 60000;
      if (Date.now() - thresholdLastProbe < interval) return;
      thresholdLastProbe = Date.now();
      thresholdProbeBusy = true;
      try {
        const current = await readRateLimits(account);
        const latest = localThresholds();
        if (settled || terminating || rateLimited || capacityLimited || thresholdLimited || !latest || !belowReserve(current, latest)) return;
        // A confirmed threshold stops this owned account immediately. Finding
        // a replacement and proving session ownership are later resume checks.
        thresholdLimited = true;
"""
new=r"""      const thresholds = localThresholds();
      const threshold = (thresholds?.[account.name] || 0) || (thresholds?.__weekly?.[account.name] || 0);
      if (settled || terminating || rateLimited || capacityLimited || thresholdLimited || thresholdProbeBusy || !threshold) return;
      const before = activity();
      if (before.deferThreshold) return;
      const newTurn = before.state === 'active' && before.generation !== checkedGeneration;
      const testInterval = Number(process.env.CX_LOCAL_THRESHOLD_TEST_INTERVAL_MS);
      const interval = Number.isFinite(testInterval) && testInterval >= 100 ? testInterval : 60000;
      if (!newTurn && Date.now() - thresholdLastProbe < interval) return;
      thresholdLastProbe = Date.now();
      checkedGeneration = before.generation;
      thresholdProbeBusy = true;
      try {
        const current = await readRateLimits(account);
        const latest = localThresholds();
        if (settled || terminating || rateLimited || capacityLimited || thresholdLimited || !latest || !belowReserve(current, latest)) return;
        const after = activity();
        if (after.deferThreshold ||
            (before.state === 'active' && after.state !== 'active') ||
            (after.state === 'active' && (!after.ready || after.generation !== before.generation))) return;
        // A confirmed threshold stops this owned account immediately. Finding
        // a replacement and proving session ownership are later resume checks.
        thresholdLimited = true;
"""
if source.count(old)!=1:
    raise SystemExit('Idle threshold patch anchor mismatch')
source=source.replace(old,new,1)

old=r"""}

module.exports = {
  restoreTerminal,
  ownedSessionId,
  fiveHourQuotaBucket,
"""
new=r"""}

module.exports = {
  LocalTurnActivity,
  restoreTerminal,
  ownedSessionId,
  fiveHourQuotaBucket,
"""
if source.count(old)!=1:
    raise SystemExit('Idle threshold patch anchor mismatch')
source=source.replace(old,new,1)

if hashlib.sha256(source.encode()).hexdigest()!='fea44dead06802ac89135bf4cc5bd086a4728d2d73ec08dff128b027000dd201':
    raise SystemExit('Idle threshold patch output hash mismatch')
p.write_text(source)

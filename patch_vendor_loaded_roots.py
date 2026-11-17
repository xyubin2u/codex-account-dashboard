"""Apply after process ownership: preserve loaded-root activity distinctions."""
import hashlib
from pathlib import Path
import sys

p=Path(sys.argv[1])
source=p.read_text()
expected='630f029451b97004054735079a41ccb861554fa0c26ad8d3338d4d360cb4b91b'
if hashlib.sha256(p.read_bytes()).hexdigest()!=expected:
    raise SystemExit('Loaded roots patch source hash mismatch; refusing unexpected runtime')

old=r"""      if (proof?.status === 'unique') return proof.session_id;
      if (proof?.status === 'blocked') return null;
      if (proof?.status === 'none') return jsonSessionId;
      return jsonSessionId || ownedSessionId(logFile, commandArgs);
    }

    function activity() {
      const proof = processOwnership();
      let owner = proof?.status === 'unique' ? proof.session_id : jsonSessionId;
      if (proof?.status === 'blocked') owner = null;
      if (!owner) {
"""
new=r"""      if (proof?.status === 'unique') return proof.session_id;
      if (proof?.status === 'blocked') return null;
      if (proof?.status === 'none') return jsonSessionId;
      if (['idle','pending'].includes(proof?.status)) return null;
      return jsonSessionId || ownedSessionId(logFile, commandArgs);
    }

    function activity() {
      const proof = processOwnership();
      if (['idle','pending'].includes(proof?.status)) return {state:'idle',ready:false,generation:0,deferThreshold:true};
      let owner = proof?.status === 'unique' ? proof.session_id : jsonSessionId;
      if (proof?.status === 'blocked') owner = null;
      if (!owner) {
"""
if source.count(old)!=1:
    raise SystemExit('Loaded roots patch anchor mismatch')
source=source.replace(old,new,1)

old=r"""        const file = path.join(directory, `${Date.now()}-${process.pid}.json`);
        fs.writeFileSync(file, JSON.stringify({version:1, reason:result.ownership?.reason || 'no_exact_session_evidence',
          root_count:result.ownership?.root_count || 0, child_count:result.ownership?.child_count || 0,
          stopped_account:selected.account.name, model_capacity:result.capacityLimited,
          threshold:result.thresholdLimited}, null, 2)+'\n', {mode:0o600,flag:'wx'});
        console.error(`[cx-auto] Session ownership diagnostic: ${file}`);
"""
new=r"""        const file = path.join(directory, `${Date.now()}-${process.pid}.json`);
        fs.writeFileSync(file, JSON.stringify({version:1, reason:result.ownership?.reason || 'no_exact_session_evidence',
          root_count:result.ownership?.root_count || 0, child_count:result.ownership?.child_count || 0,
          root_states:result.ownership?.root_states || {}, active_count:result.ownership?.active_count,
          idle_count:result.ownership?.idle_count,
          stopped_account:selected.account.name, model_capacity:result.capacityLimited,
          threshold:result.thresholdLimited}, null, 2)+'\n', {mode:0o600,flag:'wx'});
        console.error(`[cx-auto] Session ownership diagnostic: ${file}`);
"""
if source.count(old)!=1:
    raise SystemExit('Loaded roots patch anchor mismatch')
source=source.replace(old,new,1)

if hashlib.sha256(source.encode()).hexdigest()!='229cd256e12b2cb6f6ed5989f6151092d484dc8a56ba34b3eb656dc9fe54c6ba':
    raise SystemExit('Loaded roots patch output hash mismatch')
p.write_text(source)

"""Apply after weekly thresholds: stop first, then attempt safe account resume."""
import hashlib
from pathlib import Path
import sys

path=Path(sys.argv[1]);source=path.read_text()
expected='4cf3e9676a11ddcfc3bed9eb6c4e92476e4a7af3a66fec98b2f61fb738edbbeb'
if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
    raise SystemExit('Threshold stop patch source hash mismatch; refusing unexpected runtime')

old='''        const sessionId = jsonSessionId || ownedSessionId(logFile, commandArgs);
        if (!sessionId || !findLatestSessionFile(account.home, {sessionId})) {
          if (!thresholdUnavailableNotice) console.error("[cx-auto] threshold reached but exact session is unavailable; continuing current account.");
          thresholdUnavailableNotice = true;
          return;
        }
        const candidates = await probeAccounts(new Set([...excludedNames,account.name]));
        const finalThresholds = localThresholds();
        if (settled || terminating || rateLimited || capacityLimited || thresholdLimited || !finalThresholds || !belowReserve(current, finalThresholds)) return;
        if (!selectResult(applyReserveThresholds(candidates, finalThresholds))) {
          if (!thresholdUnavailableNotice) console.error("[cx-auto] threshold reached but no alternative account is available; continuing current account.");
          thresholdUnavailableNotice = true;
          return;
        }
        const confirmed = await readRateLimits(account);
        const confirmedThresholds = localThresholds();
        if (settled || terminating || rateLimited || capacityLimited || thresholdLimited || !confirmedThresholds || !belowReserve(confirmed, confirmedThresholds)) return;
        if (!selectResult(applyReserveThresholds(candidates, confirmedThresholds))) return;
        thresholdLimited = true;
        console.error(`\\n[cx-auto] ${account.name} reached the configured 5h or weekly reserve threshold; switching account...`);
'''
new='''        // A confirmed threshold stops this owned account immediately. Finding
        // a replacement and proving session ownership are later resume checks.
        thresholdLimited = true;
        console.error(`\\n[cx-auto] ${account.name} reached the configured 5h or weekly reserve threshold; stopping current account before checking alternatives...`);
'''
unused='    let thresholdUnavailableNotice = false;\n'
if source.count(old)!=1 or source.count(unused)!=1:
    raise SystemExit('Threshold stop patch anchor mismatch')
source=source.replace(old,new,1).replace(unused,'',1)
path.write_text(source)

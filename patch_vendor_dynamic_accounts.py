"""Step 14, after loaded_roots: use the shared dynamic account-label contract."""
import hashlib
from pathlib import Path
import sys

p=Path(sys.argv[1]);source=p.read_text()
expected='229cd256e12b2cb6f6ed5989f6151092d484dc8a56ba34b3eb656dc9fe54c6ba'
if hashlib.sha256(p.read_bytes()).hexdigest()!=expected:
    raise SystemExit('Dynamic accounts patch source hash mismatch; refusing unexpected runtime')
old='function localThresholds() {'
new='''function validLocalAccountLabel(value) {
  return typeof value === "string" && /^[A-Z]{1,8}$/.test(value) &&
    !["S","SAVE","LOGIN","STATUS","BUZZ","AUTO","QUOTA","WEB","ACCOUNTS","REMOVE"].includes(value);
}

function localThresholds() {'''
if source.count(old)!=1 or source.count('!/^[A-H]$/.test(k)')!=1:
    raise SystemExit('Dynamic accounts patch anchor mismatch')
source=source.replace(old,new,1).replace('!/^[A-H]$/.test(k)','!validLocalAccountLabel(k)',1)
p.write_text(source)

"""Apply after patch_vendor.py, patch_vendor_extra.py and patch_vendor_safety.py."""
from pathlib import Path
import sys

p = Path(sys.argv[1])
text = p.read_text()
old = 'function selectResult(results, options = {}) {\n'
new = '''function selectResult(results, options = {}) {
  // Local preference never forces a single-account run: limit monitoring and
  // the normal fallback selection remain active after the preferred slot fails.
  const preferred = process.env.CX_LOCAL_SAFE_RESUME === "1"
    ? process.env.CX_LOCAL_PREFER_ACCOUNT : null;
  if (preferred) {
    const candidate = results.find(result => result.account.name === preferred && isUsable(result));
    if (candidate) return candidate;
  }
'''
if text.count(old) != 1 or 'const preferred = process.env.CX_LOCAL_SAFE_RESUME' in text:
    raise SystemExit('Preference patch anchor mismatch or already patched')
p.write_text(text.replace(old, new, 1))

"""Recognize official limit messages with straight or typographic apostrophes."""
from pathlib import Path
import sys

p = Path(sys.argv[1])
text = p.read_text()
if "You've hit your usage limit" not in text:
    raise SystemExit('Limit-message patch anchor mismatch or already patched')
text = text.replace("You've", "You['’‘ʼ＇]ve").replace("You're", "You['’‘ʼ＇]re")
old = r"|You['’‘ʼ＇]ve hit your usage limit\b)/i;"
new = r"|You['’‘ʼ＇]ve (?:hit|reached) your usage limit\b)/i;"
if text.count(old) != 1:
    raise SystemExit('UI limit-message patch anchor mismatch')
p.write_text(text.replace(old, new, 1))

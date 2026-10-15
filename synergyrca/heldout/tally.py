"""Tally heldout/RESULTS.txt. Run: python heldout/tally.py"""
import os
import re

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "RESULTS.txt")
rows = []
for line in open(path, encoding="utf-8"):
    m = re.match(r"^(H\d+)\s+expected=(\S+)\s+got=(\S+)\s+status=(\S+)\s+label=(\S+)", line.strip())
    if m:
        rows.append(dict(id=m[1], expected=m[2], got=m[3], status=m[4], label=m[5]))

if not rows:
    print("No results yet in RESULTS.txt")
    raise SystemExit

count = lambda label: sum(1 for r in rows if r["label"] == label)
diagnosable = [r for r in rows if r["expected"] != "unknown"]
unknown = [r for r in rows if r["expected"] == "unknown"]
diagnosed = [r for r in rows if r["status"] == "DIAGNOSED"]
ratio = lambda a, b: round(a / b, 3) if b else None

print(f"cases                      {len(rows)}")
print(f"diagnosable_cases          {len(diagnosable)}")
print(f"unknown_cases              {len(unknown)}")
print(f"precision                  {ratio(count('correct'), len(diagnosed))}")
print(f"recall                     {ratio(count('correct'), len(diagnosable))}")
print(f"unknown_handled_correctly  {ratio(count('correct_unknown'), len(unknown))}")
print(f"wrong_confident            {count('wrong_confident')}")
print(f"missed                     {count('missed')}")

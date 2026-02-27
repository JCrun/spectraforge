import json
from pathlib import Path

main = Path("gpu_specs.json")
patch = Path("amd_2019_2024_listings.json")

a = json.loads(main.read_text(encoding="utf-8"))
b = json.loads(patch.read_text(encoding="utf-8"))

old = a.get("listings", [])
new = b.get("listings", [])

# 移除主文件中 AMD 2019/2024 旧列表
kept = [
    x for x in old
    if not (x.get("manufacturer") == "AMD" and x.get("year") in (2019, 2024))
]

merged = kept + new
# 按 detail_url 去重
seen = {}
for item in merged:
    u = (item.get("detail_url") or "").rstrip("/")
    if u:
        seen[u] = item

a["listings"] = list(seen.values())
a["count"] = len(a["listings"])

# 同步 filters 里的 results（仅 AMD 2019/2024）
idx = {(x["manufacturer"], x["year"]): x for x in a.get("filters", []) if "manufacturer" in
x and "year" in x}
for k in [("AMD", 2019), ("AMD", 2024)]:
    if k in idx:
        idx[k]["results"] = sum(1 for x in a["listings"] if x.get("manufacturer")==k[0] and
x.get("year")==k[1])

main.write_text(json.dumps(a, ensure_ascii=False, indent=2), encoding="utf-8")
print("merged listings:", len(a["listings"]))

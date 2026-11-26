#!/usr/bin/env bash
# 把要上传到扣子的文件打成 dist/coze_patch.zip（只含 src 和 scripts，不含 .env、报告、快照、缓存）。
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/test_locallife_tools.py > /dev/null || { echo "工具测试没通过，停止打包"; exit 1; }
python3 scripts/test_agent_offline.py > /dev/null 2>&1 || { echo "流程测试没通过，停止打包"; exit 1; }
mkdir -p dist
rm -f dist/coze_patch.zip
python3 - <<'EOF'
import os, zipfile
files = ["src/locallife_tools.py", "src/agents/agent.py"]
files += [os.path.join("scripts", f) for f in sorted(os.listdir("scripts"))
          if f.endswith((".py", ".json")) and not f.startswith(".")]
with zipfile.ZipFile("dist/coze_patch.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for f in files:
        z.write(f)
print("已生成 dist/coze_patch.zip：")
for f in files:
    print("  " + f)
EOF
grep -o 'AGENT_VERSION = "[^"]*"' src/agents/agent.py

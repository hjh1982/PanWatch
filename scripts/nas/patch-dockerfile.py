"""给 NAS 构建上下文的 Dockerfile 打国内镜像源补丁（幂等）。

只改 ~/panwatch-build/Dockerfile，不改仓库源码。
官方源在 NAS 容器内不稳定：npm → npmmirror，PyPI → 清华镜像。
"""
from pathlib import Path

PATCHES = [
    (
        "RUN corepack enable && corepack prepare pnpm@9.15.9 --activate",
        "RUN corepack enable && COREPACK_NPM_REGISTRY=https://registry.npmmirror.com corepack prepare pnpm@9.15.9 --activate",
    ),
    (
        "RUN pnpm install --frozen-lockfile",
        "RUN pnpm install --frozen-lockfile --registry=https://registry.npmmirror.com",
    ),
    (
        "&& pip install --no-cache-dir --no-compile --prefix=/install -r /tmp/requirements-runtime.txt",
        "&& pip install --no-cache-dir --no-compile --prefix=/install -i https://pypi.tuna.tsinghua.edu.cn/simple -r /tmp/requirements-runtime.txt",
    ),
]

p = Path.home() / "panwatch-build" / "Dockerfile"
s = p.read_text()
applied = 0
for old, new in PATCHES:
    if new in s:
        continue
    if s.count(old) != 1:
        raise SystemExit(f"Dockerfile 结构与预期不符，无法应用补丁：{old[:60]}")
    s = s.replace(old, new)
    applied += 1
p.write_text(s)
print(f"Dockerfile 镜像源补丁：应用 {applied} 处（其余已就位）")
"""容器化文件的静态校验 —— 没有 Docker 也要能先抓出 80% 的低级错误。

这个脚本是「装不上 Docker 那台机器」的补偿措施，不是替身：
它查不出镜像能不能跑起来，但能查出 copy 路径写错、compose 引用了不存在的变量、
健康检查里的子命令根本不存在这类问题 —— 这些问题在真机上要 build 一次才发现，
而一次 build 可能是几分钟。

用法：
    python scripts/check_container_files.py
退出码 0 = 全部通过，1 = 有硬错误。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
IGNORE = ROOT / ".dockerignore"
ENV_EXAMPLE = ROOT / ".env.example"
CLI = ROOT / "src" / "contentops" / "cli.py"

errors: list[str] = []
checks = 0


def check(cond: bool, msg: str) -> None:
    global checks
    checks += 1
    if not cond:
        errors.append(msg)


def parse_dockerfile(text: str) -> list[tuple[str, str]]:
    """把续行拼起来，返回 [(指令, 参数)]。够用即可，不追求完整实现。"""
    joined = re.sub(r"\\\s*\n", " ", text)
    out: list[tuple[str, str]] = []
    for raw in joined.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        out.append((parts[0].upper(), parts[1] if len(parts) > 1 else ""))
    return out


def ignored_paths() -> list[str]:
    if not IGNORE.exists():
        return []
    return [
        ln.strip().rstrip("/")
        for ln in IGNORE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def main() -> int:
    check(DOCKERFILE.exists(), "缺 Dockerfile")
    check(IGNORE.exists(), "缺 .dockerignore（构建上下文会把 .git/.probe 一起打包）")
    check(COMPOSE.exists(), "缺 docker-compose.yml")
    if errors:
        return report()

    instructions = parse_dockerfile(DOCKERFILE.read_text(encoding="utf-8"))
    kinds = [k for k, _ in instructions]

    # 1) FROM 必须有 tag，不能用 latest（不可复现）
    froms = [v for k, v in instructions if k == "FROM"]
    check(bool(froms), "Dockerfile 里没有 FROM")
    for image in froms:
        check(":" in image and not image.endswith(":latest"), f"基础镜像没锁版本：{image}")

    # 2) COPY 的源必须在构建上下文里，而且没被 .dockerignore 挡掉
    ignores = ignored_paths()
    copies = [v for k, v in instructions if k in {"COPY", "ADD"}]
    for spec in copies:
        parts = spec.split()
        if len(parts) < 2 or parts[0].startswith("--from"):
            continue
        src = parts[0]
        check((ROOT / src.rstrip("/")).exists(), f"COPY 的源不存在：{src}")
        for ign in ignores:
            if src == ign or src.startswith(ign + "/"):
                errors.append(f"COPY 的源被 .dockerignore 挡掉了：{src} ← {ign}")

    # 3) ENTRYPOINT 得是 JSON 数组，且指向真的存在的模块
    entry = [v for k, v in instructions if k == "ENTRYPOINT"]
    check(len(entry) == 1, "Dockerfile 应该正好有一个 ENTRYPOINT")
    if entry:
        try:
            argv = json.loads(entry[0])
        except json.JSONDecodeError:
            argv = None
            errors.append(f"ENTRYPOINT 不是合法 JSON 数组：{entry[0]}")
        if argv:
            check("-m" in argv and "contentops.cli" in argv, f"ENTRYPOINT 没指向 contentops.cli：{argv}")
            check((ROOT / "src" / "contentops" / "cli.py").exists(), "src/contentops/cli.py 不存在")

    # 4) 不能以 root 跑；要有 HEALTHCHECK
    check("USER" in kinds and "root" not in " ".join(v for k, v in instructions if k == "USER"),
          "镜像没有切非 root 用户")
    check("HEALTHCHECK" in kinds, "没有 HEALTHCHECK，容器「起来了」和「能干活」就分不清")

    # 5) 健康检查里用到的子命令，CLI 必须真的注册过
    cli_src = CLI.read_text(encoding="utf-8")
    registered = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', cli_src))
    for k, v in instructions:
        if k != "HEALTHCHECK":
            continue
        m = re.search(r"contentops\.cli\s+([a-z-]+)", v)
        if m:
            check(m.group(1) in registered, f"HEALTHCHECK 调了没注册的子命令：{m.group(1)}")

    # 6) compose：YAML 合法、引用的变量都在 .env.example 里、卷目录存在
    try:
        import yaml
    except ImportError:
        errors.append("没装 pyyaml，compose 校验跳过 —— 请 pip install pyyaml 后重跑")
        return report()

    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8")) or {}
    services = compose.get("services") or {}
    check(bool(services), "compose 里没有 services")

    env_text = ENV_EXAMPLE.read_text(encoding="utf-8") if ENV_EXAMPLE.exists() else ""
    declared = set(re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s*=", env_text, re.MULTILINE))

    for name, svc in services.items():
        env = svc.get("environment") or {}
        check(bool(env.get("LLM_PROVIDER", "").strip()), f"{name}: 没设 LLM_PROVIDER，镜像里会走默认值")
        for key, value in env.items():
            if not isinstance(value, str):
                continue
            for var in re.findall(r"\$\{([A-Z][A-Z0-9_]*)", value):
                check(
                    var in declared,
                    f"{name}: compose 引用了 {var}，但 .env.example 里没有 —— 别人 clone 下来不知道要填什么",
                )

        for vol in svc.get("volumes") or []:
            host = str(vol).split(":")[0]
            if host.startswith("."):
                check(
                    (ROOT / host).exists() or host in {"./outputs", "./reports", "./data"},
                    f"{name}: 挂载的宿主机目录不存在：{host}",
                )

        check("build" in svc, f"{name}: 没有 build 段，compose up --build 不会构建镜像")

    return report()


def report() -> int:
    if errors:
        print(f"✗ {len(errors)} 个问题（检查 {checks} 项）")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"✓ 容器化文件检查通过（{checks} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

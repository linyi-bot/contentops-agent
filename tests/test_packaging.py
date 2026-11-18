"""打包的「合同」测试：`pip install -e .` 之后，下面这些说法必须成立。

不测「能不能装上」——那是 `pip install` 自己的事，装的过程没有断言价值；
这里守的是装完之后对外承诺的东西：命令名、版本号、返回码语义、依赖清单不漂。
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from contentops import __version__
from contentops.cli import EXIT_PAUSED, main

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_version_single_source() -> None:
    """`pyproject.toml` 与 `contentops.__version__` 必须一致。

    两处各写一遍是不得已（setuptools 读不了运行时 import，运行时也读不了 toml 的语义），
    所以就得有人看着。忘了同步的后果是 `pip show` 和 `contentops --version` 报两个版本，
    排查线上问题时最恨这种。
    """
    assert _pyproject()["project"]["version"] == __version__


def test_console_script_points_at_real_main() -> None:
    """`contentops` 这个命令必须指向一个真能调用的 main，并且返回 int。"""
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["contentops"] == "contentops.cli:main"
    assert callable(main)
    # 包内没有 toml 可读，只能这样验：入口函数接一个 argv 参数
    assert main.__defaults__ is not None or "argv" in main.__code__.co_varnames


def test_pause_exit_code_is_75() -> None:
    """跨进程「暂停」靠返回码区分，命令行入口不能把它改成 0。

    这条是给 console_scripts 的：setuptools 生成的是 `sys.exit(main())`，
    所以 main 返回什么，shell 的 `%ERRORLEVEL%` 就是什么。
    """
    assert EXIT_PAUSED == 75


def test_pyproject_deps_match_requirements() -> None:
    """pyproject 的 dependencies 与 requirements.txt 是同一份清单的两个副本。

    Dockerfile 读 requirements.txt，本地 `pip install -e .` 读 pyproject；
    两份漂了，就会出现「镜像能跑、本机装完报 ImportError」这种最难查的错。
    注释和空行不算，只比带版本号的那几行。
    """
    def norm(lines: list[str]) -> set[str]:
        out = set()
        for line in lines:
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(line.replace(" ", "").lower())
        return out

    req = norm((ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines())
    deps = norm(_pyproject()["project"]["dependencies"])
    # requirements.txt 里多出来的是测试依赖 pytest，pyproject 把它放进 [test] extra
    assert deps <= req, f"这些依赖 pyproject 有、requirements 没有：{sorted(deps - req)}"
    assert req - deps == {"pytest>=8"}, f"requirements 里多出的非测试依赖：{sorted(req - deps)}"


def test_readme_mentions_install_path() -> None:
    """README 必须写清楚「装完是什么命令」，否则别人 clone 下来只会看到 python -m。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "pip install -e" in readme
    assert "contentops --help" in readme


@pytest.mark.parametrize("sub", ["run", "resume", "kb", "llm-check", "eval", "serve"])
def test_subcommands_exist(sub: str) -> None:
    """六个子命令一个都不能少：少一个就是 README 里承诺的用法失效。"""
    with pytest.raises(SystemExit) as exc:
        main([sub, "--help"])
    assert exc.value.code == 0

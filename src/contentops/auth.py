"""JWT 鉴权：无状态登录。

为什么手写而不是装 PyJWT：这个项目的依赖已经够长，而 HS256 的 JWT
就是「base64url(header) + base64url(payload) + HMAC-SHA256 签名」，三十行能写完，
还能顺便把「为什么无状态」讲清楚 —— 面试被问到时，能手写比会调库值钱。

两种口径：
- `CONTENTOPS_AUTH=on`：`/api/*` 全部要带 `Authorization: Bearer <token>`；
- 默认 off：本地演示不折腾，但登录接口照样可用（CI 不受影响）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

#: 默认用户库（相对工作目录）
DEFAULT_USERS_DB = Path(".contentops") / "users.sqlite"


# --------------------------------------------------------------------------- #
# JWT（HS256）
# --------------------------------------------------------------------------- #


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def secret() -> str:
    """签名密钥。没配就每次启动随机生成 —— 重启后旧 token 失效，是预期行为。"""
    value = os.getenv("CONTENTOPS_SECRET", "").strip()
    if value:
        return value
    global _EPHEMERAL
    try:
        return _EPHEMERAL
    except NameError:
        _EPHEMERAL = secrets.token_urlsafe(32)
        return _EPHEMERAL


def create_token(sub: str, *, ttl: int = 86400) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {"sub": sub, "iat": now, "exp": now + ttl}
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64(json.dumps(payload, separators=(',', ':')).encode())}"
    sig = hmac.new(secret().encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(sig)}"


def decode_token(token: str) -> dict[str, Any]:
    """校验签名与过期时间；任何问题都抛 ValueError（调用方转 401）。"""
    try:
        head, payload, sig = token.split(".")
    except ValueError:
        raise ValueError("token 结构不对") from None
    signing_input = f"{head}.{payload}"
    expected = hmac.new(secret().encode(), signing_input.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64(expected), sig):
        raise ValueError("签名不匹配")
    data = json.loads(_unb64(payload))
    if int(data.get("exp", 0)) < int(time.time()):
        raise ValueError("token 已过期")
    return data


# --------------------------------------------------------------------------- #
# 用户库
# --------------------------------------------------------------------------- #


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000).hex()


@contextmanager
def _conn(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    p = Path(path or os.getenv("CONTENTOPS_USERS_DB") or DEFAULT_USERS_DB)
    if not p.is_absolute():
        p = Path.cwd() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                salt     TEXT NOT NULL,
                pwhash   TEXT NOT NULL,
                created  TEXT NOT NULL
            );
            """
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def register(username: str, password: str) -> tuple[bool, str]:
    username = (username or "").strip()
    if len(username) < 2 or len(password) < 6:
        return False, "用户名至少 2 位、密码至少 6 位"
    salt = secrets.token_bytes(16)
    with _conn() as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, salt, pwhash, created) VALUES (?,?,?,?)",
                (username, salt.hex(), _hash(password, salt), time.strftime("%Y-%m-%dT%H:%M:%S")),
            )
        except sqlite3.IntegrityError:
            return False, "用户名已存在"
    return True, ""


def verify(username: str, password: str) -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT salt, pwhash FROM users WHERE username=?", (username,)).fetchone()
    if row is None:
        return False
    salt = bytes.fromhex(row["salt"])
    return hmac.compare_digest(_hash(password, salt), row["pwhash"])


def auth_required() -> bool:
    return os.getenv("CONTENTOPS_AUTH", "").strip().lower() in {"1", "on", "true", "yes"}

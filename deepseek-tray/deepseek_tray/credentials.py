"""从 DeepSeek Harness 的凭据文件里取 API key。

文件形如 ``~/.dsh/.credentials.yaml``::

    version: 1
    records:
      client-connection/browser-session:
        payload:
          secret: <非 API key，忽略>
    refs:
      DEEPSEEK_API_KEY: sk-xxxxxxxx...

结构只有两层，用一个只匹配 ``refs:`` 区块的正则就够了 —— 为了省一个 PyYAML
依赖（以及 exe 里对应的体积），这里刻意不引入 YAML 解析器。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

KEY_NAME = "DEEPSEEK_API_KEY"
DEFAULT_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"

_REFS_HEADER = re.compile(r"^refs[ \t]*:")
_KEY_IN_REFS = re.compile(r"^[ \t]+" + KEY_NAME + r"[ \t]*:[ \t]*(.+?)[ \t]*$", re.M)
_KEY_ANYWHERE = re.compile(r"^[ \t]*" + KEY_NAME + r"[ \t]*:[ \t]*(.+?)[ \t]*$", re.M)
# sk- 后面是 32 位左右的 base62，放宽下限以免将来 key 格式变化
_VALID_KEY = re.compile(r"^sk-[A-Za-z0-9_\-]{16,}$")


def _refs_block(text: str) -> str:
    """截出顶层 ``refs:`` 下面的缩进块，避免误抓到 records 里的同名字段。"""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if _REFS_HEADER.match(line):
            start = index + 1
            break
    if start is None:
        return ""
    block: list[str] = []
    for line in lines[start:]:
        if line.strip() and not line[:1].isspace():  # 缩进结束，到了下一个顶层键
            break
        block.append(line)
    return "\n".join(block)


def _clean_scalar(raw: str) -> str:
    value = raw.strip()
    for sep in (" #", "\t#"):
        if sep in value:
            value = value.split(sep, 1)[0]
    return value.strip().strip('"').strip("'").strip()


def read_api_key(path: Path | str | None = None) -> tuple[str, str]:
    """返回 ``(key, 来源说明)``。失败时 key 为空串，说明里带人类可读的原因。"""
    target = Path(path).expanduser() if path else DEFAULT_CREDENTIALS

    if not target.is_file():
        return "", f"未找到凭据文件 {target}"

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "", f"读取凭据文件失败: {exc}"

    match = _KEY_IN_REFS.search(_refs_block(text)) or _KEY_ANYWHERE.search(text)
    if not match:
        return "", f"凭据文件里没有 {KEY_NAME} 字段"

    key = _clean_scalar(match.group(1))
    if not _VALID_KEY.match(key):
        return "", f"{KEY_NAME} 的值不像是有效 key（长度 {len(key)}）"

    return key, str(target)

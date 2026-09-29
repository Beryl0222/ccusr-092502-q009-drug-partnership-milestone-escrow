"""证据包规范化与指纹。

指纹只依赖证据内容的规范化 JSON，与提交时间、提交人无关；
同标识不同内容据此被识别为争议。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def fingerprint(payload: Any) -> str:
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"

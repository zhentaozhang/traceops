"""连接池耗尽必须能从**自然语言日志**里识别出来。

## 缺陷（F-008 暴露）

`_extract_component` 只匹配带连字符的写法：

```python
re.search(r"\b([a-z][a-z0-9-]+-connection-pool)\b", message.lower())
```

于是真实日志

```
"ERROR redis: connection pool exhausted, rejecting connection after 2400ms"
```

**提取不出任何组件** —— 因为中间是空格 "connection pool"，不是 "connection-pool"。

连带后果：

1. 该证据走 `else` 分支 → 机制判成 `dependency_timeout` 而不是 `pool_exhaustion`
2. `FT@1` 直接失败（F-008 实测 = 0）
3. 真实的 Redis / MySQL / Hikari 日志几乎**从不**写 `redis-connection-pool`，
   它们写 "connection pool exhausted"、"pool is full"、"could not get a resource"

这不是"场景数据不真实"，是解析器太窄。日志是外部输入，不能要求它符合
我们内部的命名习惯。
"""
from __future__ import annotations

import pytest

from traceops.orchestration.detectors import _extract_component


class TestNaturalLanguagePoolPhrases:
    @pytest.mark.parametrize("message", [
        "ERROR redis: connection pool exhausted, rejecting connection",
        "ERROR could not get a resource from the pool",
        "WARN connection pool is full",
        "ERROR RedisConnectionException: pool exhausted",
        "ERROR HikariPool-1 - Connection is not available, request timed out",
    ])
    def test_recognises_pool_phrases(self, message):
        component = _extract_component(message, service="redis")
        assert component is not None, f"failed to recognise pool in: {message}"
        assert "pool" in component.lower()

    def test_uses_the_reporting_service_as_prefix(self):
        """组件名应当带上所属服务，才能与配置变更的组件名对上。"""
        component = _extract_component(
            "ERROR redis: connection pool exhausted", service="redis")
        assert component == "redis-connection-pool"

    def test_explicit_hyphenated_form_still_works(self):
        assert _extract_component(
            "maxPoolSize on redis-connection-pool reduced", service="redis"
        ) == "redis-connection-pool"

    def test_non_pool_messages_yield_nothing(self):
        assert _extract_component("ERROR redis timeout after 2000ms", service="redis") is None

    def test_without_service_returns_generic_component(self):
        """没有服务名时不能编造前缀，返回通用组件名即可。"""
        component = _extract_component("ERROR connection pool exhausted")
        assert component == "connection-pool"

"""验证"真模型"这条链路到底通不通。

## 为什么要一个单独的检查命令

到目前为止，所有对外数字都来自**纯规则**路径（`run_scenario(model=None)`），
`mean_tokens=0.0` 就是证据。而 README 与文档里描述的是一个会调用模型的 agent。
这两件事必须有一个能被**跑出来**的答案，而不是靠读代码时相信它接好了。

它刻意不假装成功：
- 没有配置端点 → `UNVERIFIED`，退出码 2（不是 0，也不是 1）。
- 配置了但请求失败 → `FAILED`，退出码 1，并打印真实错误。
- 请求成功 → `OK`，打印模型是否按契约返回了结构化假设。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from traceops.runtime.model import ModelRequest

UNVERIFIED = 2


def _request() -> ModelRequest:
    return ModelRequest(
        incident_id="inc-model-check",
        service_hint="checkout-service",
        evidence_summary={
            "observations": [
                {"kind": "metric", "service": "redis", "peak_value": 0.4,
                 "above_threshold": True},
            ],
            "topology": "checkout-service -> redis",
        },
        allowed_services=["checkout-service", "redis"],
        alert_summary="model check: one synthetic observation, answer with an empty list if unsure",
        max_hypotheses=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="check that the LLM path actually works")
    parser.add_argument("--base-url", default=os.environ.get("TRACEOPS_LLM_BASE_URL"))
    parser.add_argument("--model", default=os.environ.get("TRACEOPS_LLM_MODEL"))
    parser.add_argument("--api-key", default=os.environ.get("TRACEOPS_LLM_API_KEY"))
    args = parser.parse_args(argv)

    if not args.base_url or not args.model:
        print(
            "UNVERIFIED: no model endpoint is configured.\n"
            "  set TRACEOPS_LLM_BASE_URL and TRACEOPS_LLM_MODEL (and TRACEOPS_LLM_API_KEY\n"
            "  if your endpoint needs one) and run this again.\n"
            "  Nothing in this repository has ever run the model path against a real\n"
            "  endpoint; every published number comes from the rules-only path.",
            file=sys.stderr,
        )
        return UNVERIFIED

    from traceops.runtime.openai_client import OpenAICompatibleModel

    model = OpenAICompatibleModel(base_url=args.base_url, model_name=args.model,
                                  api_key=args.api_key)
    try:
        response = model.propose(_request())
    except Exception as exc:  # 端点不可达/契约不符都到这里
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "status": "OK",
        "model": args.model,
        "base_url": args.base_url,
        "hypotheses_returned": len(response.hypotheses),
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

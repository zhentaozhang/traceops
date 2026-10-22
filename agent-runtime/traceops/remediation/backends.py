"""执行后端：真正"动手"的那一端。

## 为什么单独一个模块

受控修复此前有一个解不开的死结：执行器要求注入一个 `executor`，而仓库里
没有任何实现 —— 于是整条链路"能测、不能跑"。反过来，随手写一个
"把参数拼进 shell"的实现又是最坏的做法：那等于让计划里的参数变成命令。

这里的折中是**部署期固定命令、运行期只传值**：

    SubprocessExecutor({"restart_component": ["kubectl", "rollout", "restart",
                                             "deploy/$TARGET"]})

动作 → argv 的映射在部署时写死（配置文件、环境变量都行），运行期只做两件事：
把 `$TARGET` 换成计划里的 target、把参数放进环境变量。模型输出的任何字符串
都不是命令的一部分——它最多是一个环境变量的值，进不了 argv 结构。

## 为什么不内置 K8s/云客户端

和 Pyroscope 适配器同样的理由：没有真实集群可验，写一个"看起来能动"的客户端
只会让下一次演练时以为它已经能改环境。这里给的是**可验证的接口**与一个
以子进程为后端的参考实现；接哪家由部署方决定。
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any


class BackendUnavailable(RuntimeError):
    """没有配置任何可写后端 —— 拒绝执行，而不是假装成功。"""


@dataclass
class SubprocessExecutor:
    """按"动作 → argv 模板"执行子进程。

    - `$TARGET` 出现在任何一个 argv 元素里都会被替换成计划 target；
    - 参数以 `TRACEOPS_PARAM_<NAME>` 环境变量传入（值统一字符串化）；
    - 不做 shell 展开（`shell=False`），所以参数值里的引号、分号、换行都只是字符。
    """

    commands: dict[str, list[str]]
    timeout_seconds: float = 60.0
    extra_env: dict[str, str] = field(default_factory=dict)

    def execute(self, action: str, target: str, parameters: dict[str, Any]) -> dict[str, Any]:
        template = self.commands.get(action)
        if not template:
            raise BackendUnavailable(
                f"no command is configured for action {action!r}; "
                f"configured actions: {sorted(self.commands)}"
            )
        if not target or "/" in target and ".." in target:
            raise ValueError(f"refusing to run an action against target {target!r}")

        argv = [part.replace("$TARGET", target) for part in template]
        env = dict(os.environ)
        env.update(self.extra_env)
        env["TRACEOPS_ACTION"] = action
        env["TRACEOPS_TARGET"] = target
        for key, value in parameters.items():
            env[f"TRACEOPS_PARAM_{key.upper()}"] = (
                json.dumps(value) if isinstance(value, (dict, list)) else str(value))

        completed = subprocess.run(  # noqa: S603 — argv 来自部署配置，不是模型输出
            argv, env=env, capture_output=True, text=True, timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{action} on {target} exited {completed.returncode}: "
                f"{(completed.stderr or completed.stdout).strip()[:500]}"
            )
        return {"exit": completed.returncode, "stdout": completed.stdout[-2000:]}


@dataclass
class RecordingExecutor:
    """只记录不执行。用于干跑与测试：能证明"计划走到了这一端"。"""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def execute(self, action: str, target: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"action": action, "target": target, "parameters": dict(parameters)})
        return {"recorded": True}


def executor_from_config(path: str | os.PathLike[str] | None) -> SubprocessExecutor:
    """从 JSON 配置文件读"动作 → argv"。

    文件形状：`{"scale_replicas": ["kubectl", "scale", "deploy/$TARGET", "--replicas=$REPLICAS"]}`
    —— 注意参数**不进 argv**，只能在环境变量里用；模板里出现 `$` 开头的占位符
    只支持 `$TARGET`，其余一律视为字面量（不展开比误展开安全）。
    """
    if path is None:
        raise BackendUnavailable(
            "no executor configuration was provided; refusing to execute a write action "
            "(pass --executor-config to declare what each action actually runs)")
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict) or not raw:
        raise BackendUnavailable(f"executor config {path} must be a non-empty object")
    commands: dict[str, list[str]] = {}
    for action, argv in raw.items():
        if not isinstance(argv, list) or not argv or not all(isinstance(p, str) for p in argv):
            raise BackendUnavailable(f"action {action!r} must map to a non-empty list of strings")
        commands[action] = list(argv)
    return SubprocessExecutor(commands=commands)

"""把外部真实数据集导入成 `evaluation/scenarios` 认得的场景。

这里只有转换，没有诊断：导入后的场景走与 12 个合成场景**完全相同**的
harness / metrics / decomposition / blind 路径，于是真实数据上的分数
可以和被信任的数字比较，而不是另起一套自己算分的口径。
"""

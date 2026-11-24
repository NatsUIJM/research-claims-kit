---
id: C-000            # 与文件名一致；C=结论，A=前提假设；编号永不复用
# status: retracted  # 平时不写：已支撑 / 待验证由 claims.py 按证据与依赖算；只在撤回（retracted）或被替代（superseded）时写
statement: 一句话写清结论本身
scope: 算例与版本；工况范围；求解器与容差；重复次数
depends_on:          # 这个结论成立需要哪些其他结论或假设；没有就写 []
  - id: A-001
    why: 为什么依赖它
evidence:            # 支撑它的实验；stage 是 dvc.yaml 里的实验名；没有就写 []
  - stage: exp01
    metric: results/exp01/metrics.json:summary.key   # 文件路径:键，嵌套键用点连接
    value: "0.00"    # 论文里写的数，加引号，stale 按这个位数比较
supersedes: null     # 改了含义的新版本在这里写旧编号
# paper: §4.2 第2段  # 只在 Word 稿时手写；LaTeX 稿在句旁加注释 %C-000
---
判断理由、已知反例、待查事项（不超过 20 行）。

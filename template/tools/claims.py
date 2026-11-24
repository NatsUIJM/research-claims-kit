#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6", "dvc>=3.50,<4"]
# ///
"""结论台账：检查格式、反查依赖、找过期证据、登记远端结果、生成索引、外部改稿进出、作业状态。

在论文仓库根目录运行：
  uv run tools/claims.py check                 # 格式、悬空引用、依赖环；有错误时退出码 1
  uv run tools/claims.py impact C-009          # 列出依赖 C-009 的全部下游结论
  uv run tools/claims.py stale                 # 证据过期或数值已变的结论；有发现时退出码 1
  uv run tools/claims.py runinfo exp14 --out results/exp14/run.json   # 远端作业开始时写运行记录
  uv run tools/claims.py accept exp14          # 核对运行记录后执行 dvc commit
  uv run tools/claims.py index [--no-stale]    # 重新生成 RESEARCH.md
  uv run tools/claims.py export out/paper.zip  # 把 paper/ 打包交给外部修改，记基线并打 tag
  uv run tools/claims.py import 改过的包.zip    # 按基线三方合并回来，写改动审查清单
  uv run tools/claims.py show C-012             # 一条结论的状态、依赖、证据、下游、论文位置
  uv run tools/claims.py ls --status 待验证      # 按条件列结论
  uv run tools/claims.py hook                   # 安装 pre-commit hook：提交前跑 check 与 index
  uv run tools/claims.py jobs                   # 作业状态：本地运行记录 + 远端状态
  uv run tools/claims.py wait q3_train          # 等作业结束（后台运行）

工具只列出事实：不修改任何结论文件。结论的「已支撑 / 待验证」由工具按证据与依赖算，不手写。
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import Counter, deque
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

try:
    import yaml
except ImportError:  # 直接用 python3 运行时没有依赖
    sys.exit("缺少 pyyaml：请用 `uv run tools/claims.py ...` 运行（脚本头部已声明依赖）")

# ---- 可按仓库调整的约定 ----
CLAIMS_DIR = "claims"
PAPER_GLOB = "paper/**/*.tex"
INDEX_FILE = "RESEARCH.md"
SOURCE_PATHS = ["code", "data", "params.yaml", "dvc.yaml"]  # runinfo/accept 核对的源文件范围
BODY_MAX_LINES = 20
PAPER_DIR = "paper"              # export / import 打包的论文目录
BASE_FILE = "CLAIMS_BASE.txt"    # 导出包里记基线提交的文件；交回时不要删
IMPORT_DIR = "imports"           # import 写改动审查清单的目录
GENERATED_FILES: list[str] = []  # 由工具生成的论文文件：外部改了会在下次生成时被覆盖
BUILD_SUFFIXES = (".aux", ".log", ".out", ".toc", ".lof", ".lot", ".fls", ".fdb_latexmk", ".synctex.gz",
                  ".bbl", ".blg", ".bcf", ".run.xml", ".nav", ".snm", ".xdv")
# 改动审查里单独列出的、会改变结论强弱的词
STRENGTH_EN = re.compile(r"\b(significant(?:ly)?|all|any|every|always|never|none|no|not|only|must|cannot|"
                         r"prove[sdn]?|guarantee[sd]?|optimal|best|worst|unrelated|irrelevant|consistently|"
                         r"strictly|exactly|dominates?|outperforms?|fails?|may|might|could|likely|"
                         r"suggests?|indicates?|appears?|bound)\b", re.I)
STRENGTH_ZH = ("显著", "全部", "所有", "一定", "必然", "证明", "最优", "无关", "无法", "不能", "不会",
               "总是", "从不", "上限", "下限", "可能", "表明")

ID_RE = re.compile(r"^[CA]-\d{3,}$")
TAG_RE = re.compile(r"(?<![A-Za-z0-9-])[CA]-\d{3,}(?!\d)")
STATUSES = ("proposed", "supported", "retracted", "superseded")  # 前两个是旧写法：读入时忽略，状态由工具算
INACTIVE = ("retracted", "superseded")
STATUS_CN = {"proposed": "待验证", "supported": "已支撑", "retracted": "已撤回", "superseded": "已被替代",
             "premise": "前提"}
INDEX_ALL_FILE = "RESEARCH_ALL.md"  # 全表；RESEARCH.md 只放会话开始要看的头部
RECENT_COMMITS = 20                 # 头部列出最近这么多个提交里改过的结论
HEAD_MAX_ITEMS = 10                 # 头部每类待处理最多列这么多条，其余只报条数
# 远端作业状态从哪读：一条 shell 命令，输出每行一个 JSON（含 task_id 或 name 与 state）；空表示只读本地 status.json。
# 环境变量 CLAIMS_JOBS_CMD 优先。例：rsh -c 'for f in /srv/compute/runs/*/status.json; do cat "$f"; echo; done'
JOBS_STATUS_CMD = ""
JOBS_PREFIX = ""  # 远端只列 task_id 以此开头的作业（同一台机器上还有别的项目时用）；环境变量 CLAIMS_JOBS_PREFIX 优先
JOB_DONE = ("done",)
JOB_TERMINAL = ("done", "failed", "canceled", "cancelled")


class Claim:
    def __init__(self, cid: str, path: Path, meta: dict, body: str):
        self.id, self.path, self.meta, self.body = cid, path, meta, body
        self.computed: tuple[str, str] | None = None  # (状态, 待验证的原因)，由 compute_status 填

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or "")

    @property
    def active(self) -> bool:
        return self.status not in INACTIVE

    @property
    def label(self) -> str:
        st = self.computed[0] if self.computed else self.status
        return STATUS_CN.get(st, st or "未计算")

    @property
    def statement(self) -> str:
        return " ".join(str(self.meta.get("statement") or "").split())

    @property
    def deps(self) -> list[dict]:
        raw = self.meta.get("depends_on") or []
        return [d for d in raw if isinstance(d, dict) and d.get("id")] if isinstance(raw, list) else []

    @property
    def evidence(self) -> list[dict]:
        raw = self.meta.get("evidence") or []
        return [e for e in raw if isinstance(e, dict) and e.get("stage")] if isinstance(raw, list) else []

    def stages(self) -> set[str]:
        return {str(e["stage"]) for e in self.evidence}


def load_claims(root: Path, errors: list[str]) -> dict[str, Claim]:
    claims: dict[str, Claim] = {}
    folder = root / CLAIMS_DIR
    if not folder.is_dir():
        errors.append(f"找不到 {CLAIMS_DIR}/ 目录")
        return claims
    for p in sorted(folder.glob("*.md")):
        if p.name.startswith("_"):  # 模板等辅助文件
            continue
        rel = p.relative_to(root)
        m = re.match(r"^---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)(.*)$", p.read_text(encoding="utf-8"), re.S)
        if not m:
            errors.append(f"{rel}: 缺少 YAML 头（文件以 --- 开头，头部以 --- 结束）")
            continue
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as e:
            errors.append(f"{rel}: YAML 解析失败：{e}")
            continue
        if not isinstance(meta, dict):
            errors.append(f"{rel}: YAML 头必须是键值对")
            continue
        cid = str(meta.get("id") or "").strip()
        if not ID_RE.match(cid):
            errors.append(f"{rel}: 编号「{cid}」不合格，应为 C-012 或 A-003 这种形式")
            continue
        if cid != p.stem:
            errors.append(f"{rel}: 编号 {cid} 与文件名不一致")
        if cid in claims:
            errors.append(f"{rel}: 编号 {cid} 重复（另见 {claims[cid].path.relative_to(root)}）")
            continue
        claims[cid] = Claim(cid, p, meta, m.group(2))
    return claims


def paper_tags(root: Path) -> dict[str, list[str]]:
    """LaTeX 注释里出现的编号 → ["paper/sec4.tex:37", ...]；转义的 \\% 不算注释。"""
    tags: dict[str, list[str]] = {}
    for p in sorted(root.glob(PAPER_GLOB)):
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = re.search(r"(?<!\\)%(.*)$", line)
            if m:
                for cid in TAG_RE.findall(m.group(1)):
                    tags.setdefault(cid, []).append(f"{p.relative_to(root)}:{n}")
    return tags


SECTION_RE = re.compile(r"\\(?:part|chapter|(?:sub)*section|paragraph)\*?(?:\[[^\]]*\])?\{([^}]*)\}")


def paper_sections(root: Path) -> dict[str, list[str]]:
    """编号 → ["paper/sec4.tex#sec:method", ...]：记到所在节（节命令同行的 \\label，没有就用节标题），
    不记行号，排版改动不会让索引变。文件里第一个节命令之前的标注记作「文件头」。"""
    out: dict[str, list[str]] = {}
    for p in sorted(root.glob(PAPER_GLOB)):
        sec = "文件头"
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = SECTION_RE.search(line)
            if m:
                lab = re.search(r"\\label\{([^}]*)\}", line)
                sec = lab.group(1) if lab else " ".join(m.group(1).split())
            c = re.search(r"(?<!\\)%(.*)$", line)
            for cid in TAG_RE.findall(c.group(1)) if c else []:
                loc = f"{p.relative_to(root)}#{sec}"
                if loc not in out.setdefault(cid, []):
                    out[cid].append(loc)
    return out


def anchors(c: Claim, tags: dict[str, list[str]]) -> list[str]:
    manual = c.meta.get("paper") or []  # Word 稿手写的位置
    return tags.get(c.id, []) + ([str(manual)] if isinstance(manual, str) else [str(x) for x in manual])


def successors(claims: dict[str, Claim]) -> dict[str, str]:
    """旧编号 → 写了 supersedes 的新编号。"""
    return {str(c.meta["supersedes"]): c.id for c in claims.values() if c.meta.get("supersedes")}


def run(cmd: list[str], root: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "DVC_NO_ANALYTICS": "1"}  # 本脚本发起的 dvc 调用不上报使用统计
    return subprocess.run(cmd, cwd=root, capture_output=True, text=True, env=env)


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return run(["git", *args], root)


def dvc_cmd() -> list[str]:
    if importlib.util.find_spec("dvc"):
        return [sys.executable, "-m", "dvc"]
    if shutil.which("dvc"):
        return ["dvc"]
    sys.exit("找不到 dvc：请用 `uv run tools/claims.py ...` 运行（脚本头部已声明依赖）")


def dvc_stages(root: Path, errors: list[str]) -> set[str] | None:
    """dvc.yaml 中的实验名（foreach 展开为 name@key）；没有 dvc.yaml 或读取失败时返回 None。"""
    if not (root / "dvc.yaml").exists():
        return None
    r = run(dvc_cmd() + ["stage", "list", "--name-only"], root)
    if r.returncode != 0:
        errors.append(f"dvc stage list 失败：{(r.stderr or r.stdout).strip()[:300]}")
        return None
    return {s.strip() for s in r.stdout.splitlines() if s.strip()}


def sources(root: Path) -> list[str]:
    return [p for p in SOURCE_PATHS if (root / p).exists()] or ["."]


def find_cycles(claims: dict[str, Claim]) -> list[list[str]]:
    color: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(n: str) -> None:
        color[n] = 1
        stack.append(n)
        for d in claims[n].deps:
            m = str(d["id"])
            if m not in claims or m == n:
                continue
            if color.get(m) == 1:
                cycles.append(stack[stack.index(m):] + [m])
            elif m not in color:
                visit(m)
        stack.pop()
        color[n] = 2

    for n in sorted(claims):
        if n not in color:
            visit(n)
    return cycles


def check(root: Path, claims: dict[str, Claim], tags: dict[str, list[str]],
          stages: set[str] | None, errors: list[str], warnings: list[str]) -> None:
    succ = successors(claims)
    has_dvc = (root / "dvc.yaml").exists()
    for c in claims.values():
        if c.meta.get("status") is not None and c.status not in STATUSES:
            errors.append(f"{c.id}: status「{c.status}」只能不写，或写 retracted / superseded")
        if not c.statement:
            errors.append(f"{c.id}: 缺少 statement")
        raw = c.meta.get("depends_on") or []
        if not isinstance(raw, list) or len(raw) != len(c.deps):
            errors.append(f"{c.id}: depends_on 必须是列表，每项含 id 和 why")
        for d in c.deps:
            did = str(d["id"])
            if not str(d.get("why") or "").strip():
                errors.append(f"{c.id} → {did}: 依赖缺少 why")
            if did == c.id:
                errors.append(f"{c.id}: 依赖了自己")
            elif did not in claims:
                errors.append(f"{c.id}: 依赖的 {did} 不存在")
            elif c.active and not claims[did].active:
                hint = f"（新版本 {succ[did]}）" if did in succ else ""
                warnings.append(f"{c.id} 依赖的 {did} {STATUS_CN[claims[did].status]}{hint}，这条依赖需要复核")
        raw = c.meta.get("evidence") or []
        if not isinstance(raw, list) or len(raw) != len(c.evidence):
            errors.append(f"{c.id}: evidence 必须是列表，每项至少含 stage")
        for e in c.evidence:
            st, metric = str(e["stage"]), e.get("metric")
            if stages is None and not has_dvc:
                errors.append(f"{c.id}: 引用了实验 {st}，但仓库没有 dvc.yaml")
            elif stages is not None and st not in stages:
                errors.append(f"{c.id}: 实验 {st} 不在 dvc.yaml 中")
            if metric and ":" not in str(metric):
                errors.append(f"{c.id}: metric 应写成 文件路径:键，例如 results/exp14/metrics.json:summary.median")
            elif metric and e.get("value") is None:
                warnings.append(f"{c.id}: {metric} 没有 value 快照，stale 无法比较数值")
            elif metric and not isinstance(e.get("value"), str):
                warnings.append(f"{c.id}: value 建议加引号写成字符串，否则末尾的 0 会丢失")
        sup = c.meta.get("supersedes")
        if sup and str(sup) not in claims:
            errors.append(f"{c.id}: supersedes 的 {sup} 不存在")
        elif sup and claims[str(sup)].status != "superseded":
            warnings.append(f"{c.id} 替代了 {sup}，但 {sup} 的 status 还不是 superseded")
        if c.status == "superseded" and c.id not in succ:
            errors.append(f"{c.id}: 标为 superseded，但没有结论写 supersedes: {c.id}")
        if len(c.body.strip().splitlines()) > BODY_MAX_LINES:
            warnings.append(f"{c.id}: 正文超过 {BODY_MAX_LINES} 行，细节请移到 results/ 或提交信息")
    for cycle in find_cycles(claims):
        errors.append("依赖成环：" + " → ".join(cycle))
    for cid, locs in sorted(tags.items()):
        if cid not in claims:
            errors.append(f"论文 {locs[0]} 标注的 {cid} 不存在")
        elif not claims[cid].active:
            warnings.append(f"论文 {'、'.join(locs)} 仍在引用{STATUS_CN[claims[cid].status]}的 {cid}")


def impact(claims: dict[str, Claim], tags: dict[str, list[str]], seed: str) -> list[str]:
    s = claims[seed]
    rev: dict[str, list[tuple[str, str]]] = {}
    for c in claims.values():
        for d in c.deps:
            rev.setdefault(str(d["id"]), []).append((c.id, " ".join(str(d.get("why") or "").split())))
    path = {seed: [seed]}
    why: dict[str, str] = {}
    queue = deque([seed])
    while queue:  # 广度优先：只沿 depends_on 反向走，记录到每个下游结论的最短路径
        n = queue.popleft()
        for child, reason in sorted(rev.get(n, [])):
            if child not in path:
                path[child] = path[n] + [child]
                why[child] = reason
                queue.append(child)
    out = [f"# {seed} 的下游", "", f"{seed}（{s.label}）：{s.statement}", ""]
    down = sorted((x for x in path if x != seed), key=lambda x: (len(path[x]), x))
    if not down:
        out.append("没有结论通过 depends_on 依赖它。")
    else:
        out += [f"沿 depends_on 反向找到 {len(down)} 个结论。A → B 表示 A 依赖 B。是否仍成立由人判断。", ""]
        for x in down:
            c = claims[x]
            out += [f"- {x}（{c.label}）：{c.statement}",
                    f"  - 路径：{' → '.join(reversed(path[x]))}",
                    f"  - 依赖 {path[x][-2]} 的理由：{why[x] or '未写'}",
                    f"  - 适用范围：{c.meta.get('scope') or '未写'}",
                    f"  - 论文位置：{'、'.join(anchors(c, tags)) or '未标注'}"]
    shared = sorted(c.id for c in claims.values() if c.id not in path and c.stages() & s.stages())
    if shared:
        out += ["", "## 引用了同一实验的结论（不是依赖关系，仅供核对）", ""]
        for x in shared:
            c = claims[x]
            common = "、".join(sorted(c.stages() & s.stages()))
            out.append(f"- {x}（{c.label}）：{c.statement}；共用实验 {common}")
    return out


def metric_value(root: Path, metric: str):
    path, key = str(metric).rsplit(":", 1)
    f = root / path
    if not f.exists():
        raise LookupError(f"指标文件 {path} 不存在")
    text = f.read_text(encoding="utf-8")
    cur = yaml.safe_load(text) if f.suffix in (".yaml", ".yml") else json.loads(text)
    for part in key.split("."):  # 与 dvc metrics 一致，嵌套键用点连接
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise LookupError(f"{path} 中没有键 {key}")
    return cur


def same_as_snapshot(current, snapshot) -> bool:
    """按快照的位数四舍五入后比较：快照 "0.82" 时，0.8249 视为相同，0.826 视为已变。"""
    try:
        snap = Decimal(str(snapshot).strip())
        step = Decimal(1).scaleb(snap.as_tuple().exponent)
        return Decimal(str(current)).quantize(step, rounding=ROUND_HALF_UP) == snap
    except (InvalidOperation, TypeError, ValueError):
        return str(current).strip() == str(snapshot).strip()


def describe_changes(changed: dict) -> str:
    parts = []
    for stage, items in changed.items():
        files = []
        for item in items if isinstance(items, list) else [items]:
            if isinstance(item, dict):
                for kind, fs in item.items():
                    if not isinstance(fs, dict):
                        files.append(str(kind))
                        continue
                    for p, s in fs.items():  # 参数变化的形式是 {文件: {参数名: 状态}}
                        files += [f"{p}:{k} {v}" for k, v in s.items()] if isinstance(s, dict) else [f"{p} {s}"]
            else:
                files.append(str(item))
        parts.append(f"{stage}（{'；'.join(files)}）")
    return "，".join(parts)


def stale(root: Path, claims: dict[str, Claim]) -> list[str]:
    found: list[str] = []
    cache: dict[str, str] = {}
    for c in sorted(claims.values(), key=lambda c: c.id):
        if not c.active:
            continue
        for e in c.evidence:
            st = str(e["stage"])
            if st not in cache:  # 每个实验只查一次；--with-deps 才能发现上游实验的改动
                r = run(dvc_cmd() + ["status", st, "--with-deps", "--json"], root)
                if r.returncode != 0:
                    cache[st] = f"dvc status 失败：{(r.stderr or r.stdout).strip()[:200]}"
                else:
                    try:
                        cache[st] = describe_changes(json.loads(r.stdout or "{}"))
                    except json.JSONDecodeError:
                        cache[st] = f"dvc status 输出无法解析：{r.stdout.strip()[:200]}"
            if cache[st]:
                found.append(f"{c.id} 的证据 {st} 已过期：{cache[st]}")
            metric, snap = e.get("metric"), e.get("value")
            if not metric or snap is None or ":" not in str(metric):
                continue
            try:
                cur = metric_value(root, metric)
            except (LookupError, OSError, ValueError, yaml.YAMLError) as ex:
                found.append(f"{c.id}: {ex}")
                continue
            if not same_as_snapshot(cur, snap):
                found.append(f"{c.id} 的数值已变：{metric} 论文写 {snap}，当前 {cur}")
    return found


def compute_status(claims: dict[str, Claim], found: list[str] | None) -> None:
    """结论的 proposed / supported 由工具算，不手写：有证据或依赖、证据未过期（found 为 None 时不看）、
    依赖都已支撑（前提只要求未撤回）才算已支撑。A- 前提记为「前提」。"""
    stale_ids = None if found is None else {m.group() for x in found if (m := re.match(r"[CA]-\d{3,}", x))}
    memo: dict[str, tuple[str, str]] = {}

    def go(cid: str, seen: frozenset) -> tuple[str, str]:
        if cid in memo:
            return memo[cid]
        c = claims[cid]
        if not c.active:
            r = (c.status, "")
        elif cid.startswith("A-"):
            r = ("premise", "")
        elif not c.evidence and not c.deps:
            r = ("proposed", "没有证据也没有依赖")
        elif stale_ids is not None and cid in stale_ids:
            r = ("proposed", "证据过期或数值已变（见 stale）")
        else:
            bad = [d for d in (str(x["id"]) for x in c.deps)
                   if d not in claims or d in seen or go(d, seen | {cid})[0] not in ("supported", "premise")]
            r = ("proposed", f"依赖的 {'、'.join(bad)} 未支撑") if bad else ("supported", "")
        memo[cid] = r
        c.computed = r
        return r

    for cid in sorted(claims):
        go(cid, frozenset())


def runinfo(root: Path, stage: str, out: str) -> int:
    head = git(root, "rev-parse", "HEAD")
    if head.returncode != 0:
        print("当前目录不是 git 仓库，或还没有任何提交", file=sys.stderr)
        return 1
    dirty = git(root, "status", "--porcelain", "--", *sources(root)).stdout.strip()
    info = {"stage": stage, "commit": head.stdout.strip(), "dirty": bool(dirty),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "host": socket.gethostname()}
    target = root / out
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if dirty:
        print(f"警告：源文件有未提交改动，这次结果之后无法用 accept 登记：\n{dirty}", file=sys.stderr)
    print(f"已写入 {out}（commit {info['commit'][:10]}）")
    return 0


def accept(root: Path, stage: str, run_file: str | None, force: bool) -> int:
    rel = run_file or f"results/{stage.replace('@', '-')}/run.json"
    problems: list[str] = []
    try:
        info = json.loads((root / rel).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as ex:
        info = {}
        problems.append(f"读不到运行记录 {rel}：{ex}（远端作业开始时应执行 runinfo）")
    commit = str(info.get("commit") or "")
    if info and info.get("stage") not in (None, stage):
        problems.append(f"运行记录属于 {info.get('stage')}，不是 {stage}")
    if info.get("dirty"):
        problems.append("运行时源文件有未提交改动，无法确定结果由哪个版本产生")
    if info and git(root, "cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
        problems.append(f"本地找不到运行时的提交 {commit[:10] or '（空）'}，先 git fetch")
    elif info:
        changed = git(root, "diff", "--name-only", commit, "HEAD", "--", *sources(root)).stdout.split()
        if changed:
            problems.append("运行之后这些源文件又改过：" + "、".join(changed))
    local = git(root, "status", "--porcelain", "--", *sources(root)).stdout.strip()
    if local:
        problems.append("本地有未提交的源文件改动：" + "；".join(local.splitlines()))
    if problems and not force:
        print("拒绝登记：\n- " + "\n- ".join(problems))
        print("确认这些差异不影响结果时可加 --force，并在提交信息里写明原因。")
        return 1
    if problems:
        print("已用 --force 跳过以下问题：\n- " + "\n- ".join(problems))
    r = run(dvc_cmd() + ["commit", "-f", stage], root)
    print((r.stdout + r.stderr).strip() or f"已登记 {stage}")
    return r.returncode


def git_bytes(root: Path, *args: str) -> bytes:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip())
    return r.stdout


def git_files(root: Path, *args: str) -> list[str]:
    """git 的 -z 输出按 NUL 切开；非 ASCII 路径不会被转义。"""
    return [x for x in git(root, *args).stdout.split("\0") if x]


def export(root: Path, out: str, tag: str | None) -> int:
    head = git(root, "rev-parse", "HEAD")
    if head.returncode != 0:
        print("当前目录不是 git 仓库，或还没有任何提交", file=sys.stderr)
        return 1
    dirty = git(root, "status", "--porcelain", "--untracked-files=no", "--", PAPER_DIR).stdout.strip()
    if dirty:
        print(f"拒绝导出：{PAPER_DIR}/ 有未提交改动，先提交，导出包才对得上基线：\n{dirty}", file=sys.stderr)
        return 1
    sha = head.stdout.strip()
    tag = tag or "export-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    old = git(root, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}")
    if old.returncode == 0 and old.stdout.strip() != sha:
        print(f"拒绝导出：tag {tag} 已指向另一个提交 {old.stdout.strip()[:10]}", file=sys.stderr)
        return 1
    files = git_files(root, "ls-files", "-z", "--", PAPER_DIR)
    if not files:
        print(f"{PAPER_DIR}/ 下没有已提交的文件", file=sys.stderr)
        return 1
    target = Path(out).expanduser().resolve()
    if target.exists() and (target.is_file() or any(target.iterdir())):
        print(f"拒绝导出：{target} 已存在", file=sys.stderr)
        return 1
    payload = {f[len(PAPER_DIR) + 1:]: git_bytes(root, "show", f"{sha}:{f}") for f in files}
    payload[BASE_FILE] = (
        "# 由 claims.py export 生成。改完后整包交回，用 `uv run tools/claims.py import <包>` 并回仓库；不要删这个文件。\n"
        f"commit {sha}\ntag {tag}\npaper_dir {PAPER_DIR}\n"
        f"exported_at {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n").encode()
    if target.suffix == ".zip":
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, data in sorted(payload.items()):
                z.writestr(f"{target.stem}/{rel}", data)
    else:
        for rel, data in payload.items():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            (target / rel).write_bytes(data)
    if old.returncode != 0:
        git(root, "tag", tag, sha)
    print(f"已导出 {len(files)} 个文件到 {target}（基线 {sha[:10]}，tag {tag}）")
    return 0


def read_package(src: Path, tmp: Path) -> Path:
    """返回包里与 paper/ 对应的那一层目录；zip 先解压到 tmp。"""
    if src.is_file() and src.suffix == ".zip":
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                if n.startswith("/") or ".." in Path(n).parts:
                    raise ValueError(f"包里有不安全的路径：{n}")
            z.extractall(tmp)
        src = tmp
    if not src.is_dir():
        raise ValueError(f"{src} 既不是目录也不是 zip")
    bases = [p for p in src.rglob(BASE_FILE) if "__MACOSX" not in p.parts]
    if len(bases) > 1:
        raise ValueError(f"包里有 {len(bases)} 个 {BASE_FILE}")
    if bases:
        return bases[0].parent
    kids = [p for p in src.iterdir() if p.name not in (".DS_Store", "__MACOSX")]
    return kids[0] if len(kids) == 1 and kids[0].is_dir() else src


def is_build_junk(rel: str, present: set[str]) -> bool:
    """编译产物与系统杂项不导入；同目录有 X.tex 时 X.pdf 视为编译输出。"""
    p = Path(rel)
    if p.name in (".DS_Store", BASE_FILE) or "__MACOSX" in p.parts or p.name.endswith(BUILD_SUFFIXES):
        return True
    return p.suffix == ".pdf" and str(p.with_suffix(".tex")) in present


def hunk_marks(text: str) -> dict[str, Counter]:
    """一段 LaTeX 里要核对的记号：取数宏、结论注释、引用、交叉引用、标签、数字、强弱词。"""
    body, notes = [], []
    for line in text.splitlines():
        m = re.search(r"(?<!\\)%", line)
        body.append(line[:m.start()] if m else line)
        notes.append(line[m.end():] if m else "")
    b = "\n".join(body)
    marks = {
        "取数宏": Counter(re.findall(r"\\claimval\{([^}]*)\}", b)),
        "结论注释": Counter(TAG_RE.findall(" ".join(notes))),
        "引用": Counter(k.strip() for g in re.findall(r"\\cite\w*\*?(?:\[[^\]]*\])*\{([^}]*)\}", b)
                      for k in g.split(",") if k.strip()),
        "交叉引用": Counter(re.findall(r"\\(?:eq|auto|c|C|page|name)?ref\{([^}]*)\}", b)),
        "标签": Counter(re.findall(r"\\label\{([^}]*)\}", b)),
    }
    plain = re.sub(r"\\claimval\{[^}]*\}|\\\\\[[^\]]*\]|\\(?:label|[a-zA-Z]*ref|cite\w*\*?|input|include|"
                   r"includegraphics|bibitem|usepackage|documentclass|[hv]space\*?|setlength|addtolength|"
                   r"resizebox|scalebox|rule|begin|end|multicolumn|multirow|renewcommand|cmidrule|cline)"
                   r"(?:\[[^\]]*\])*(?:\{[^}]*\})*", " ", b)
    marks["数字"] = Counter(re.sub(r"[\s\\]", "", x) for x in
                            re.findall(r"(?<![A-Za-z\d.])\d+(?:\.\d+)?(?:\s*\\?%)?", plain))
    marks["强弱词"] = Counter(w.lower() for w in STRENGTH_EN.findall(plain)) + \
        Counter({z: plain.count(z) for z in STRENGTH_ZH})
    return marks


def snippet(lines: list[str]) -> str:
    text = " ".join(" ".join(lines).split())
    return (text[:300] + "…" if len(text) > 300 else text) or "（无）"


def review_import(root: Path, base: str, head: str, paper: str) -> tuple[list[str], int, Counter]:
    """基线到导入提交之间的改动：需核对的逐条列出，其余按文件计数。返回（各节条目、需核对处数、只计数）。"""
    sec: dict[str, list[str]] = {k: [] for k in ("文件", "记号", "数字", "强弱词")}
    other: Counter = Counter()
    toks = git_files(root, "diff", "-z", "--name-status", "--no-renames", base, head, "--", paper)
    for st, f in zip(toks[0::2], toks[1::2]):
        kind = {"A": "新增", "D": "删除"}.get(st, "修改")
        if st != "M" or not f.endswith(".tex"):
            sec["文件"].append(f"- {kind} `{f}`")
        if f in GENERATED_FILES:
            sec["文件"].append(f"- `{f}` 由工具生成，这次的外部改动会在下次生成时被覆盖")
        if not f.endswith(".tex"):
            continue
        old = [] if st == "A" else git_bytes(root, "show", f"{base}:{f}").decode(errors="replace").splitlines()
        new = [] if st == "D" else git_bytes(root, "show", f"{head}:{f}").decode(errors="replace").splitlines()
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if op == "equal":
                continue
            a, b = hunk_marks("\n".join(old[i1:i2])), hunk_marks("\n".join(new[j1:j2]))
            diffs = {k: (a[k] - b[k], b[k] - a[k]) for k in a}
            changed = [k for k, (rm, ad) in diffs.items() if rm or ad]
            if not changed:
                other[f] += 1
                continue
            loc = f"{f}:{j1 + 1}" + (f"–{j2}" if j2 - j1 > 1 else "") if j2 > j1 else f"{f}:{j1} 之后（删除）"
            parts = []
            for k in changed:
                rm, ad = diffs[k]
                parts += [f"删{k} {'、'.join(sorted(rm.elements()))}"] if rm else []
                parts += [f"加{k} {'、'.join(sorted(ad.elements()))}"] if ad else []
            if diffs["取数宏"][0] and diffs["数字"][1]:
                parts.append("可能把取数宏换成了写死的数")
            tagged = any(a[k] or b[k] for k in ("取数宏", "结论注释"))
            where = "记号" if set(changed) - {"数字", "强弱词"} else "数字" if "数字" in changed else "强弱词"
            head_line = f"- `{loc}`" + ("（带结论标注）" if tagged and where == "强弱词" else "") + "：" + "；".join(parts)
            entry = [head_line, f"  - 原：{snippet(old[i1:i2])}", f"  - 新：{snippet(new[j1:j2])}"]
            sec[where].append((0 if tagged else 1, entry) if where == "强弱词" else entry)
    sec["强弱词"] = [e for _, e in sorted(sec["强弱词"], key=lambda x: x[0])]
    need = len(sec["记号"]) + len(sec["数字"]) + len(sec["强弱词"])
    lines: list[str] = []
    for title, key in (("1. 文件", "文件"), ("2. 取数宏、结论注释、引用、交叉引用、标签的增删", "记号"),
                       ("3. 数字的增删", "数字"), ("4. 强弱词的改动（带结论标注的在前）", "强弱词")):
        lines += ["", f"## {title}", ""]
        items = sec[key]
        lines += (sum(items, []) if items and isinstance(items[0], list) else items) or ["- 无"]
    lines += ["", "## 5. 其余改动（只计数）", ""]
    lines += [f"- `{f}`：{n} 处" for f, n in sorted(other.items())] or ["- 无"]
    return lines, need, other


def stage_package(root: Path, pkg: Path, sha: str, paper: str, partial: bool, wt: Path, branch: str) -> list[str]:
    """在基线上开工作树，把包覆盖进论文目录并暂存；返回包里缺、但因包可能不全而保留的文件。"""
    r = git(root, "worktree", "add", "-q", "-b", branch, str(wt), sha)
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add 失败：{(r.stderr or r.stdout).strip()}")
    tracked = {f[len(paper) + 1:] for f in git_files(wt, "ls-files", "-z", "--", paper)}
    present = {str(p.relative_to(pkg)) for p in pkg.rglob("*") if p.is_file()}
    incoming = {f for f in present if not is_build_junk(f, present)}
    for f in sorted(incoming):
        data, dst = (pkg / f).read_bytes(), wt / paper / f
        if f in tracked and b"\r\n" in data and b"\r\n" not in dst.read_bytes():
            data = data.replace(b"\r\n", b"\n")  # 外部编辑器改成 CRLF 时还原，免得整文件都算改动
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
    missing = sorted(tracked - incoming)
    if not partial:
        for f in missing:
            (wt / paper / f).unlink()
    git(wt, "add", "-A", "--", paper)
    return missing if partial else []


def import_paper(root: Path, src: str, base_opt: str | None, note: str | None) -> int:
    if git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip() or \
            git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
        print("拒绝导入：仓库有未提交改动或未完成的合并，先提交，合并才不会混进别的改动", file=sys.stderr)
        return 1
    src_path = Path(src).expanduser().resolve()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            pkg = read_package(src_path, tmp / "pkg")
        except (ValueError, OSError, zipfile.BadZipFile) as ex:
            print(f"读不了包：{ex}", file=sys.stderr)
            return 1
        meta = {}
        if (pkg / BASE_FILE).exists():
            for line in (pkg / BASE_FILE).read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#") and " " in line:
                    k, v = line.split(" ", 1)
                    meta[k] = v.strip()
        base = base_opt or meta.get("commit")
        if not base:
            print(f"包里没有 {BASE_FILE}（不是用 export 导出的）：用 --base <提交或 tag> 指明它是从哪个版本改的",
                  file=sys.stderr)
            return 1
        r = git(root, "rev-parse", "-q", "--verify", f"{base}^{{commit}}")
        if r.returncode != 0:
            print(f"本地找不到基线 {base}，先 git fetch", file=sys.stderr)
            return 1
        sha, paper, partial = r.stdout.strip(), meta.get("paper_dir", PAPER_DIR), not meta
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        branch, wt = f"import/{stamp}", tmp / "wt"
        msg = f"外部改稿导入：{src_path.name}（基线 {sha[:10]}，导入分支 {branch}）" + (f"\n\n{note}" if note else "")
        try:
            kept = stage_package(root, pkg, sha, paper, partial, wt, branch)
            empty = git(wt, "diff", "--cached", "--quiet").returncode == 0
            if not empty:
                r = git(wt, "commit", "-q", "-m", msg)
                if r.returncode != 0:
                    raise RuntimeError(f"git commit 失败：{(r.stderr or r.stdout).strip()}")
        except RuntimeError as ex:
            print(str(ex), file=sys.stderr)
            empty = None
        finally:
            git(root, "worktree", "remove", "--force", str(wt))
            git(root, "worktree", "prune")
        if empty is None or empty:
            git(root, "branch", "-D", branch)
            if empty:
                print("包与基线相同，没有改动")
            return 1 if empty is None else 0
    return finish_import(root, src_path.name, sha, meta.get("tag"), branch, paper, kept, msg)


def finish_import(root: Path, name: str, sha: str, tag: str | None, branch: str, paper: str,
                  kept: list[str], msg: str) -> int:
    """合并导入分支（暂存、不提交），写改动审查清单。"""
    body, need, other = review_import(root, sha, branch, paper)
    git(root, "merge", "--no-ff", "--no-commit", "-m", msg, branch)
    conflicts = git_files(root, "diff", "-z", "--name-only", "--diff-filter=U")
    lines = ["# 外部改稿导入审查", "",
             f"- 来源：{name}",
             f"- 基线：{sha[:10]}" + (f"（tag {tag}）" if tag else "") + f"；导入分支：{branch}",
             f"- 需核对 {need} 处（第 2–4 节）；其余 {sum(other.values())} 处只计数（第 5 节）",
             "- 合并：" + ("有冲突，先解决：" + "、".join(f"`{f}`" for f in conflicts) if conflicts
                         else "无冲突，已暂存、未提交")]
    if kept:
        lines.append(f"- 包里没有基线文件、可能不全：基线有而包里缺的 {len(kept)} 个文件没有删除："
                     + "、".join(f"`{f}`" for f in kept[:30]) + ("…" if len(kept) > 30 else ""))
    lines += ["", "核对：第 2、3 节逐条对照台账与结果文件；第 4 节看结论强弱是否变了，要改回还是接受由人定。"
              "看完后跑 check、编译，再 git commit（合并说明已备好）。"]
    report = root / IMPORT_DIR / f"{branch.split('/', 1)[1]}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines + body) + "\n", encoding="utf-8")
    print(f"已导入到分支 {branch} 并合并进当前分支（未提交）；需核对 {need} 处，"
          f"审查清单 {report.relative_to(root)}")
    if conflicts:
        print("有冲突：" + "、".join(conflicts))
    return 1 if conflicts else 0


def cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def recent_claims(root: Path) -> list[str]:
    """最近 RECENT_COMMITS 个提交里改过的结论编号，新的在前。"""
    out: list[str] = []
    for f in git(root, "log", f"-n{RECENT_COMMITS}", "--name-only", "--format=", "--", CLAIMS_DIR).stdout.split():
        cid = Path(f).stem
        if ID_RE.match(cid) and cid not in out:
            out.append(cid)
    return out


def index_head(root: Path, claims: dict[str, Claim], errors: list[str], warnings: list[str],
               found: list[str] | None) -> str:
    """RESEARCH.md：会话开始只读这一页。"""
    lines = ["# 研究状态索引", "",
             f"> 由 `uv run tools/claims.py index` 生成，不要手改。会话开始读这一页；全表在 `{INDEX_ALL_FILE}`，"
             "单条用 `claims.py show <编号>`，按条件列用 `claims.py ls`。", ""]
    pending = [f"{c.id}（{c.computed[1]}）" for c in sorted(claims.values(), key=lambda c: c.id)
               if c.computed and c.computed[0] == "proposed"]
    todo: list[str] = []
    for kind, items, cmd in (("错误", errors, "check"), ("警告", warnings, "check"), ("过期", found or [], "stale"),
                             ("待验证", pending, "ls --status 待验证")):
        todo += [f"- {kind}：{x if len(x) <= 200 else x[:200] + '…'}" for x in items[:HEAD_MAX_ITEMS]]
        if len(items) > HEAD_MAX_ITEMS:  # 短页要保持短：多出来的只报条数
            todo.append(f"- {kind}：另有 {len(items) - HEAD_MAX_ITEMS} 条，用 `claims.py {cmd}` 看全部")
    if found is None:
        todo.append("- 本次生成用了 --no-stale，没有检查证据是否过期，状态未计入过期")
    lines += ["## 待处理", ""] + (todo or ["- 无"])
    n = Counter(c.computed[0] if c.computed else c.status for c in claims.values() if c.id.startswith("C-"))
    na = sum(1 for c in claims.values() if c.id.startswith("A-") and c.active)
    lines += ["", "## 概况", "",
              f"- 前提 {na} 条；结论 {n['supported'] + n['proposed']} 条：已支撑 {n['supported']}、"
              f"待验证 {n['proposed']}；已撤回 {n['retracted']}、已被替代 {n['superseded']}"]
    recent = recent_claims(root)
    lines += ["", "## 最近改动", "",
              f"- 最近 {RECENT_COMMITS} 个提交改过：{'、'.join(recent)}" if recent else "- 无"]
    return "\n".join(lines) + "\n"


def index(claims: dict[str, Claim], tags: dict[str, list[str]]) -> str:
    """RESEARCH_ALL.md：全表。论文位置记到节，不记行号。"""
    succ = successors(claims)
    lines = ["# 结论全表", "",
             "> 由 `uv run tools/claims.py index` 生成，不要手改。待处理事项在 RESEARCH.md。"
             "论文位置记到节；具体行号用 `claims.py show <编号>` 现查。"]
    ordered = sorted(claims.values(), key=lambda c: c.id)
    for prefix, title in (("A-", "前提假设"), ("C-", "结论")):
        rows = [c for c in ordered if c.id.startswith(prefix) and c.active]
        lines += ["", f"## {title}", ""]
        if not rows:
            lines.append("（无）")
            continue
        lines += ["| 编号 | 状态 | 内容 | 依赖 | 证据 | 论文位置 |", "|---|---|---|---|---|---|"]
        for c in rows:
            lines.append("| " + " | ".join([
                c.id, c.label, cell(c.statement),
                ", ".join(str(d["id"]) for d in c.deps) or "—",
                ", ".join(sorted(c.stages())) or "—",
                cell("、".join(anchors(c, tags))) or "—"]) + " |")
    gone = [f"{c.id}→{succ[c.id]}" if c.id in succ else c.id for c in ordered if not c.active]
    lines += ["", "## 已撤回或已替代", "", "、".join(gone) or "（无）"]
    return "\n".join(lines) + "\n"


def show(claims: dict[str, Claim], tags: dict[str, list[str]], cid: str) -> list[str]:
    """一条结论的全部信息：状态与原因、陈述、依赖及其状态、证据、直接下游、论文位置（现查行号）、正文。"""
    c, succ = claims[cid], successors(claims)
    why = f"：{c.computed[1]}" if c.computed and c.computed[1] else ""
    out = [f"# {cid}（{c.label}{why}）", "", f"- 陈述：{c.statement}", f"- 适用范围：{c.meta.get('scope') or '未写'}"]
    if c.meta.get("supersedes"):
        out.append(f"- 替代了 {c.meta['supersedes']}")
    if cid in succ:
        out.append(f"- 已被 {succ[cid]} 替代")
    out.append("- 依赖：" + ("无" if not c.deps else ""))
    for d in c.deps:
        dc = claims.get(str(d["id"]))
        out.append(f"  - {d['id']}（{dc.label if dc else '不存在'}）：{' '.join(str(d.get('why') or '').split()) or '未写理由'}")
    out.append("- 证据：" + ("无" if not c.evidence else ""))
    for e in c.evidence:
        out.append(f"  - {e['stage']}：{e.get('metric') or '（无指标）'} = {e.get('value')}")
    down = sorted(x.id for x in claims.values() if any(str(d["id"]) == cid for d in x.deps))
    out += [f"- 直接下游：{'、'.join(down) or '无'}（全部下游用 impact）",
            f"- 论文位置：{'、'.join(anchors(c, tags)) or '未标注'}"]
    return out + (["", c.body.strip()] if c.body.strip() else [])


def ls(claims: dict[str, Claim], status: str | None, kind: str | None, stage: str | None,
       text: str | None) -> list[str]:
    """按状态（中文或英文）、类别（A / C）、实验名、陈述里的文字筛选，每条一行。"""
    rows = []
    for c in sorted(claims.values(), key=lambda c: c.id):
        st = c.computed[0] if c.computed else c.status
        if (kind and not c.id.startswith(kind.upper() + "-")) or (status and status not in (st, c.label)) \
                or (stage and stage not in c.stages()) or (text and text not in c.statement):
            continue
        rows.append(f"{c.id}  {c.label}  {c.statement[:80]}")
    return rows


HOOK = f"""#!/bin/sh
# 由 claims.py hook 安装：提交前跑 check，再重新生成索引；索引变了就拒绝提交，git add 后再提交即可。
run() {{ if [ -n "$CLAIMS_PYTHON" ]; then "$CLAIMS_PYTHON" tools/claims.py "$@"; else uv run --quiet tools/claims.py "$@"; fi; }}
run check || exit 1
run index > /dev/null || exit 1
if ! git diff --quiet -- {INDEX_FILE} {INDEX_ALL_FILE} || [ -n "$(git ls-files --others --exclude-standard -- {INDEX_FILE} {INDEX_ALL_FILE})" ]; then
  echo "索引已重新生成（{INDEX_FILE}、{INDEX_ALL_FILE}），git add 后再提交" >&2
  exit 1
fi
"""


def hook_install(root: Path, force: bool) -> int:
    path = root / git(root, "rev-parse", "--git-path", "hooks").stdout.strip() / "pre-commit"
    if path.exists() and "claims.py hook" not in path.read_text(errors="replace") and not force:
        print(f"{path} 已有别的 pre-commit hook；确认可覆盖时加 --force", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HOOK, encoding="utf-8")
    path.chmod(0o755)
    print(f"已安装 {path.relative_to(root)}：提交前跑 check 与 index")
    return 0


def remote_status() -> tuple[list[dict], str]:
    """跑 JOBS_STATUS_CMD，每行一个 JSON；返回（状态列表、出错说明）。"""
    cmd = os.environ.get("CLAIMS_JOBS_CMD", JOBS_STATUS_CMD)
    if not cmd:
        return [], ""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return [], "读取远端状态超时（60 s）"
    if r.returncode != 0:
        return [], f"读取远端状态失败：{(r.stderr or r.stdout).strip()[:200]}"
    out = []
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and (d.get("task_id") or d.get("name")):
            out.append(d)
    return out, ""


def collect_jobs(root: Path) -> tuple[dict[str, dict], str]:
    """本地运行记录（results/ 与 work/ 下的 run.json，旁边的 status.json）加远端状态；远端的更新覆盖本地。"""
    jobs: dict[str, dict] = {}
    for p in sorted([*root.glob("results/**/run.json"), *root.glob("work/**/run.json")]):
        try:
            info = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        st: dict = {}
        if p.with_name("status.json").exists():
            try:
                st = json.loads(p.with_name("status.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                st = {"state": "状态文件无法解析"}
        name = str(info.get("stage") or p.parent.name)
        jobs[name] = {"state": "有运行记录、无状态文件", "started_at": info.get("started_at"),
                      **(st if isinstance(st, dict) else {}), "source": "本地"}
    remote, err = remote_status()
    prefix = os.environ.get("CLAIMS_JOBS_PREFIX", JOBS_PREFIX)
    for d in remote:
        key = str(d.get("task_id") or d.get("name"))
        if prefix and not key.startswith(prefix):
            continue
        local = next((n for n in jobs if key == n or key.startswith(n + "-")), None)
        jobs[local or key] = {**jobs.get(local or key, {}), **d, "source": "远端"}
    return jobs, err


def fmt_time(v) -> str:
    if v in (None, ""):
        return "—"
    try:
        t = datetime.fromtimestamp(v) if isinstance(v, (int, float)) else datetime.fromisoformat(str(v)).astimezone()
        return t.strftime("%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return str(v)


def jobs_lines(jobs: dict[str, dict], err: str) -> list[str]:
    out = [f"注意：{err}"] if err else []
    if not jobs:
        return out + ["没有找到作业"]
    out += ["| 作业 | 状态 | 开始 | 最后更新 | 说明 | 来源 |", "|---|---|---|---|---|---|"]
    for name, j in sorted(jobs.items()):
        upd = fmt_time(j["updated_at"]) if j.get("updated_at") else \
            f"租约至 {fmt_time(j['leased_until'])}" if j.get("leased_until") else "—"
        prog = j.get("progress")
        if isinstance(prog, dict):
            prog = f"{prog.get('done', '?')}/{prog.get('total', '?')}"
        note = "；".join(x for x in (f"进度 {prog}" if prog else "",
                                     f"退出码 {j['exit_code']}" if j.get("exit_code") not in (None, "") else "",
                                     str(j.get("err_summary") or "")) if x)
        out.append("| " + " | ".join([name, str(j.get("state") or "—"), fmt_time(j.get("started_at")), upd,
                                      cell(note) or "—", j["source"]]) + " |")
    return out


def job_state(jobs: dict[str, dict], err: str, name: str) -> str:
    key = next((k for k in jobs if k == name), None) or next((k for k in jobs if k.startswith(name + "-")), None)
    if key:
        return str(jobs[key].get("state") or "未知")
    return "读取失败" if err else "未找到"


def wait(root: Path, names: list[str], every: float, timeout: float) -> int:
    """等作业全部结束：只在状态变化时打一行；全部 done 退出码 0，有失败 1，超时 2。
    用后台方式运行（harness 会在它结束时通知），不要在前台等。"""
    start, last, last_err = time.monotonic(), {}, ""
    while True:
        jobs, err = collect_jobs(root)
        if err != last_err:
            print(f"{datetime.now():%m-%d %H:%M:%S} " + (err or "远端状态恢复读取"), flush=True)
            last_err = err
        cur = {n: job_state(jobs, err, n) for n in names}
        for n, s in cur.items():
            if last.get(n) != s:
                print(f"{datetime.now():%m-%d %H:%M:%S} {n}: {s}", flush=True)
        last = cur
        if all(s in JOB_TERMINAL for s in cur.values()):
            return 0 if all(s in JOB_DONE for s in cur.values()) else 1
        if timeout and time.monotonic() - start > timeout:
            print(f"超时：等了 {timeout:.0f} s 仍未全部结束", flush=True)
            return 2
        time.sleep(every)


def find_root() -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 else Path.cwd()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="论文仓库根目录；默认取当前 git 仓库根目录")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="检查格式、引用和依赖环")
    sub.add_parser("impact", help="列出依赖某个结论的全部下游结论").add_argument("id")
    sub.add_parser("stale", help="列出证据过期或数值已变的结论")
    p = sub.add_parser("runinfo", help="远端作业开始时写运行记录")
    p.add_argument("stage")
    p.add_argument("--out", required=True)
    p = sub.add_parser("accept", help="核对运行记录后登记远端结果")
    p.add_argument("stage")
    p.add_argument("--run", help="运行记录路径，默认 results/<实验名>/run.json")
    p.add_argument("--force", action="store_true")
    sub.add_parser("index", help=f"重新生成 {INDEX_FILE}（短页）与 {INDEX_ALL_FILE}（全表）").add_argument(
        "--no-stale", action="store_true")
    p = sub.add_parser("export", help=f"把 {PAPER_DIR}/ 打包交给外部修改，记下基线提交并打 tag")
    p.add_argument("out", help="输出路径：以 .zip 结尾打成 zip，否则写成目录")
    p.add_argument("--tag", help="基线 tag，默认 export-<时间>；交卷或投稿版可用 submit-final 之类")
    p = sub.add_parser("import", help="把外部改过的包按基线三方合并回来，并写改动审查清单")
    p.add_argument("src", help="改过的包：export 导出的 zip 或目录")
    p.add_argument("--base", help=f"包里没有 {BASE_FILE} 时，指明它是从哪个提交或 tag 改的")
    p.add_argument("--note", help="写进合并说明，例如谁改的、改了什么")
    p = sub.add_parser("show", help="一条结论的状态与原因、依赖、证据、下游、论文位置")
    p.add_argument("id")
    p.add_argument("--no-stale", action="store_true", help="不查过期，快")
    p = sub.add_parser("ls", help="按状态、类别、实验、文字筛选结论，每条一行")
    p.add_argument("--status", help="已支撑 / 待验证 / 前提 / 已撤回 / 已被替代（或英文）")
    p.add_argument("--kind", choices=["A", "C", "a", "c"])
    p.add_argument("--stage", help="证据挂在这个实验上的")
    p.add_argument("--text", help="陈述里含这段文字的")
    p.add_argument("--no-stale", action="store_true", help="不查过期，快")
    p = sub.add_parser("hook", help="安装 git pre-commit hook：提交前跑 check 与 index")
    p.add_argument("--force", action="store_true", help="覆盖已有的别的 hook")
    sub.add_parser("jobs", help="列出作业状态（本地运行记录 + JOBS_STATUS_CMD 读到的远端状态）")
    p = sub.add_parser("wait", help="等作业结束，只在状态变化时打一行；请用后台方式运行")
    p.add_argument("names", nargs="+")
    p.add_argument("--every", type=float, default=60, help="隔多少秒读一次，默认 60")
    p.add_argument("--timeout", type=float, default=0, help="最多等多少秒，0 表示不限")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve() if a.root else find_root()

    if a.cmd == "hook":
        return hook_install(root, a.force)
    if a.cmd == "jobs":
        print("\n".join(jobs_lines(*collect_jobs(root))))
        return 0
    if a.cmd == "wait":
        return wait(root, a.names, a.every, a.timeout)
    if a.cmd == "export":
        return export(root, a.out, a.tag)
    if a.cmd == "import":
        return import_paper(root, a.src, a.base, a.note)
    if a.cmd == "runinfo":
        return runinfo(root, a.stage, a.out)
    if a.cmd == "accept":
        return accept(root, a.stage, a.run, a.force)
    errors: list[str] = []
    warnings: list[str] = []
    claims = load_claims(root, errors)
    tags = paper_tags(root)
    if a.cmd in ("impact", "stale") and errors:
        print("注意：以下文件读取失败，结果可能不全：\n" + "\n".join(errors), file=sys.stderr)
    if a.cmd in ("show", "impact") and a.id not in claims:
        print(f"找不到 {a.id}", file=sys.stderr)
        return 1
    if a.cmd == "impact":  # 不查过期：状态只看证据与依赖是否齐
        compute_status(claims, None)
        print("\n".join(impact(claims, tags, a.id)))
        return 0
    if a.cmd == "stale":
        found = stale(root, claims)
        print("\n".join(found) if found else "所有证据都是最新的，数值与论文快照一致。")
        return 1 if found else 0
    if a.cmd in ("show", "ls"):
        compute_status(claims, None if a.no_stale else stale(root, claims))
        rows = show(claims, tags, a.id) if a.cmd == "show" else ls(claims, a.status, a.kind, a.stage, a.text)
        print("\n".join(rows) if rows else "没有符合条件的结论")
        return 0
    check(root, claims, tags, dvc_stages(root, errors), errors, warnings)
    if a.cmd == "check":
        for x in errors:
            print(f"错误：{x}")
        for x in warnings:
            print(f"警告：{x}")
        print(f"共 {len(claims)} 条；错误 {len(errors)} 个，警告 {len(warnings)} 个")
        return 1 if errors else 0
    found = None if a.no_stale else stale(root, claims)
    compute_status(claims, found)
    (root / INDEX_FILE).write_text(index_head(root, claims, errors, warnings, found), encoding="utf-8")
    (root / INDEX_ALL_FILE).write_text(index(claims, paper_sections(root)), encoding="utf-8")
    pending = sum(1 for c in claims.values() if c.computed and c.computed[0] == "proposed")
    print(f"已更新 {INDEX_FILE}、{INDEX_ALL_FILE}：{len(claims)} 条，"
          f"待处理 {len(errors) + len(warnings) + len(found or [])} 项，待验证 {pending} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())

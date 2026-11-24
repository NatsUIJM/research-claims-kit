"""端到端测试：在临时目录建一个 git + DVC 小仓库，逐个检查 claims.py 的命令。

运行（在工具包根目录）：
  uv run --with pytest --with pyyaml --with "dvc==3.67.1" pytest -q tests/
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

os.environ["DVC_NO_ANALYTICS"] = "1"
KIT = Path(__file__).resolve().parents[1]
SCRIPT = KIT / "template" / "tools" / "claims.py"
sys.dont_write_bytecode = True  # 不在 template/ 里留下 __pycache__
_spec = importlib.util.spec_from_file_location("claims", SCRIPT)
claims_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(claims_mod)
DVC = [sys.executable, "-m", "dvc"]

PREP = ('import json, os\nos.makedirs("results/prep", exist_ok=True)\n'
        'json.dump({"scale": 1.0}, open("results/prep/out.json", "w"))\n')
EXP = ('import json, os\ns = json.load(open("results/prep/out.json"))["scale"]\n'
       'os.makedirs("results/exp14", exist_ok=True)\n'
       'json.dump({"summary": {"median_ratio": round(0.82 * s, 4)}}, open("results/exp14/metrics.json", "w"))\n')
PAPER = "方法 A 更快。 %C-012\n见表 3。 % C-015 C-020\n转义的 50\\% 不是注释 C-999\n"


def sh(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"{cmd} 失败：{r.stdout}{r.stderr}"
    return r


def claims(repo, *args):
    return subprocess.run([sys.executable, str(repo / "tools" / "claims.py"), *args],
                          cwd=repo, capture_output=True, text=True)


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def claim(repo, cid, status="supported", deps=(), stage=None, value=None, extra=""):
    dep_yaml = "".join(f"\n  - id: {d}\n    why: 测试依赖" for d in deps) or " []"
    ev = (f"\n  - stage: {stage}\n    metric: results/{stage}/metrics.json:summary.median_ratio"
          f"\n    value: \"{value}\"") if stage else " []"
    write(repo / "claims" / f"{cid}.md",
          f"---\nid: {cid}\nstatus: {status}\nstatement: {cid} 的陈述\nscope: case39\n"
          f"depends_on:{dep_yaml}\nevidence:{ev}\n{extra}---\n理由。\n")


def set_status(repo, cid, status):
    p = repo / "claims" / f"{cid}.md"
    p.write_text(p.read_text(encoding="utf-8").replace("status: supported", f"status: {status}"), encoding="utf-8")


def edit(path: Path, old: str, new: str):
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def digest(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            h.update(p.name.encode() + p.read_bytes())
    return h.hexdigest()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "paper-repo"
    (r / "tools").mkdir(parents=True)
    shutil.copy(SCRIPT, r / "tools" / "claims.py")
    sh(["git", "init", "-q"], r)
    sh(["git", "config", "user.email", "t@t"], r)
    sh(["git", "config", "user.name", "t"], r)
    sh(DVC + ["init", "-q"], r)
    write(r / "code/prep.py", PREP)
    write(r / "code/exp.py", EXP)
    sh(DVC + ["stage", "add", "-q", "-n", "prep", "-d", "code/prep.py",
              "-o", "results/prep/out.json", "python3 code/prep.py"], r)
    sh(DVC + ["stage", "add", "-q", "-n", "exp14", "-d", "code/exp.py", "-d", "results/prep/out.json",
              "-M", "results/exp14/metrics.json", "python3 code/exp.py"], r)
    sh(DVC + ["repro", "-q"], r)
    claim(r, "A-001")
    claim(r, "C-009", deps=["A-001"], stage="exp14", value="0.82")
    claim(r, "C-012", deps=["C-009"])
    claim(r, "C-015", deps=["C-012"])
    claim(r, "C-020", stage="exp14", value="0.82")
    write(r / "paper/main.tex", PAPER)
    sh(["git", "add", "-A"], r)
    sh(["git", "commit", "-qm", "init"], r)
    return r


def test_check_clean(repo):
    r = claims(repo, "check")
    assert r.returncode == 0, r.stdout
    assert "错误 0 个，警告 0 个" in r.stdout, r.stdout


def test_check_reports_errors(repo):
    claim(repo, "C-030", deps=["C-404"])
    claim(repo, "C-031", deps=["C-032"])
    claim(repo, "C-032", deps=["C-031"])
    claim(repo, "C-033", status="done")
    claim(repo, "C-034", stage="exp99", value="1")
    claim(repo, "C-035", status="superseded")
    write(repo / "claims/C-036.md", "---\nid: C-036\nstatus: proposed\nstatement: x\n"
          "depends_on:\n  - id: C-009\nevidence: []\n---\n")
    write(repo / "paper/extra.tex", "引用不存在的结论 %C-777\n")
    r = claims(repo, "check")
    assert r.returncode == 1
    for s in ["C-030: 依赖的 C-404 不存在", "依赖成环：C-031 → C-032 → C-031", "C-033: status「done」",
              "C-034: 实验 exp99 不在 dvc.yaml 中", "C-035: 标为 superseded", "C-036 → C-009: 依赖缺少 why",
              "paper/extra.tex:1 标注的 C-777 不存在"]:
        assert s in r.stdout, (s, r.stdout)
    assert "C-999" not in r.stdout  # \% 后面不是注释


def test_retracted_upstream_and_paper_warnings(repo):
    set_status(repo, "C-012", "retracted")
    out = claims(repo, "check").stdout
    assert "C-015 依赖的 C-012 已撤回，这条依赖需要复核" in out, out
    assert "论文 paper/main.tex:1 仍在引用已撤回的 C-012" in out, out


def test_impact_lists_downstream_and_changes_nothing(repo):
    before = digest(repo / "claims")
    r = claims(repo, "impact", "C-009")
    assert r.returncode == 0, r.stderr
    down, shared = r.stdout.split("## 引用了同一实验的结论")
    assert "C-012 → C-009" in down and "C-015 → C-012 → C-009" in down
    assert "论文位置：paper/main.tex:1" in down and "论文位置：paper/main.tex:2" in down
    assert "A-001" not in r.stdout  # 上游不出现
    assert "C-020" in shared and "C-020" not in down
    assert digest(repo / "claims") == before


def test_supersedes_does_not_propagate(repo):
    claim(repo, "C-021", deps=["A-001"], stage="exp14", value="0.82", extra="supersedes: C-009\n")
    set_status(repo, "C-009", "superseded")
    out = claims(repo, "impact", "C-009").stdout
    assert "C-021" not in out.split("## 引用了同一实验的结论")[0]  # 新版本不算下游
    chk = claims(repo, "check").stdout
    assert "C-012 依赖的 C-009 已被替代（新版本 C-021），这条依赖需要复核" in chk, chk


def test_stale_finds_upstream_change_then_value_change(repo):
    r = claims(repo, "stale")
    assert r.returncode == 0, r.stdout + r.stderr
    edit(repo / "code/prep.py", '"scale": 1.0', '"scale": 1.1')
    r = claims(repo, "stale")
    assert r.returncode == 1
    assert "C-009 的证据 exp14 已过期：prep（code/prep.py modified）" in r.stdout, r.stdout
    sh(DVC + ["repro", "-q"], repo)
    r = claims(repo, "stale")
    assert "已过期" not in r.stdout, r.stdout
    assert "C-009 的数值已变：results/exp14/metrics.json:summary.median_ratio 论文写 0.82，当前 0.902" in r.stdout


@pytest.mark.parametrize("current,snapshot,same", [
    (0.8249, "0.82", True), (0.826, "0.82", False), (0.8204, "0.820", True), (0.8206, "0.820", False),
    (0.0012, "1.2e-3", True), (30, "30", True), (31, "30", False), ("ok", "ok", True), (0.82, 0.82, True),
])
def test_same_as_snapshot(current, snapshot, same):
    assert claims_mod.same_as_snapshot(current, snapshot) is same


def test_describe_changes_flattens_params():
    changed = {"exp14": [{"changed deps": {"params.yaml": {"env.solver": "modified"}}}, "changed command"]}
    assert claims_mod.describe_changes(changed) == "exp14（params.yaml:env.solver modified；changed command）"


def remote_run(repo):
    """模拟远端作业：先写运行记录，再产出结果。"""
    for stage in ("prep", "exp14"):
        r = claims(repo, "runinfo", stage, "--out", f"results/{stage}/run.json")
        assert r.returncode == 0, r.stderr
    sh(["python3", "code/prep.py"], repo)
    sh(["python3", "code/exp.py"], repo)


def test_accept_registers_clean_remote_run(repo):
    edit(repo / "code/prep.py", '"scale": 1.0', '"scale": 1.1')
    sh(["git", "commit", "-qam", "[C-009] 改缩放"], repo)
    remote_run(repo)
    for stage in ("prep", "exp14"):
        r = claims(repo, "accept", stage)
        assert r.returncode == 0, r.stdout
    assert json.loads(sh(DVC + ["status", "--json"], repo).stdout or "{}") == {}


def test_accept_refuses_when_code_changed_after_run(repo):
    remote_run(repo)
    edit(repo / "code/exp.py", "import json, os", "import json, os  # 运行之后又改了")
    sh(["git", "commit", "-qam", "改代码"], repo)
    r = claims(repo, "accept", "exp14")
    assert r.returncode == 1 and "运行之后这些源文件又改过：code/exp.py" in r.stdout, r.stdout
    assert claims(repo, "accept", "exp14", "--force").returncode == 0


def test_accept_refuses_dirty_or_mismatched_records(repo):
    edit(repo / "code/exp.py", "import json, os", "import json, os  # 未提交")
    remote_run(repo)  # 运行记录会标记 dirty
    r = claims(repo, "accept", "exp14")
    assert r.returncode == 1
    assert "运行时源文件有未提交改动" in r.stdout and "本地有未提交的源文件改动" in r.stdout, r.stdout
    r = claims(repo, "accept", "exp14", "--run", "results/prep/run.json")
    assert "运行记录属于 prep，不是 exp14" in r.stdout
    r = claims(repo, "accept", "exp14", "--run", "results/none/run.json")
    assert r.returncode == 1 and "读不到运行记录" in r.stdout


def read_index(repo):
    return [(repo / f).read_text(encoding="utf-8") for f in ("RESEARCH.md", "RESEARCH_ALL.md")]


def test_index_is_derived_and_deterministic(repo):
    assert claims(repo, "index").returncode == 0
    head, full = read_index(repo)
    assert "已支撑 4、待验证 0" in head and "- 前提 1 条" in head, head
    assert "| C-012 | 已支撑 | C-012 的陈述 | C-009 | — | paper/main.tex#文件头 |" in full, full
    assert "| A-001 | 前提 | A-001 的陈述 | — | — | — |" in full and "| C-" not in head
    edit(repo / "code/prep.py", '"scale": 1.0', '"scale": 1.1')  # 证据过期：状态跟着变，不用人改
    assert claims(repo, "index").returncode == 0
    head, full = read_index(repo)
    assert "- 过期：C-009 的证据 exp14 已过期" in head
    assert "- 待验证：C-009（证据过期或数值已变（见 stale））" in head
    assert "- 待验证：C-012（依赖的 C-009 未支撑）" in head and "- 待验证：C-015（依赖的 C-012 未支撑）" in head
    set_status(repo, "C-015", "retracted")
    claims(repo, "index", "--no-stale")
    first = read_index(repo)
    claims(repo, "index", "--no-stale")
    assert read_index(repo) == first
    assert "| C-015 |" not in first[1] and "## 已撤回或已替代\n\nC-015\n" in first[1]
    assert "仍在引用已撤回的 C-015" in first[0] and "没有检查证据是否过期" in first[0]


def test_status_is_computed_not_written(repo):
    claim(repo, "C-040")  # 写了旧的 status: supported，但没有证据也没有依赖
    write(repo / "claims/C-041.md", "---\nid: C-041\nstatement: 不写 status\ndepends_on:\n  - id: C-009\n"
          "    why: 测试\nevidence: []\n---\n")
    claim(repo, "C-042", status="proposed", deps=["C-040"])
    assert claims(repo, "check").returncode == 0
    r = claims(repo, "ls", "--status", "待验证", "--no-stale")
    assert r.stdout.split("\n")[0].startswith("C-040  待验证") and "C-042  待验证" in r.stdout, r.stdout
    assert "C-041  已支撑" in claims(repo, "ls", "--kind", "C", "--no-stale").stdout
    out = claims(repo, "show", "C-042", "--no-stale").stdout
    assert "# C-042（待验证：依赖的 C-040 未支撑）" in out and "- C-040（待验证）：测试依赖" in out, out
    for i in range(60, 71):  # 待验证太多时短页只列 10 条
        claim(repo, f"C-0{i}")
    claims(repo, "index", "--no-stale")
    head = read_index(repo)[0]
    assert head.count("- 待验证：C-") == 10 and "- 待验证：另有 3 条，用 `claims.py ls --status 待验证`" in head, head
    out = claims(repo, "show", "C-009").stdout
    assert "直接下游：C-012" in out and "论文位置：—" not in out and "exp14：results/exp14" in out, out


def test_index_locations_survive_reflow(repo):
    write(repo / "paper/sec.tex", "\\section{Method}\\label{sec:m}\n结论一。 %C-009\n\\subsection{Data}\n结论二。 %C-020\n")
    claims(repo, "index", "--no-stale")
    first = read_index(repo)[1]
    assert "paper/sec.tex#sec:m" in first and "paper/sec.tex#Data" in first, first
    edit(repo / "paper/sec.tex", "结论一。", "\n\n结论一，改了措辞。\n")
    edit(repo / "paper/main.tex", "方法 A 更快。", "\n方法 A 更快。")
    claims(repo, "index", "--no-stale")
    assert read_index(repo)[1] == first  # 行号变了，全表不变
    assert "paper/sec.tex:5" in claims(repo, "show", "C-009", "--no-stale").stdout  # 行号现查


def unzip(zpath: Path, dest: Path) -> Path:
    import zipfile
    with zipfile.ZipFile(zpath) as z:
        z.extractall(dest)
    return dest / zpath.stem


def rezip(folder: Path, zpath: Path):
    import zipfile
    with zipfile.ZipFile(zpath, "w") as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                z.write(p, f"{folder.name}/{p.relative_to(folder)}")


def test_export_records_base_and_tag(repo, tmp_path):
    out = tmp_path / "out" / "pkg.zip"
    r = claims(repo, "export", str(out), "--tag", "submit-v1")
    assert r.returncode == 0, r.stderr
    pkg = unzip(out, tmp_path / "x")
    head = sh(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    assert (pkg / "main.tex").read_text(encoding="utf-8") == PAPER
    assert f"commit {head}" in (pkg / "CLAIMS_BASE.txt").read_text(encoding="utf-8")
    assert sh(["git", "rev-parse", "submit-v1^{commit}"], repo).stdout.strip() == head
    edit(repo / "paper/main.tex", "方法 A", "方法 B")
    r = claims(repo, "export", str(tmp_path / "pkg2.zip"))
    assert r.returncode == 1 and "有未提交改动" in r.stderr
    sh(["git", "commit", "-qam", "改稿"], repo)
    r = claims(repo, "export", str(tmp_path / "pkg3.zip"), "--tag", "submit-v1")
    assert r.returncode == 1 and "已指向另一个提交" in r.stderr


def test_import_merges_and_lists_what_to_check(repo, tmp_path):
    write(repo / "paper/intro.tex", "背景。\n\n结尾。\n")
    sh(["git", "add", "-A"], repo)
    sh(["git", "commit", "-qm", "加 intro"], repo)
    assert claims(repo, "export", str(tmp_path / "pkg.zip")).returncode == 0
    edit(repo / "paper/intro.tex", "结尾。", "结尾，仓库里同时改。")  # 导出之后仓库这边也改了
    sh(["git", "commit", "-qam", "仓库改 intro"], repo)
    pkg = unzip(tmp_path / "pkg.zip", tmp_path / "x")
    text = (pkg / "main.tex").read_text(encoding="utf-8")
    text = text.replace("方法 A 更快。", "方法 A 显著更快。").replace("见表 3。", "见表 4。")
    (pkg / "main.tex").write_bytes(text.replace("\n", "\r\n").encode())  # 外部编辑器存成 CRLF
    edit(pkg / "intro.tex", "背景。", "背景，润色过。")
    write(pkg / "main.aux", "x")
    write(pkg / "main.pdf", "x")
    write(pkg / "README.txt", "外部加的说明\n")
    rezip(pkg, tmp_path / "back.zip")
    r = claims(repo, "import", str(tmp_path / "back.zip"), "--note", "润色")
    assert r.returncode == 0, r.stdout + r.stderr
    main = (repo / "paper/main.tex").read_bytes().decode()
    assert "\r" not in main and "显著更快" in main and "见表 4" in main
    intro = (repo / "paper/intro.tex").read_text(encoding="utf-8")
    assert "背景，润色过。" in intro and "结尾，仓库里同时改。" in intro  # 两边的改动都在
    assert not (repo / "paper/main.aux").exists() and not (repo / "paper/main.pdf").exists()
    report = next((repo / "imports").glob("*.md")).read_text(encoding="utf-8")
    assert "新增 `paper/README.txt`" in report
    assert "`paper/main.tex:1–2`：删数字 3；加数字 4；加强弱词 显著" in report, report
    assert "`paper/intro.tex`：1 处" in report and "无冲突" in report
    sh(["git", "commit", "-q", "--no-edit"], repo)
    assert "外部改稿导入" in sh(["git", "log", "-1", "--format=%B"], repo).stdout


def test_import_reports_conflict_and_replaced_macro(repo, tmp_path):
    write(repo / "paper/num.tex", "比值为 \\claimval{C-009}。\n")
    sh(["git", "add", "-A"], repo)
    sh(["git", "commit", "-qm", "加取数"], repo)
    assert claims(repo, "export", str(tmp_path / "pkg")).returncode == 0  # 导出成目录
    edit(repo / "paper/main.tex", "方法 A 更快。", "方法 A 快一些。")
    sh(["git", "commit", "-qam", "仓库改同一行"], repo)
    edit(tmp_path / "pkg/main.tex", "方法 A 更快。", "方法 A 总是更快。")
    edit(tmp_path / "pkg/num.tex", "\\claimval{C-009}", "0.82")
    r = claims(repo, "import", str(tmp_path / "pkg"))
    assert r.returncode == 1 and "有冲突：paper/main.tex" in r.stdout, r.stdout
    report = next((repo / "imports").glob("*.md")).read_text(encoding="utf-8")
    assert "删取数宏 C-009；加数字 0.82；可能把取数宏换成了写死的数" in report, report
    assert "`paper/main.tex:1`（带结论标注）：加强弱词 总是" in report, report
    assert "有冲突，先解决：`paper/main.tex`" in report


def test_import_needs_base_without_base_file_and_keeps_missing(repo, tmp_path):
    write(repo / "paper/extra.tex", "附录。\n")
    sh(["git", "add", "-A"], repo)
    sh(["git", "commit", "-qm", "加附录"], repo)
    pkg = tmp_path / "hand"  # 别人手工拷走、缺了 extra.tex 的稿子
    write(pkg / "main.tex", PAPER.replace("方法 A", "方法 C"))
    r = claims(repo, "import", str(pkg))
    assert r.returncode == 1 and "--base" in r.stderr
    r = claims(repo, "import", str(pkg), "--base", "HEAD")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (repo / "paper/extra.tex").exists()
    report = next((repo / "imports").glob("*.md")).read_text(encoding="utf-8")
    assert "没有删除：`extra.tex`" in report, report


def test_import_unchanged_package_leaves_no_branch(repo, tmp_path):
    assert claims(repo, "export", str(tmp_path / "pkg.zip")).returncode == 0
    r = claims(repo, "import", str(tmp_path / "pkg.zip"))
    assert r.returncode == 0 and "没有改动" in r.stdout, r.stdout + r.stderr
    assert sh(["git", "branch", "--list", "import/*"], repo).stdout.strip() == ""
    assert sh(["git", "status", "--porcelain"], repo).stdout.strip() == ""


def run_env(repo, cmd, **env):
    return subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env={**os.environ, **env})


def test_hook_blocks_errors_and_stale_index(repo):
    assert claims(repo, "hook").returncode == 0
    commit = ["git", "commit", "-qam", "改"]
    claim(repo, "C-050", deps=["C-404"])
    sh(["git", "add", "-A"], repo)
    r = run_env(repo, commit, CLAIMS_PYTHON=sys.executable)
    assert r.returncode != 0 and "C-050: 依赖的 C-404 不存在" in r.stdout + r.stderr
    claim(repo, "C-050", deps=["C-009"])
    sh(["git", "add", "-A"], repo)
    r = run_env(repo, commit, CLAIMS_PYTHON=sys.executable)
    assert r.returncode != 0 and "git add 后再提交" in r.stderr, r.stdout + r.stderr
    sh(["git", "add", "-A"], repo)
    r = run_env(repo, commit, CLAIMS_PYTHON=sys.executable)
    assert r.returncode == 0, r.stdout + r.stderr
    write(repo / ".git/hooks/pre-commit", "#!/bin/sh\nexit 0\n")
    assert claims(repo, "hook").returncode == 1  # 别人的 hook 不覆盖


def test_jobs_and_wait_read_local_and_remote_status(repo, tmp_path):
    assert claims(repo, "runinfo", "exp14", "--out", "results/exp14/run.json").returncode == 0
    write(repo / "work/probe/run.json", json.dumps({"stage": "probe", "commit": "abc"}))
    write(repo / "work/probe/status.json", json.dumps({"state": "done", "progress": {"done": 3, "total": 3}}))
    feed = tmp_path / "feed.jsonl"
    write(feed, json.dumps({"task_id": "exp14-1c95bdf", "state": "working", "leased_until": 1795593106}) + "\n"
          + json.dumps({"task_id": "other-x", "state": "failed"}) + "\nnot json\n")
    env = {"CLAIMS_JOBS_CMD": f"cat '{feed}'", "CLAIMS_JOBS_PREFIX": "exp"}
    r = run_env(repo, [sys.executable, "tools/claims.py", "jobs"], **env)
    assert "| exp14 | working |" in r.stdout and "租约至" in r.stdout and "| 远端 |" in r.stdout, r.stdout
    assert "| probe | done |" in r.stdout and "进度 3/3" in r.stdout and "other-x" not in r.stdout
    w = [sys.executable, "tools/claims.py", "wait", "--every", "0.1", "--timeout", "0.5"]
    r = run_env(repo, w + ["exp14", "probe"], **env)
    assert r.returncode == 2 and "exp14: working" in r.stdout and "超时" in r.stdout, r.stdout
    write(feed, json.dumps({"task_id": "exp14-1c95bdf", "state": "done"}) + "\n")
    assert run_env(repo, w + ["exp14", "probe"], **env).returncode == 0
    write(feed, json.dumps({"task_id": "exp14-1c95bdf", "state": "failed", "exit_code": 1}) + "\n")
    assert run_env(repo, w + ["exp14"], **env).returncode == 1
    r = run_env(repo, w + ["exp14"], CLAIMS_JOBS_CMD="exit 3")
    assert r.returncode == 2 and "读取远端状态失败" in r.stdout, r.stdout  # 读失败要说出来，不能静默

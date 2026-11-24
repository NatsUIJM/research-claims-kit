# 结论台账工具包

把"结论、依赖、实验证据"写成文本文件，让人和 Agent 都能查三件事：某个结论用了哪些实验；改了代码或参数后，哪些结论的证据过期、数字变了；推翻一个结论会牵连哪些结论。依赖关系由人或 Agent 手写。工具不判断结论是否成立；结论的「已支撑 / 待验证」由工具按证据与依赖算，不手写，也不需要人批准（2026-11-24 起，见下文「状态」）。

## 文件

| 文件 | 用途 |
|---|---|
| `template/tools/claims.py` | 脚本，依赖写在文件头，`uv run` 时自动准备环境 |
| `template/tools/claims.py.lock` | 依赖锁定（dvc 3.67.1、pyyaml） |
| `template/claims/_template.md` | 结论文件模板（以 `_` 开头的文件不会被当作结论读取） |
| `template/AGENTS.claims.md` | 给 Agent 的规则，追加到论文仓库的 AGENTS.md 或 CLAUDE.md |
| `tests/test_claims.py` | 端到端测试 |

## 装进论文仓库

```bash
KIT="<本工具包目录>"
cd <论文仓库>
mkdir -p tools claims
cp "$KIT/template/tools/claims.py" "$KIT/template/tools/claims.py.lock" tools/
cp "$KIT/template/claims/_template.md" claims/
cat "$KIT/template/AGENTS.claims.md" >> AGENTS.md    # 或追加到 CLAUDE.md
uv run tools/claims.py hook           # pre-commit hook：提交前跑 check 与 index
uvx dvc init                          # 仓库还没用 DVC 时
uvx dvc config core.analytics false   # 关闭 DVC 默认的匿名使用统计
uv run tools/claims.py check
```

默认目录约定：结论在 `claims/`，论文在 `paper/**/*.tex`，源文件在 `code/`、`data/`、`params.yaml`、`dvc.yaml`，结果在 `results/`。仓库结构不同时，改 `claims.py` 顶部的常量。

## 命令

| 命令 | 作用 |
|---|---|
| `check` | 检查必填字段、编号格式、指向不存在编号的依赖、依赖成环、实验名是否在 `dvc.yaml` 中；警告依赖了已撤回或已替代的结论、论文仍引用已撤回的结论。有错误时退出码 1 |
| `impact <编号>` | 沿 `depends_on` 反向列出全部下游结论，给出依赖路径、理由、适用范围和论文位置；另列"引用同一实验的结论"。不改任何文件 |
| `stale` | 对每条证据跑 `dvc status <实验> --with-deps`，并把当前指标按论文快照的位数四舍五入后比较。有发现时退出码 1 |
| `runinfo <实验> --out <路径>` | 远端作业开始时记录当前 commit 和源文件是否有未提交改动 |
| `accept <实验>` | 核对运行记录：运行时没有未提交改动、之后源文件没再改过、本地也干净，通过后才执行 `dvc commit` |
| `index [--no-stale]` | 生成两个文件。`RESEARCH.md` 是会话开始读的短页：待处理（错误、警告、过期、待验证及原因；每类最多 10 条，其余只报条数）、各状态条数、最近 20 个提交改过的结论。`RESEARCH_ALL.md` 是全表：假设表、结论表、已撤回清单；论文位置记到节（`文件#节的 label 或标题`），不记行号 |
| `show <编号> [--no-stale]` | 一条结论的状态与原因、陈述、依赖及其状态、证据、直接下游、论文位置（现查行号） |
| `ls [--status 状态] [--kind A/C] [--stage 实验] [--text 文字] [--no-stale]` | 按条件列结论，每条一行 |
| `hook [--force]` | 安装 git pre-commit hook：提交前跑 `check`，再重新生成索引，有错误或索引变了就拒绝提交。已有别的 hook 时不覆盖 |
| `jobs` | 列出作业状态：本地 `results/`、`work/` 下的 `run.json` 及旁边的 `status.json`，加上 `JOBS_STATUS_CMD` 读到的远端状态 |
| `wait <作业…> [--every 秒] [--timeout 秒]` | 等作业结束，只在状态变化或远端读取出错时打一行；全部 done 退出码 0，有失败 1，超时 2。用后台方式运行 |
| `export <包.zip 或目录> [--tag 名]` | 把 `paper/` 里已提交的文件打包交给仓库外修改，包里写 `CLAIMS_BASE.txt`（基线提交号），并给该提交打 tag（默认 `export-<时间>`）。`paper/` 有未提交改动或 tag 已指向别的提交时拒绝 |
| `import <包> [--base 提交] [--note 说明]` | 在基线提交上开 `import/<时间>` 分支，把包覆盖进 `paper/` 并提交，再合并进当前分支（暂存、不提交），两边都改过的地方以冲突列出。编译产物、`.DS_Store` 不导入，外部存成 CRLF 的还原。在 `imports/` 写审查清单：文件增删；取数宏、结论注释、引用、交叉引用、标签的增删；数字的增删；强弱词（significantly、all、显著、无关等）的改动；其余改动只按文件计数。包里没有基线文件时必须用 `--base` 指明，且包里缺的文件不删 |

论文用 LaTeX 写时，在句子旁加注释 `%C-012`，`check`、`impact`、`show`、`index` 会自动找到位置；转义的 `\%` 不算注释。`check`、`impact`、`show` 现查行号；`index` 写进文件的只记到节，排版改动不会让索引变。文件里第一个节命令之前的注释（例如列出本文件结论的总注释）记作「文件头」。Word 稿在结论文件里写 `paper: §4.2 第2段`。

## 状态

结论文件平时不写 `status`。工具按以下规则算：A- 前提记为「前提」；C- 结论有证据或依赖、证据未过期、依赖的结论都已支撑（依赖前提只要求未撤回），记为「已支撑」，否则「待验证」并给出原因。只有撤回或被替代时手写 `status: retracted` / `superseded`。旧文件里写着的 `proposed`、`supported` 读入时忽略，不必改。`impact` 不查过期；`index`、`show`、`ls` 默认查，`--no-stale` 时不查。

改这一条的原因：原规则「改为 supported 先问人」在使用中只能整批照单批准（一次数学建模比赛里 6 次人批、0 次拒绝），人能核对的只有证据是否齐、是否过期、依赖是否成立，这些工具都能算。人的判断放在结果出来之前（判据、前提、读法）和推翻结论时（`impact`）。

## dvc.yaml 写法

一个表或一张图对应一个实验。依赖写入口脚本和共享函数目录；只写入口脚本的话，改了公共函数不会被发现。求解器版本放进 `params.yaml`，脚本开头断言实际版本一致，升级后不改参数就跑不过去，改了参数则相关实验自动显示过期。

```yaml
# dvc.yaml
stages:
  exp14:
    cmd: matlab -nojvm -batch "expect_mpver='${env.matpower}'; run('code/exp14.m')"
    deps:
      - code/exp14.m
      - code/lib
    params:
      - env.matpower
    metrics:
      - results/exp14/metrics.json:
          cache: false
```

```yaml
# params.yaml
env:
  matpower: "8.1"
```

```matlab
% code/exp14.m 开头
assert(strcmp(mpver, expect_mpver), 'MATPOWER 版本是 %s，params.yaml 写的是 %s', mpver, expect_mpver);
```

`${env.matpower}` 参数模板和版本断言的机制已用 Python 实验实测；上面的 MATLAB 命令没有在 MATLAB 上跑过。`v = mpver` 返回版本号字符串，依据是 MATPOWER 8.1 文档。

## 远端运行

1. 本地提交代码，远端拉到同一个 commit。
2. 远端作业第一步：`uv run tools/claims.py runinfo exp14 --out results/exp14/run.json`，然后执行 `dvc.yaml` 里该实验的命令。
3. 把 `results/exp14/` 拉回本地，执行 `uv run tools/claims.py accept exp14`，再提交 `dvc.lock` 和结果。

不要直接用 `dvc commit` 登记远端结果：实测改了代码不重跑就 `dvc commit`，`dvc status` 照样显示干净。

不是 dvc 实验的远端作业（探索、扫参）也在第一步跑 `runinfo`，`--out` 写到 `work/<名>/run.json`，这样 `jobs` 能看到它。

### 作业状态

工具不管作业怎么提交、在哪里跑，只认一种状态文件 `status.json`（每个作业一个）：`state`（queued / working / done / failed / canceled）、`started_at`、`updated_at` 或 `leased_until`、`exit_code`、`err_summary`、可选 `progress: {done, total}`，时间写 ISO 字符串或 Unix 秒。本地的放在 `run.json` 旁边；远端的由 `claims.py` 顶部的 `JOBS_STATUS_CMD`（或环境变量 `CLAIMS_JOBS_CMD`）读回：一条 shell 命令，输出每行一个 JSON，含 `task_id` 或 `name`。远端 `task_id` 等于作业名、或以「作业名-」开头时与本地记录合并；同一台机器上有别的项目时，用 `JOBS_PREFIX`（或 `CLAIMS_JOBS_PREFIX`）只列本项目的。

例：工作站的作业队列给每个作业写 `/srv/compute/runs/<task_id>/status.json`（字段同上），配置为

```python
JOBS_STATUS_CMD = "rsh -c 'for f in /srv/compute/runs/*/status.json; do cat \"$f\"; echo; done'"
JOBS_PREFIX = "<项目前缀>-"
```

换到集群时把这条命令换成读 `squeue` / `sacct` 并转成同样字段的命令即可，其余不变。等作业用 `claims.py wait <作业名>`，由 Agent 以后台方式启动，结束时 harness 只通知一次；不要在前台写 `until … sleep` 循环。

## 测试

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --with pytest --with pyyaml --with "dvc==3.67.1" pytest -q -p no:cacheprovider tests/
```

29 个测试覆盖：状态由工具计算（旧写法忽略、过期与依赖传导、原因）、`show` 与 `ls`；索引拆成短页与全表、位置记到节且排版改动后全表不变；pre-commit hook 拦下错误与过期索引、不覆盖别的 hook；`jobs` 合并本地与远端状态、按前缀过滤，`wait` 的完成、失败、超时与远端读取出错；外部改稿的导出（基线与 tag、有未提交改动或 tag 冲突时拒绝）与导入（两边改动三方合并、冲突列出、取数宏被换成数字、强弱词、编译产物与 CRLF、无基线文件时要 --base 且不删缺失文件、包未改时不留分支）；格式与引用错误、依赖环、撤回和替代后的警告、`impact` 只沿依赖反向走且不改文件、`supersedes` 不参与传播、上游代码改动和数值变化的发现、快照按位数比较、远端结果登记的拒绝情形（运行后又改过代码、运行时或本地有未提交改动、运行记录缺失或不属于该实验）与 `--force`、索引可重复生成。

## 暂不做

一个结论有多组独立支撑、适用范围单独建节点、"什么情况下推翻"字段、检测改了含义却原地改、PROV / RO-Crate 导出、依赖图可视化、MCP 封装、独立复核 Agent。需要时再补。

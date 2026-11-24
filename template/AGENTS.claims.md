# 结论台账规则

本仓库用 `claims/` 记录结论和前提假设，用 DVC 记录实验。`tools/claims.py` 只列出事实，不判断结论是否成立；结论的「已支撑 / 待验证」由它按证据与依赖算，不手写。

1. 每次会话开始先读 `RESEARCH.md`（待处理、概况、最近改动）和 `git log --oneline -n 20`。查单条用 `uv run tools/claims.py show <编号>`，按条件列用 `uv run tools/claims.py ls`，要看全表打开 `RESEARCH_ALL.md`。只按编号打开需要的 `claims/` 文件，不整读 `results/`。
2. 要写进论文的结论，先按 `claims/_template.md` 建文件（不写 status），再在论文对应句子旁加注释 `%C-xxx`。
3. 写 `depends_on` 时每条都写 `why`。只写人确认过、或你能说清理由的依赖。
4. 状态不手写，也不为它问人：有证据或依赖、证据未过期、依赖都已支撑，工具就记为已支撑。`RESEARCH.md` 里的「待验证」由你处理（补证据、重跑、改依赖）。人的判断放在结果出来之前：判据、前提与读法。只有这几种情况先跑 `impact`、把清单交给人：结论方向变了或要撤回；结果与事先写定的判据或最简估算冲突；要删除依赖。数值变了但方向不变时，你更新 `value` 快照，并在提交信息里写新旧值。
5. 只改措辞可以原地改。改了范围、条件或数值方向，就新建编号，写 `supersedes: 旧编号`，旧文件写 `status: superseded`。
6. 改了代码、参数或数据后跑 `uv run tools/claims.py stale`。推翻一个结论前跑 `uv run tools/claims.py impact <编号>`，把清单原样交给人逐条处理，不自行改下游结论。
7. `dvc.yaml` 里每个实验声明完整依赖（入口脚本、共享函数目录、用到的参数），指标文件用 `-M` 声明。本机运行用 `uvx dvc repro <实验名>`。
8. 远端运行：先提交代码；作业开始时执行 `uv run tools/claims.py runinfo <作业名> --out <结果目录>/run.json`（不是 dvc 实验的写到 `work/<名>/run.json`）；dvc 实验的结果拉回后用 `uv run tools/claims.py accept <实验名>` 登记，不直接用 `dvc commit`。远端有作业队列就走队列，在 `claims.py` 顶部配好 `JOBS_STATUS_CMD`。看状态用 `claims.py jobs`；等作业结束用 `claims.py wait <作业名>`，并以后台方式运行，不在前台写轮询循环。
9. 仓库装好 pre-commit hook（`uv run tools/claims.py hook`）：提交前自动跑 `check` 与 `index`，有错误或索引变了会拒绝提交，`git add` 后再提交。提交信息以编号开头，例如 `[C-012] 重跑 exp14: 0.82→0.79`。查某个结论的历史用 `git log --grep C-012`。
10. 论文交给仓库外修改（合作者、别的模型润色、Overleaf 上改）时，用 `uv run tools/claims.py export <包.zip>` 导出，改回来的包用 `uv run tools/claims.py import <包>` 并回，不整包覆盖 `paper/`；投稿版用 `export --tag submit-<n>` 标记。`import` 后逐条核对 `imports/` 下清单的第 2–4 节（取数与引用、数字、强弱词），结论强弱变了才问人，再跑 `check`、编译后提交。

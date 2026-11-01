# AgentBench Stage 1 / 1.5 独立审查清单

> 审查对象：`Globex-agent-showcase` 的 AgentBench Stage 1 资产、能力矩阵与冻结 catalog。
> 审查性质：**只读**。审查者不得修改正式证据、放宽测试、复用作者结论或把“命令跑过”当作“结论成立”。
> 每个小节都要求粘贴命令、退出码和关键原始输出。只写“通过”视为无效。

---

## 1. 审查范围与禁止事项

必须复核：

1. 冻结 inventory 的三个文件与 `template-catalog.json` 的哈希；
2. 冻结来源（commit / tree / scanner blob / generator blob）可复算；
3. catalog 能从冻结输入在干净副本中确定性重建；
4. 六类单点篡改 + 协调篡改 + 脏工作区冻结均被拒绝；
5. 数据卫生（无绝对路径、邮箱、密钥、`.env`、原始 prompt）；
6. 2131 / 1940 / 23 的语义不是“可执行题数”；
7. 能力矩阵为 23 项：18 `supported + verified`、4 `instrumentation_required`、1 `not_supported`；
8. 抽查 search / memory / session / trade / KB 五类运行时证据与 `observed_values`；
9. CI workflow 的路径覆盖、无模型密钥、artifact 上传。

禁止：

- 修改 `eval/agent/v1/evidence-probe.json`、`eval/agent/v1/inventory/**`、`eval/agent/v1/template-catalog.json`；
- 修改测试断言或跳过负例；
- 用“作者说已通过”“之前跑过”替代本次原始输出；
- 在正式仓库内制造脏工作区；所有篡改实验必须在临时副本或临时目录完成。

---

## 2. 环境与来源

在仓库根目录执行并粘贴输出：

```bash
REPO="/Users/hkh/Desktop/实习准备/Globex-agent-showcase"
PY="$REPO/.venv/bin/python"
cd "$REPO"
git rev-parse HEAD
git branch --show-current
git status --porcelain
git log --oneline -5
$PY --version
```

代理清理前缀（本机代理会污染本地 HTTP/httpx；后续命令统一使用）：

```bash
CLEAN="env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy -u NO_PROXY -u no_proxy"
```

记录：HEAD、当前分支、工作区是否为空、Python 版本。工作区非空即记一条环境异常，不直接判失败，但所有正例必须在干净副本中重跑。

---

## 3. 干净副本复现

不要在原仓库做正例。创建临时 clone：

```bash
TMP=$(mktemp -d)
git clone "$REPO" "$TMP/review"
cd "$TMP/review"
git checkout --detach "$(git -C "$REPO" rev-parse HEAD)"
git status --porcelain
```

在 clone 中运行：

```bash
$CLEAN $PY -m scripts.eval.agentbench.check_catalog --out /tmp/review-catalog.json
echo "exit=$?"
$CLEAN $PY -m scripts.eval.agentbench.capability_audit
echo "exit=$?"
$CLEAN $PY -m pytest -q -p no:cacheprovider \
  tests/test_agentbench_catalog_ci.py \
  tests/test_agentbench_capability.py \
  tests/test_agentbench_probe.py \
  eval/agent/v1/probes/test_evidence_probe.py
echo "exit=$?"
```

> clone 通常不含 `.venv`（被 gitignore）。上面的 `$PY` 指向原仓库虚拟环境，依赖可用；`check_catalog`/`capability_audit` 本身只依赖标准库与 git。若审查者选择在 clone 内新建环境，需按仓库说明安装依赖后再运行 pytest。

预期：`check_catalog` 输出 `{"passed": true, "errors": []}` 且退出 0；`capability_audit` 输出 `passed: true` 且 counts 为 18/4/1；pytest 全绿。

---

## 4. 哈希与来源复算

### 4.1 冻结文件与 catalog

```bash
cd "$TMP/review"
shasum -a 256 eval/agent/v1/inventory/source-inventory.json \
              eval/agent/v1/inventory/record-index.jsonl \
              eval/agent/v1/inventory/manifest.json \
              eval/agent/v1/template-catalog.json
```

口径必须区分：

- `manifest.inventory_file_sha256` / `record_index_file_sha256`：**文件字节**的 SHA-256；
- `shasum` 对 `template-catalog.json` 得到的是**文件字节**哈希；
- catalog 内部的 `catalog_sha256`：由 `template_catalog.build()` 对生成内容做的**语义哈希**，与文件字节哈希本来就不同。

因此：三个冻结文件用 `shasum` 对照 manifest 的文件哈希；catalog 的 `catalog_sha256` 由 `check_catalog` 重建后比较，不能用 `shasum` 去比。

### 4.2 commit / tree

```bash
python3 - <<'PY'
import json, subprocess
m = json.load(open("eval/agent/v1/inventory/manifest.json"))
for prefix in ("inventory_source", "freeze_implementation"):
    commit = m[f"{prefix}_commit"]; tree = m[f"{prefix}_tree"]
    actual = subprocess.run(["git", "rev-parse", f"{commit}^{{tree}}"], text=True,
                            capture_output=True, check=True).stdout.strip()
    print(prefix, commit, tree, "tree_ok=", actual == tree)
    print("exists=", subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"]).returncode == 0)
    print("ancestor=", subprocess.run(["git", "merge-base", "--is-ancestor", commit, "HEAD"]).returncode == 0)
PY
```

### 4.3 scanner / generator blob

```bash
python3 - <<'PY'
import hashlib, json, subprocess
m = json.load(open("eval/agent/v1/inventory/manifest.json"))
def blob(commit, path):
    return subprocess.run(["git", "show", f"{commit}:{path}"], capture_output=True, check=True).stdout
scanner = blob(m["inventory_source_commit"], "scripts/eval/agentbench/inventory.py")
generator = blob(m["freeze_implementation_commit"], "scripts/eval/agentbench/template_catalog.py")
print("scanner_match=", hashlib.sha256(scanner).hexdigest() == m["scanner_sha256"])
print("generator_match=", hashlib.sha256(generator).hexdigest() == m["generator_sha256"])
print("current_generator_match=", hashlib.sha256(open("scripts/eval/agentbench/template_catalog.py","rb").read()).hexdigest() == m["generator_sha256"])
PY
```

三项必须全为 `True`。

---

## 5. 正向重建

```bash
cd "$TMP/review"
$CLEAN $PY -m scripts.eval.agentbench.check_catalog --out /tmp/review-catalog.json
echo "exit=$?"
python3 - <<'PY'
import json
a = json.load(open("/tmp/review-catalog.json"))
b = json.load(open("eval/agent/v1/template-catalog.json"))
print("equal=", a == b)
print("sha_match=", a["catalog_sha256"] == b["catalog_sha256"])
print("groups=", a["group_count"], "records=", a["record_count"], "splits=", a["recommended_split_counts"])
PY
```

必须 `equal=True`、`sha_match=True`，且 groups=1185、records=2131、splits=592/388/205。

---

## 6. 负向篡改（必须全部非零退出）

所有实验在临时副本中做。先准备：

```bash
cd "$TMP"
rm -rf tamper && cp -R review tamper && cd tamper
```

### 6.1 record-index

```bash
echo '{"record_id":"tampered"}' >> eval/agent/v1/inventory/record-index.jsonl
$CLEAN $PY -m scripts.eval.agentbench.check_catalog; echo "exit=$?"
git checkout -- eval/agent/v1/inventory/record-index.jsonl
```

预期：`exit=1`，错误含 `record-index hash mismatch`。

### 6.2 source-inventory

```bash
python3 - <<'PY'
import json
p="eval/agent/v1/inventory/source-inventory.json"
d=json.load(open(p)); d["totals"]["raw_records"]=1
json.dump(d, open(p,"w"))
PY
$CLEAN $PY -m scripts.eval.agentbench.check_catalog; echo "exit=$?"
git checkout -- eval/agent/v1/inventory/source-inventory.json
```

预期：`exit=1`，错误含 `source-inventory hash mismatch`。

### 6.3 manifest

```bash
python3 - <<'PY'
import json
p="eval/agent/v1/inventory/manifest.json"
d=json.load(open(p)); d["counts"]["records"]=1
json.dump(d, open(p,"w"))
PY
$CLEAN $PY -m scripts.eval.agentbench.check_catalog; echo "exit=$?"
git checkout -- eval/agent/v1/inventory/manifest.json
```

预期：`exit=1`，出现 record count 不一致。

### 6.4 scanner

```bash
python3 - <<'PY'
import hashlib, json
inv="eval/agent/v1/inventory/source-inventory.json"
man="eval/agent/v1/inventory/manifest.json"
d=json.load(open(inv)); d["scanner_sha256"]="0"*64; json.dump(d, open(inv,"w"))
m=json.load(open(man)); m["scanner_sha256"]="0"*64
m["inventory_file_sha256"]=hashlib.sha256(open(inv,"rb").read()).hexdigest()
json.dump(m, open(man,"w"))
PY
$CLEAN $PY -m scripts.eval.agentbench.check_catalog; echo "exit=$?"
git checkout -- eval/agent/v1/inventory
```

预期：`exit=1`，错误含 `scanner_sha256 does not match inventory.py`。

### 6.5 generator

```bash
printf '\n# tampered\n' >> scripts/eval/agentbench/template_catalog.py
$CLEAN $PY -m scripts.eval.agentbench.check_catalog; echo "exit=$?"
git checkout -- scripts/eval/agentbench/template_catalog.py
```

预期：`exit=1`，错误含 `generator hash mismatch` 或 `generator_sha256 does not match`。

### 6.6 catalog

```bash
python3 - <<'PY'
import json
p="eval/agent/v1/template-catalog.json"
d=json.load(open(p)); d["record_count"]=1; json.dump(d, open(p,"w"))
PY
$CLEAN $PY -m scripts.eval.agentbench.check_catalog; echo "exit=$?"
git checkout -- eval/agent/v1/template-catalog.json
```

预期：`exit=1`，错误含 `does not match the committed catalog`。

### 6.7 inventory + manifest 协调篡改

与 6.4 相同，但把 `scanner_sha256` 改成另一个任意 64 位值、并同步更新 manifest 文件哈希。预期仍然 `exit=1`，因为 scanner 必须等于 **commit blob** 的哈希。

### 6.8 脏 tracked / staged 工作区冻结

```bash
cd "$TMP/review"
echo "" >> README.md
$CLEAN $PY -m scripts.eval.agentbench.freeze_inventory \
  --inventory eval/agent/v1/inventory --out /tmp/should-not-exist
echo "exit=$?"
git checkout -- README.md

echo "" >> README.md
git add README.md
$CLEAN $PY -m scripts.eval.agentbench.freeze_inventory \
  --inventory eval/agent/v1/inventory --out /tmp/should-not-exist
echo "exit=$?"
git reset --hard HEAD
```

预期两次都非零退出，输出含 `refusing to freeze from a dirty tracked/staged worktree`，且 `/tmp/should-not-exist` 不存在。

---

## 7. 数据卫生扫描

```bash
cd "$TMP/review"
grep -RInE '/Users/|/home/|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|sk-[A-Za-z0-9]{8,}|TAVILY_API_KEY|\.env' \
  eval/agent/v1/inventory eval/agent/v1/template-catalog.json
echo "grep_exit=$?"
```

预期：无匹配（`grep_exit=1`）。另检查 record-index 不含原始 prompt 文本：

```bash
python3 - <<'PY'
import json
rows=[json.loads(l) for l in open("eval/agent/v1/inventory/record-index.jsonl")]
bad=[r for r in rows if any("prompt" in k and k not in ("exact_prompt_sha256","normalized_prompt_sha256") for k in r)]
print("rows=", len(rows), "raw_prompt_rows=", len(bad))
print("keys=", sorted(rows[0]))
PY
```

预期：`raw_prompt_rows=0`，keys 仅包含 ID/哈希/family/group/split 等白名单字段。

---

## 8. 语义复核

必须明确写出并核对：

- `raw_records=2131`：原始资产记录数；
- `family_effective_cases=1940`：同 family 内折叠后的有效案例数；
- `assets=23`：登记的资产文件数；
- 三者**都不是**“可执行 Agent 评测题数”；2131 条记录不能对外表述为 2131 道题；
- catalog 的 1185 个 leakage group 只是模板分组，不是已生成任务。

从冻结输入复算：

```bash
python3 - <<'PY'
import json
m=json.load(open("eval/agent/v1/inventory/manifest.json"))
inv=json.load(open("eval/agent/v1/inventory/source-inventory.json"))
print("manifest_counts=", m["counts"])
print("inventory_totals=", inv["totals"])
print("semantics=", m.get("record_semantics"))
PY
```

---

## 9. 能力证据复核

```bash
cd "$TMP/review"
$CLEAN $PY -m scripts.eval.agentbench.capability_audit
echo "exit=$?"
```

必须为 23 / 18 / 4 / 1，`passed: true`。

抽查五类能力的 probe 记录与具体值：

```bash
python3 - <<'PY'
import json
d=json.load(open("eval/agent/v1/evidence-probe.json"))
for cap in ("search.catalog","memory.update","state.session_isolation","trade.approve","search.category_insight"):
    rows=[r for r in d["records"] if r["capability_id"]==cap]
    print("==", cap, "records=", len(rows))
    for r in rows:
        print(" ", r["observation_source"], r.get("case"), r["result"],
              "required=", r["required_fields"],
              "values=", r.get("observed_values"))
PY
```

必须确认：

- search：`hits` 与 `landed_price` 字段；
- memory：具体 statement/action，以及 update 后的存储值；
- session：两个 session 的原始 ID、两个不同哈希、A/B 偏好不串线；
- trade.approve：`0→1`、库存 `-1`；reject `0→0`、`delta=0`；
- KB：`hit_count>0`、`abstained=false`、`source/source_reference/source_type`，abstain case 满足 `abstained=true`、`hit_count=0`、`unanswerable=true`、`reason` 非空。

---

## 10. CI 审核

```bash
cd "$TMP/review"
cat .github/workflows/agentbench-catalog.yml
# 只扫描本 workflow 与两个脚本；不要扫其他 workflow（HARNESS_* 是合法配置）
grep -nE 'secrets\.|OPENAI_API_KEY|DASHSCOPE_API_KEY|LLM_API_KEY|ANTHROPIC_API_KEY' \
  .github/workflows/agentbench-catalog.yml \
  scripts/eval/agentbench/check_catalog.py \
  scripts/eval/agentbench/freeze_inventory.py
echo "grep_exit=$?"
```

说明：安全扫描代码里出现 `TAVILY_API_KEY` 字样是**字符串检测规则**，不是密钥；不能据此判泄漏。只匹配真实赋值/密钥格式。

必须确认：

- 触发路径覆盖 `eval/agent/**`、inventory/freeze/check/catalog/audit 脚本、`tests/test_agentbench_*.py`、`.github/workflows/**`；
- 不引用任何 secrets 或模型密钥；
- 有 `upload-artifact`；
- `check_catalog` 与 `capability_audit` 均以 `python -m` 运行；
- 准确表述：**重建脚本不调用模型、无需密钥、不产生付费请求；GitHub Actions 基础步骤仍访问 GitHub 服务**。

---

## 11. 判定标准

以下任一情况为**阻塞**：

1. 任一冻结文件/catalog/来源 commit/tree/blob 哈希无法复算；
2. 正向重建与 tracked catalog 不一致；
3. 六类单点篡改、协调篡改或脏工作区冻结未被拒绝；
4. 能力矩阵不是 23/18/4/1，或 `capability_audit` 不为 `passed:true`；
5. probe 记录缺 `observed_values`，或 required 字段未出现在 observed/values 中；
6. 冻结输入含绝对路径、邮箱、密钥、`.env` 或原始 prompt 文本；
7. 把 2131 条资产记录表述为可执行题；
8. workflow 引用密钥、缺路径覆盖或缺 artifact。

评分、历史印象、作者说明均不得掩盖上述阻塞。

---

## 12. 原始输出区（审查者填写）

对每一节粘贴：

```text
### 第 N 节
$ <命令>
<原始输出>
exit=<退出码>
结论：<通过/阻塞，及具体阻塞项>
```

必须包含：第 3、4、5、6.1–6.8、7、8、9、10 节的实际命令与退出码。不得只写“已验证”。

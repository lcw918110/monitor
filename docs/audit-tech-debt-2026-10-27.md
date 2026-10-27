# 技术债审计（2026-10-27）

| 基线 | `main` @ 文档编写时的 v1.10.0（`center/__init__.py` 与 `agent/__init__.py`） |
| 范围 | `center/`、`agent/`、`common/`、`scripts/`、`center/static/`、`docs/`、`tests/`、`config/`、`vendor/python/bundles.json` |
| 方法 | 通读文档与入口脚本；对照版本串、部署短码、报告空闲线、主机类型；抽查采集编排、存储和静态页调用的 API。加速卡解析以现有夹具测试为证据，不把「解析器复杂」本身当成缺陷 |
| 开放 PR | 审计时 `gh pr list --state open` 为空。加速卡按卡解析与 PCI 回退已在 #28、#27 合并，没有未合并的加速卡重写草稿 |

严重度：P0 会丢数据或把错误操作写成推荐路径；P1 会让现场升级或运维走错；P2 会让后续修改分叉或漏测。分类：**UNAMBIGUOUS** 本分支可以改；**NEEDS_HUMAN** 涉及产品、策略或架构分叉，本文只给选项和建议。

本轮没有发现 P0 代码缺陷。空闲线仍是代码里的常量：CPU 平均 <5% 且繁忙 <5%；有卡时加速卡同样（`center/reports.py` 的 `IDLE_AVG_LT` / `IDLE_BUSY_LT`）。本审计不改阈值，也不新增指标。

## 已在本分支改掉的 UNAMBIGUOUS 项

| ID | 改了什么 |
| --- | --- |
| PD-01 / AD-01 | README、`docs/08`、`docs/13` 不再把 `sync_tree.sh` + `systemctl restart` 或「重跑 deploy 再重启」写成已安装中心的升级办法。升级指向 `push_upgrade.sh` / `upgrade_center.sh` |
| TD-01 | `scripts/lib/sync_tree.sh` 被直接执行时退出 1，并说明它是函数库 |
| PD-02 | 新增 `docs/14-ops-runbook.md`：升级实际步骤、便携 Python、隐藏主机 |
| TD-02 | README 自测改为 `unittest discover -s tests`，不再漏掉 host meta、改址、升级、寒武纪、PCI、便携包等模块 |
| TD-03 | 测试锁住中心版本、Agent 版本和两个页面副标题一致 |
| PD-03 | `docs/07`、`docs/09`、`docs/05` 标明是历史快照；`docs/11` 不再建议往任意中心灌演示数据 |
| PD-04 | SRS 文首不再写「与代码冲突时以 v1.7 本节为准」 |
| PD-05 | `docs/08` 写明没有厂商工具时 PCI 仍可上报已知卡，利用率不当成 0 |
| PD-06 / PD-07 | 设计说明里的采集循环、时段文档里的页签与现行三页签对齐 |
| CS-01 / CS-02 / CS-05 / CS-07 | 会误导操作的版本文首、backlog 里缺 AMD、部署短码缺 `unknown`、U1 仍写成「正在做且是下一优先」 |
| AD-02（只写现状） | 运维说明写明 `push_upgrade.sh` 与便携包同步的目标目录固定是 `/opt/monitor`。要不要加 `--dir` 仍见下面的 NH-01，本分支不加 |

## 1. 技术债

### TD-01 直接跑同步函数库等于没升级 — P1 — UNAMBIGUOUS — 已修

- 区域：`scripts/lib/sync_tree.sh`、README「已有中心」
- 证据：该文件只定义 `sync_tree` 等函数，没有「被直接执行时去同步」的入口。README 曾写「优先用 `scripts/lib/sync_tree.sh` 把新代码同步到安装目录，再 `systemctl restart`」。
- 风险：操作者看到退出码 0（改之前）或只重启了旧进程，以为中心已经换成新代码。
- 处理：直接执行改为退出 1；文档改指向 `push_upgrade.sh`。

### TD-02 文档里的自测命令漏掉一半测试文件 — P2 — UNAMBIGUOUS — 已修

- 区域：`README.md`「自测」、`tests/test_*.py`
- 证据：旧命令列出 14 个模块。仓库里还有 `test_host_meta`、`test_readdress`、`test_upgrade_center`、`test_cnmon`、`test_pci_accel`、`test_accel_fixtures`、`test_python_bundle`、`test_network`、`test_netutil`、`test_rknn`、`test_aaa_sandbox`。`upgrade_center.sh` 自己用的是 `unittest discover -s tests`。
- 风险：改了改址、升级或寒武纪解析，按 README 自测仍是绿的。
- 处理：README 与升级脚本使用同一条 discover。

### TD-03 版本串写在两处 Python 和两处 HTML — P2 — UNAMBIGUOUS — 已用测试锁住

- 区域：`center/__init__.py`、`agent/__init__.py`、`center/static/index.html`、`center/static/deploy.html`
- 证据：四处都是 `1.10.0`。页面不读 `/api/v1/health` 的 `version`。
- 风险：只改 Python 或只改页面时，监测台副标题和上报的 `agent_version` 不一致。
- 处理：测试要求两处 `__version__` 相等，且两个页面包含 `v` + 该版本。不在本轮改成运行时从 health 填副标题（见 NH-06）。

### TD-04 读不到负载时上报 0 — P2 — NEEDS_HUMAN

- 区域：`agent/metrics/system.py` `sample_load`
- 证据：`getloadavg` 和 `/proc/loadavg` 都失败时返回 `load1/5/15 = 0.0`。异常判定把「没有 load1」跳过，但 0 会被当成真实负载。加速卡路径则明确不把缺失利用率当成 0。
- 风险：只在两种读取都失败时发生。Linux 上很少见；一旦发生，详情会显示 1 分钟负载 0，而不是「没有读到」。
- 选项：维持 0；或失败时写成 `null`，页面用已有的 `?? "-"` 显示横线，判定继续跳过。
- 建议：改成 `null`。本轮不动，避免在没有现场样本时改采集报文。

### TD-05 没有 CI、没有格式化或 lint 配置 — P2 — NEEDS_HUMAN

- 区域：仓库根目录。没有 `.github/workflows`、`pyproject.toml`、`Makefile`。
- 证据：测试只在本机和 `upgrade_center.sh` 的同步前步骤里跑。
- 风险：合并进 `main` 之前没有一条固定的检查。升级脚本会在中心上跑全套测试，但那是推送之后。
- 选项：不加 CI；或加一个只跑 `PYTHONPATH=. python3 -m unittest discover -s tests` 的工作流，不引入新依赖。
- 建议：加这一条工作流。本轮不加，避免在没人要求时改仓库的合并策略。

### TD-06 加速卡文本解析仍然脆，但已有夹具 — P2 — 记录，不改

- 区域：`agent/metrics/gpu.py`、`amd.py`、`npu.py`、`accelerators.py`
- 证据：英伟达走 CSV，AMD 走 JSON，寒武纪先试 `cnmon info -j`（临时目录）再按整行 `Card N` 解析；失败的 stderr 进 note。`tests/fixtures/accel/` 与 `test_cnmon.py`、`test_npu.py`、`test_amd.py`、`test_pci_accel.py`、`test_accel_fixtures.py` 覆盖这些路径。PCI 回退只认已知厂商 ID，健康 `abnormal`，利用率留空。
- 风险：厂商改一版输出仍可能漏字段。这是采集器的常态，不是半成品重写。
- 处理：保持「结构化输出优先、部分字段保留、夹具回归」。不在本轮抽一层新框架（见 NH-04）。

### TD-07 旧阈值键 `npu_*` 与 `accel_*` 并存 — P2 — 保持兼容，不删

- 区域：`center/anomaly.py`、`center/static/app.js`
- 证据：默认值两套相同；判定先读 `accel_*`，没有再读 `npu_*`。
- 风险：删掉别名会让仍写着 `npu_temp_warn_c` 的旧 `center.json` 悄悄回到默认值。
- 处理：留下。不是待删除的死代码。

## 2. 架构债

### AD-01 升级入口在文档里分成了三条 — P1 — UNAMBIGUOUS — 已修

- 区域：README、`docs/08-cpu-npu-focus.md`、`docs/13-diagnostic-reports.md`、`scripts/push_upgrade.sh`、`scripts/upgrade_center.sh`
- 证据（改之前）：脚本注释和 `docs/10-roadmap.md` O5 写明升级只走 `push_upgrade.sh` → 新包里的 `upgrade_center.sh`。README 同一篇里先推荐 `sync_tree.sh` 再重启，后面又禁止手写 `systemctl restart`。`docs/13` 写「重新执行 `deploy_center.sh`（或现有的 systemd 重启方式）」，并带有某一现场库约 1.9GB 的说法。`docs/08` 写已有中心「更稳妥」的是 sync 后重启，以免重写单元。
- 现状：`upgrade_center.sh` 会重写单元文件，但保留原 `ExecStart` 和已有 `Environment=`。先在新包上跑测试，失败不备份、不同步、不重启；健康检查失败不覆盖 `data/`。`deploy_center.sh` 没有这套同步前测试和回滚。
- 处理：文档改成首次安装用 `deploy_center.sh`，已安装中心的升级只用 `push_upgrade.sh`。1.9GB 不写进通用步骤。

### AD-02 升级和便携包的目标目录写死 `/opt/monitor` — P1 文档 / 行为分叉 — 文档已写明；改脚本见 NH-01

- 区域：`scripts/push_upgrade.sh`（`--dir /opt/monitor`）、`scripts/push_python_bundles.sh`（`REMOTE_DIR=/opt/monitor/vendor/python`）、`scripts/deploy_center.sh`（非 root 默认 `~/monitor`）
- 证据：首次安装在非 root 时可以落到家目录。升级命令没有 `--dir`，便携包远端目录也不能改。
- 风险：对 `~/monitor` 跑 `push_upgrade.sh` 会去找 `/opt/monitor`，目录不存在就退出；若 `/opt/monitor` 是另一套安装，则会升级那一套。
- 处理：运维说明写明这条限制。是否让脚本接受目录，见 NH-01。

### AD-03 报告阈值有的在配置里，有的是代码常量 — P2 — NEEDS_HUMAN

- 区域：`config/center.example.json` 的 `period_util` 与 `anomaly`；`center/reports.py` 的 `IDLE_AVG_LT = 5.0`、`IDLE_BUSY_LT = 0.05`、`HIGH_BUSY_GTE = 0.40`、`HIGH_P95_GTE = 85.0`
- 证据：时段繁忙默认 CPU / 加速卡 ≥80%，可以改配置或查询参数。诊断报告的「基本不用」5% 和「高负荷」40% / P95 85% 不在 `center.json`。
- 风险：有人把 `period_util` 改成 70 时，会以为诊断报告的繁忙线也变了。空闲线按产品规定就是 5%，不该跟着配置漂。
- 选项：维持常量，并在报告抬头继续写明 5% / 40% / 85%；或把高负荷两档放进配置，空闲线仍固定 5%。
- 建议：三档都保持代码常量。空闲线是产品规则，不是站点参数。本轮不改数字，也不加配置键。

### AD-04 采集仍是「编排层 + 厂商模块」，不是可插拔指标框架 — P2 — NEEDS_HUMAN（已有路线 N2）

- 区域：`agent/metrics/accelerators.py` 调用 `gpu.py` / `amd.py` / `npu.py`，再合并 PCI。中心 `storage.py`、`anomaly.py`、`app.js` 认识固定字段。
- 证据：`docs/10-roadmap.md` N2 仍是 `todo`。没有半成品插件接口。
- 风险：加一种指标要改采集、存储、API 和页面。这是当前架构，不是坏掉的重构。
- 选项：继续在 `agent/metrics/` 加模块；或按 N2 做「指标名 → 展示」配置。
- 建议：新厂商继续走现有编排和夹具。N2 单独做，不跟文档纠偏绑在一起。

### AD-05 单进程 SQLite，容量没有实测数字 — P2 — NEEDS_HUMAN（已有路线 N4）

- 区域：`center/storage.py`、`center/server.py`、`docs/10-roadmap.md` N4 / O3
- 证据：`retention_days` 只删 `metrics_history`，不删 `diagnostic_reports`。没有压测报告。
- 风险：用文档里的「数十到低百台」当成承诺。路线图已写明那是待验证的预期。
- 建议：不在本轮猜台数。容量结论等 N4。

### AD-06 监测台和部署 API 不校验 Token — P2 — NEEDS_HUMAN（已有路线 U3 / O5）

- 区域：`center/api.py`、`docs/13`、README
- 证据：`token` 只在 `POST /api/v1/metrics` 校验。隐藏、删除、改址、部署、生成报告都可以被能访问端口的人调用。
- 风险：中心端口暴露在不可信网络时，这是缺省行为，不是漏洞修复能悄悄加上的。
- 选项：维持现状；或按 U3 做可选管理 Token，不启用时与现在一致。
- 建议：保持不启用。不要把 Agent Token 扩大成网页登录。

### AD-07 报告推送挂钩是空函数 — P2 — NEEDS_HUMAN（已有 H1）

- 区域：`center/report_schedule.py` `deliver_report`
- 证据：成功写入后调用，函数直接 `return None`。文档写明企业微信 / 钉钉 / 飞书未发送。
- 建议：保持空函数，直到有人提供通道和凭证。

### AD-08 中心与 Agent 的边界是清楚的 — 记录

Agent 只采集并 POST。日/周/月报告、隐藏、改址、资源组都在中心。隐藏不要求升级 Agent。这条不要改成 Agent 侧过滤。

## 3. 过程与操作文档

### PD-01 升级说明自相矛盾 — P1 — UNAMBIGUOUS — 已修

见 AD-01。操作步骤以 `docs/14-ops-runbook.md` 为准。

### PD-02 缺少一份和脚本一致的「怎么干活」 — P1 — UNAMBIGUOUS — 已修

- 区域：升级、便携 Python、隐藏主机原先散落在 README 和多篇 docs 里，其中升级段落互相打架。
- 处理：`docs/14-ops-runbook.md` 按 `push_upgrade.sh`、`upgrade_center.sh`、`push_python_bundles.sh`、`fetch_python_bundle.sh` 和隐藏接口的现行行为写。不引入新的备份或重启步骤。

### PD-03 历史记录会被当成现行步骤 — P2 — UNAMBIGUOUS — 已修

- 证据：`docs/07-morning-report.md` 仍写「看告警条」、health `1.1.0`，并建议页面空时跑 `demo_seed.py`。`docs/09-two-round-opt.md` 写判定增加了 load5（现行 `center/anomaly.py` 只看 load1/核数）。`docs/11-period-utilization.md` 写无数据时对一个 URL 灌演示点。README 则写试用不要跑 `demo_seed.py`。
- 处理：三篇历史或时段文档加了醒目的范围说明。`scripts/demo_seed.py` 的说明改为只打本地可丢弃的库，并注明它故意发送旧主机类型 `app` / `npu`（中心会归一成 cpu / gpu）。

### PD-04 SRS 要求冲突时相信过期文首 — P2 — UNAMBIGUOUS — 已修

- 证据：`docs/01-requirements.md` 文首标「实现现状（v1.7）」且「与代码冲突时以本节为准」。正文仍有起草时的「本期不做」和仅英伟达的句子。
- 处理：文首改为 v1.10.0 的摘要，冲突时以代码和 `docs/14` 为准，并应改文档。不整篇重写 SRS（见 NH-02）。

### PD-05 加速卡文档漏了 PCI 回退 — P2 — UNAMBIGUOUS — 已修

- 证据：`docs/08` 写「无对应工具时该项为空列表」。代码在 `collect_pci_accelerators`：已知 PCI ID 仍上报，健康 `abnormal`，利用率不进入 0% 样本。
- 处理：改成与 README、路线图同一说法。

### PD-06 详细设计仍写 `collect_gpu()` — P2 — UNAMBIGUOUS — 已修

- 证据：`docs/03-design-and-test.md` 的调度伪代码。`agent/main.py` 调用的是 `collect_accelerators()`。

### PD-07 时段文档的页面只有两个页签 — P2 — UNAMBIGUOUS — 已修

- 证据：`docs/11` 写监测台只有实时和时段。现行还有「使用诊断」。

### PD-08 文档索引不完整 — P2 — UNAMBIGUOUS — 已修

- 证据：README 索引没有 `docs/04-engineering-process.md`，也没有本审计和运维说明。`docs/07`、`docs/09` 容易被当成现行手册。

## 4. 一致性

### CS-01 多篇文档的版本文首落后于 v1.10.0 — P2 — UNAMBIGUOUS — 已修会误导操作的那些

- 证据：产品版本是 1.10.0。`docs/08` 标题带 v1.4，`docs/06` 标题带「下一步（v1.4+）」，`docs/01` 文首 v1.7，`docs/07` 表内 v1.1.0。
- 处理：操作类文档的文首改到现行基线。`docs/07` / `docs/09` 保留当时的版本表，只加「这是历史记录」的文首，避免改写当时的结论。

### CS-02 backlog 的多厂商一行漏了 AMD — P2 — UNAMBIGUOUS — 已修

- 证据：`docs/06` O13 写「英伟达 / 昇腾 / 寒武纪 / RKNN」。代码和 README 包含 AMD（`rocm-smi` / `amd-smi`）。

### CS-03 历史文档写「判定 load5」，代码只判定 load1/核数 — P2 — NEEDS_HUMAN

- 证据：`docs/09` 第 1 轮表格。`center/anomaly.py` 与 `center/static/ux.js` 的高负载用 `load1 / cpu_count` 对比 `cpu_load_per_core_warn`（默认 2.0）。load5、load15 只采集和展示。
- 选项：维持只判定 load1；或把 load5 也纳入着色。
- 建议：维持 load1。U1 的「高负载」已经按这条定义。改判定会改变谁变黄。

### CS-04 「总览」是实时页，不是独立页面 — P2 — 记录（路线图 U2 已排队）

- 证据：`center/static/index.html` 顶栏「总览」指向 `#realtime`。部署仍是 `/deploy.html`。路线图写明把部署收进同一页、以及升级引导界面，属于 U2，还没做。
- 处理：不在本轮改信息架构。

### CS-05 部署短码表缺 `unknown` — P2 — UNAMBIGUOUS — 已修

- 证据：`center/deploy_errors.py` 有 `UNKNOWN = "unknown"`。README 失败短码表没有这一行。其余短码与代码常量一致（含 `python_bundle_*`、`package_incomplete`、`sudo_required`）。

### CS-06 页面中文与空闲口径 — 抽查未发现要改的文案

- 证据：实时页写「长期空闲仍看使用诊断」。报告模块的说明字符串写平均与繁忙都低于 5%，并写明不再使用 15%/10%。`tests/test_reports.py` 覆盖「CPU 平均 14%、加速卡平均 9% 不是基本不用」。
- 处理：不改阈值，不改这些句子。

### CS-07 README「下一步」仍把 U1 写成正在做的下一优先 — P2 — UNAMBIGUOUS — 已修

- 证据：v1.10.0 已有顶栏、今日关注四类、白话健康、报告里 `#host=`。路线图 U1 状态仍是 `doing`，因为长期空闲名单没做。下一优先在路线图「接着」一节是 U2。
- 处理：README 与 `docs/08` 文末改成同一说法。路线图里的 `doing` 保留，那是范围状态，不是过期步骤。

## 5. NEEDS_HUMAN（请人拍板，本分支没有实现）

下面每条都**没有**在本分支改行为。和上面「已修」不重叠。

### NH-01 非 root 中心要不要走 `push_upgrade.sh`

- 现状：命令固定 `--dir /opt/monitor`，便携包固定 `/opt/monitor/vendor/python`。`deploy_center.sh` 在非 root 时默认 `~/monitor`。
- 选项 A：生产中心一律 root + `/opt/monitor`。家目录只用于本机试用，试用用 `local_up.sh`，不走 `push_upgrade.sh`。
- 选项 B：给 `push_upgrade.sh` 和 `push_python_bundles.sh` 加 `--dir`，默认仍是 `/opt/monitor`，便携包目录跟着这个目录下的 `vendor/python`。
- 建议：选 A，除非确实有长期跑在 `~/monitor` 上的中心。选 B 的默认值不能改，否则会把现有现场升错目录。

### NH-02 要不要重写 SRS 正文

- 现状：`docs/01` 文首已改成摘要，并声明冲突时以代码为准。编号需求里仍有起草时的范围（例如告警「本期不做」、GPU 只写英伟达）。
- 选项 A：保持正文为历史基线，只维护文首和 `docs/14`。
- 选项 B：按 v1.10.0 重写第 3 节以后，使 SRS 单独可读。
- 建议：选 A。重写 SRS 不改变行为，但容易在抄写时改入口径。

### NH-03 空闲 5%、高负荷 40% / P95 85% 要不要进配置

- 见 AD-03。建议都保持代码常量。不要把 5% 做成可配，以免站点改出另一套「基本不用」。

### NH-04 指标框架化（路线图 N2）

- 见 AD-04。建议单独立项，不要在纠偏文档时顺手做。

### NH-05 管理 API 鉴权与报告推送

- 见 AD-06、AD-07。建议继续按路线图 U3 / H1 搁置。本轮不要加登录，也不要接 Webhook。

### NH-06 页面版本要不要改成读 `/api/v1/health`

- 现状：副标题写死 `v1.10.0`，测试保证它和 `__version__` 一致。
- 选项 A：维持写死 + 测试。
- 选项 B：页面加载后用 health 的 `version` 替换副标题。
- 建议：选 A。health 失败时写死的副标题仍然能看；动态替换要处理请求失败和闪烁。

### NH-07 读不到负载时上报 0 还是 null

- 见 TD-04。建议改成 null，但等有人确认现场是否依赖「总有一个数字」。

### NH-08 要不要加只跑现有单元测试的 CI

- 见 TD-05。建议加，且不要在 CI 里装监控栈或跑真实 SSH。

### NH-09 异常判定要不要纳入 load5

- 见 CS-03。建议不要。高负载继续用 load1/核数。

### NH-10 中心容量与高可用

- 见 AD-05 和路线图 N4、H4。在压测之前不要写「能撑多少台」，也不要拆库或做双中心。

## 6. 刻意没做

- 不改报告阈值，不新增指标，不把长期空闲名单做进总览（路线图已把它留在使用诊断）。
- 不把部署页并进监测台，不做升级引导界面（U2）。
- 不给 `push_upgrade.sh` 加 `--dir`（NH-01）。
- 不删 `npu_*` 配置别名，不把 `gpu.py` / `npu.py` 收成一个文件。
- 不重写 SRS、竞品矩阵或 2026-07 的晨报正文。
- 不新增 GitHub Actions（NH-08）。
- 不改 Agent 与中心的数据边界，不用笔记本 scp 作为文档中的安装办法。

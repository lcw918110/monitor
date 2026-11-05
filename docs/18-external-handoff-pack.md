# 对外交付资料清单

本文是交给客户机房时的索引：交哪些文件、交给谁、现场要自备什么、装完如何签字。操作步骤不在这里重复，部署看 [`16-external-deploy-guide.md`](16-external-deploy-guide.md)，日常使用看 [`17-external-user-guide.md`](17-external-user-guide.md)。

手册按仓库 README 所标的现行版本 **v1.10.0** 编写。交付时把本次实际交出的标签和提交 SHA 填进下表。若仓库没有名为 v1.10.0 的 git 标签，以提交 SHA 为准，版本栏仍写 README 里的版本号。

## 1. 本次交付记录

| 项 | 填写 |
| --- | --- |
| 产品版本（README） | v1.10.0 |
| git 标签 | |
| 提交 SHA | |
| 交付日期 | |
| 交付方 | |
| 接收方 | |
| 中心计划地址 | `http://<中心IP>:8080` |
| 生产目录 | `/opt/monitor`（固定，不按用户拆分） |

源码树就是这一次标签或 SHA 的完整仓库。不要只拷贝 `center/` 目录：中心安装树必须带 `agent/`，否则无法打包采集端。便携 Python 的 tar.gz 不在 git 里，若目标机可能没有 Python 3.6+，按第 4 节另交或在现场有网的机器上下载。

## 2. 交给谁

| 接收人 | 至少要拿到 | 用来做什么 |
| --- | --- | --- |
| 客户系统管理员 / 机房运维 | 源码、本节第 3 项全部、配置样例、运行手册 | 安装中心、部署采集端、升级、备份、验收 |
| 日常使用人员 | 使用手册，以及中心的访问地址 | 看监测台、导入资产、读诊断报告 |
| 双方交接人 | 本清单，含第 6 节签字表 | 确认交了什么、装完是否可接收 |

管理 Token、SSH 密码、Webhook 由客户在自己的中心上生成或填写。交付包里不要夹带交付方环境的口令、私钥或机器人地址。

## 3. 必交文件

路径都相对于源码根目录。

| 路径 | 页 / 内容 | 说明 |
| --- | --- | --- |
| `README.md` | 全文，尤其「唯一部署方式」「部署失败短码」「主要 API」「文档」 | 产品入口。API 全表以这里为准 |
| `docs/16-external-deploy-guide.md` | 全文 13 节 | 对外部署手册 |
| `docs/17-external-user-guide.md` | 全文 9 节 | 对外使用手册 |
| `docs/18-external-handoff-pack.md` | 本文件 | 交付索引和验收表 |
| `docs/14-ops-runbook.md` | 全文 | 升级、便携 Python、备份、恢复、回滚的现行步骤 |
| `docs/13-diagnostic-reports.md` | 全文 | 日/周/月报告的时间窗、闲忙口径、推送 |
| `config/center.example.json` | 一份样例 | 中心配置默认值。复制思路见旁注，生产文件是中心机上的 `config/center.json` |
| `config/center.example.md` | 全文 | 每个配置键的含义。设置页说明与此一致 |
| `config/hosts.example.txt` | 全文 | `deploy_fleet.sh` 的清单格式。其中地址是示例 |
| `config/hosts-meta.example.csv` | 全文 | 资产导入列示例。人名和地址是虚构的，凭据列会被丢弃 |
| `config/agent.example.json` | 一份样例 | 采集端配置长什么样。正式文件由部署写入目标机的 `config/agent.json`，中心地址不要保留样例里的 `127.0.0.1` |

同时交付实现这些文档的同一份源码，包括 `center/`、`agent/`、`common/`、`scripts/`、`config/`。`scripts/deploy_center.sh`、`scripts/push_upgrade.sh`、`scripts/deploy_fleet.sh`、`scripts/deploy_agent.sh`、`scripts/backup_center.sh`、`scripts/restore_center.sh`、`scripts/uninstall.sh`、`scripts/fetch_python_bundle.sh`、`scripts/push_python_bundles.sh` 都在这棵树里。

## 4. 可选文件

客户需要估容量或对照架构时再交。不作为安装的前置阅读。

| 路径 | 何时交 |
| --- | --- |
| `docs/15-capacity-lab.md` | 客户要在自己的机器上跑容量探针时。文中的方法可用。仓库里若已经有一行开发机实测，那一行不是客户现场的容量结论，也不构成主机台数承诺。结果表由客户重跑后自己填写 |
| `docs/02-architecture.md` | 客户要看模块划分和数据表时 |
| `docs/01-requirements.md` | 客户要一份可单独阅读的现行需求时 |
| `docs/11-period-utilization.md` | 客户要核对时段统计口径时 |
| `docs/12-disk-mounts.md` | 客户要核对哪些磁盘挂载计入汇总时 |
| `vendor/python/*.tar.gz` | 目标机可能没有 Python ≥ 3.6 时，在有互联网的机器执行 `scripts/fetch_python_bundle.sh` 后单独拷贝。不要期待 git 检出里自带这些文件 |

## 5. 不纳入本次对外操作依据

下列文件仍在仓库中，给内部排期或历史对照，不作为客户安装和日常操作步骤：

- `docs/10-roadmap.md`（尚未交付的后续项）
- `docs/audit-tech-debt-2026-10-27.md`
- `docs/03-design-and-test.md`、`docs/04-engineering-process.md`、`docs/05-competitive-analysis.md`、`docs/06-optimization-backlog.md`、`docs/08-cpu-npu-focus.md`
- `docs/07-morning-report.md`、`docs/09-two-round-opt.md`（历史记录）

客户若在这些文件里看到和三份对外手册不一致的说法，以 README、`docs/14-ops-runbook.md` 和脚本为准。

## 6. 客户自备的条件

交付方不提供下列秘密。由客户在安装前准备好，并只保存在客户自己的中心机上。

| 项 | 要求 | 备注 |
| --- | --- | --- |
| 中心机 | 一台 Linux，root，Python 3.8+，建议 systemd | 生产目录将是 `/opt/monitor` |
| 中心登录 | 客户自己的 root SSH，供以后从工作站执行 `push_upgrade.sh` | 工作站要有同一份 git 源码 |
| 中心对外地址 | 客户确定的 IP 或域名，形如 `http://<中心IP>:8080` | 采集端不要填 `127.0.0.1` |
| 防火墙或安全组 | 运维浏览器和各采集端能访问中心 TCP 端口（默认 8080） | 改了端口就放行那个端口 |
| 目标机 SSH | 每台的地址、端口（默认 22）、用户，以及密码或私钥 | 私钥文件放在**中心机**上。密码部署要求中心机安装 `sshpass` |
| 目标机到中心 | 每台被监控机器能访问中心 URL | 采集端是主动上报 |
| 目标机 Python | 3.6+，或允许安装，或接受便携包 | Ubuntu 16.04 自带 3.5.2 不够 |
| 管理 Token | 客户自定，至少 8 位、不含空格 | 对外前在设置页保存。只存哈希 |
| Agent 上报 Token | 可选。客户自定 | 写在 `center.json`，与管理 Token 不是同一把。非空时采集端必须带同一口令 |
| 报告 Webhook | 可选。客户自己的企业微信、钉钉或飞书机器人 | 钉钉请用包含「诊断」的自定义关键词。当前版本不算加签 |
| 加速卡工具 | 可选。客户在目标机自行安装厂商工具 | 产品不捆绑驱动。没有工具时的 PCI 回退见部署手册 |
| 时钟 | 中心机时间准确 | 诊断日界按 Asia/Shanghai（UTC+8） |

SSH 密码会以明文进入中心数据库和备份。请客户限制 `/opt/monitor/data` 与 `/opt/monitor-backup-*` 的访问权限。这一点写在使用手册第 8 节。

## 7. 部署前检查

由客户管理员在动手安装前勾选。

- [ ] 已拿到第 3 节的源码，并记下第 1 节的标签或 SHA
- [ ] 中心机为 Linux，可以 root 登录，Python 不低于 3.8
- [ ] 已确定中心对外 URL，不是 `127.0.0.1`
- [ ] 浏览器所在网段和各目标机能访问该 URL 的端口
- [ ] 中心机能 SSH 到目标机；端口已确认（默认 22），不依赖扫描
- [ ] 密码登录已在中心机安装 `sshpass`，或已把私钥放到中心机并记下路径
- [ ] 已计划采集端目录 `/opt/monitor-agent`，与中心 `/opt/monitor` 分开
- [ ] 已知目标机 Python 版本；低于 3.6 的机器已准备便携包或允许安装
- [ ] 已准备在端口对外开放之前设置管理 Token
- [ ] 已阅读部署手册第 12 节的禁止项（旁路 scp、采集端覆盖中心目录、手写 rsync 当升级、双中心）

## 8. 部署后验收签字

在客户现场做。步骤对应部署手册第 11 节。未通过的项写在备注里，不记为接收。

| # | 检查 | 期望 | 通过 |
| --- | --- | --- | --- |
| 1 | 中心机 `curl -sS http://127.0.0.1:8080/api/v1/health` | JSON 里 `ok` 为 true，并有 `version` | [ ] |
| 2 | 另一台机器访问 `http://<中心IP>:8080/api/v1/health` | 同样 `ok` 为 true | [ ] |
| 3 | `GET /api/v1/hosts` | 返回 JSON，含 `hosts`。尚无上报时数组为空可以接受 | [ ] |
| 4 | 浏览器打开 `/`、`/deploy.html`、`/settings.html` | 三页都能打开 | [ ] |
| 5 | 管理 Token | 已在设置页启用，或书面记录本次暂不启用及原因 | [ ] |
| 6 | 至少一台采集端 | 部署清单为「部署成功」，无失败短码 | [ ] |
| 7 | 样本 | 一个上报间隔后，监测台该机为在线，详情有 CPU / 内存 / 磁盘 | [ ] |
| 8 | 中心地址 | 采集端配置不是 `127.0.0.1` | [ ] |
| 9 | 使用诊断 | 能打开预览时间窗。已有足够样本时可生成一份报告并再次打开；样本不足时的失败记录要能在列表里看到原因 | [ ] |
| 10 | 备份 | 已执行一次 `backup_center.sh`，或书面约定首次备份的时间 | [ ] |
| 11 | 目录 | 中心在 `/opt/monitor`，采集端不在这个目录里覆盖中心 | [ ] |

| 角色 | 姓名 | 日期 | 签字 |
| --- | --- | --- | --- |
| 交付方 | | | |
| 客户管理员 | | | |
| 客户使用负责人（可选） | | | |

备注：

```text




```

未测项不要写成已通过。容量台数、双中心切换都不在本次验收里。主机规模以客户日后按 `docs/15-capacity-lab.md` 在自己机器上的实测为准。

# JT 灵感工作台 · 社区版审核稿

一个单用户、自部署的 JT 创作流程：输入提示词 → 使用自己的模型 API → 查看任务与图片 → 收藏 → 下载 PNG / 导出 JSON。

**当前状态：本地审核候选版，尚未发布。2026-10-22 经所有者确认，此独立社区目录采用 AGPL-3.0-only；不改变原仓库或商业版许可证。没有连接、修改或部署原生产服务。**

已在隔离目录安装安全修复版本 Pillow 13.0.0，完成新版依赖回归（73 项通过，6 项真实浏览器测试跳过）。不要部署先前固定 Pillow 12.3.0 的首包。具体状态见 [测试报告](TEST-REPORT.md) 与 [依赖清单](THIRD-PARTY.md)。

从 JT 源码快照 `294bf689662fec2583a439bcf9f5a841cb8eff07` 提取既有 Images API 流程，并补上独立单用户服务边界。不是把原项目换名，也不是把所有 JT 功能搬过来。原商业开发不受此目录影响。

## 保留的完整流程

- 中文创作界面、正向和负向提示词。
- 使用服务端配置的一个 Images API 模型；API key 不进入浏览器、任务记录或导出。
- 方图、竖图、横图；质量参数和裁切 / 留白；兼容供应商须支持这些参数。
- 保留 JT 的提示词整理、画布约束、安全下载、图片验证与本地 PNG 归档逻辑。过长提示词会按优先顺序压缩至 1,600 字符总预算。
- 持久任务历史、收藏、PNG 下载、JSON 创作参数导出。
- 单用户访问口令、HttpOnly 会话、同源和 CSRF 校验、固定静态文件白名单、有限并发。
- 同一请求 ID 始终只生成一次；网络失败、服务重启、结果未明时不自动再次发起收费请求。

## 不在本审核稿中

多用户 / 多租户、订阅支付、额度计费、运营后台、客户定制、云端工作流 ID、ComfyUI / RunningHub、完整画风与提示词素材库、视频、生产运维脚本、用户上传、原有客户媒体及数据迁移。

本版本适合一个主人自用，不适合共享给多个客户。不要把同一个口令分发给客户，也不要把单用户数据目录接到商业服务。

## 先跑本地测试

需要 Python 3.12+。唯一运行依赖是 Pillow；测试使用标准库 unittest。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

浏览器回归测试为显式可选项，需要本机已安装 Playwright 和 Chromium：

```bash
JT_BROWSER_TESTS=1 python -m unittest tests.test_ui_contract -v
```

可用 `JT_CHROMIUM_EXECUTABLE=/你的/浏览器路径` 指定 Chromium。默认测试会把这六项浏览器流程列为 skipped，不代表通过。审核环境中浏览器未能启动；详见 [测试报告](TEST-REPORT.md)。

Windows PowerShell 激活命令为 `.venv\Scripts\Activate.ps1`。自动化测试不调用真实模型、不读取生产配置、不消耗模型额度。

## 本地启动

1. 复制 `.env.example` 为 `.env`，按说明填入自己选择的供应商完整 Images API 地址、模型和 key。
2. 用 `python -c "import secrets; print(secrets.token_urlsafe(36))"` 生成新的访问口令，将它填入 `JT_ACCESS_TOKEN`。不要把口令或 key 发到聊天、提交到 Git、写进截图。
3. 在当前 shell 加载环境变量，再启动。应用不会自动解析 `.env`，避免误把配置文件当脚本读取；下面方式只适用于你自己维护且可信的配置文件。

```bash
set -a
. ./.env
set +a
python -m jt_community.app
```

4. 打开程序打印的本地地址（默认 `http://127.0.0.1:8189`），输入访问口令。API 配置只在服务器环境变量中提供。

也可以直接设置环境变量，不创建 `.env`。Windows PowerShell 使用 `$env:JT_ACCESS_TOKEN=...` 等赋值，不要执行上述 POSIX shell 命令。

首次真实生成会向你配置的供应商发送提示词并可能收费；模型可用性、参数支持和计费以该供应商为准。审核阶段没有执行真实付费生成，不能把测试通过当作你的供应商已经联通。

## Docker（部署配置已提供，镜像构建尚未在审核环境验证）

配置 `.env` 后：

```bash
docker compose up --build -d
docker compose logs --tail=20
```

默认只映射到宿主机 `127.0.0.1:8189`。应用容器内监听 `0.0.0.0`，必须显式设置 `.env` 的 `JT_PUBLIC_URL=http://127.0.0.1:8189`。数据放在持久 Docker volume 中，不进入镜像。

如果远程访问，先按 [部署与备份](docs/DEPLOYMENT.md) 配置 HTTPS 反向代理，并将 `JT_PUBLIC_URL` 改为唯一真实 HTTPS origin。不要直接开放 HTTP 端口或为了“方便”关闭认证。

## 任务未明怎么办

如果断网、超时或服务重启，界面会保留任务为“结果待确认”。请先去自己供应商的控制台查记录：可能已生成、可能已收费。点击“我已核对”只解除新任务阻塞，**不会重发旧任务，也不会保证退款**。重复点击、刷新以及恢复提交都会复用旧请求 ID。

成功结果会保存在本地。供应商已返回但下载 / 验证失败时，本版本不会再次生成，也没有自动回捞供应商结果的功能；请手动从供应商控制台取回。

## 审核与发布边界

- [来源及范围](docs/SOURCE_AND_SCOPE.md)
- [部署与备份](docs/DEPLOYMENT.md)
- [安全说明](SECURITY.md)
- [许可证说明](LICENSE-DECISION.md)、[完整 AGPLv3](LICENSE)
- [依赖及第三方许可清单](THIRD-PARTY.md)

正式公开前仍需完成目标供应商真实端到端及浏览器、部署环境验收，并确认公开仓库目的地。请只发布审核后的这个干净目录，不要复制原仓库的 `.git`、审计资料、生产配置或客户数据。

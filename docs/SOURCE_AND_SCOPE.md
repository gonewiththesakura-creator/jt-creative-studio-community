# 来源、分离方式与待确认范围

源项目：https://github.com/gonewiththesakura-creator/jt-creative-studio

本次读取的默认分支：feat/four-realism-workflows-cold-style。

固定快照：294bf689662fec2583a439bcf9f5a841cb8eff07（2026-09-19 的提交；2026-10-22 只读核验）。此快照不代表你在其他位置开发的用户隔离与订阅代码。

## 保留的源代码能力

jt_community/provider.py 改编自原 server.py：

- compact_dreamapi_prompts / build_dreamapi_input：提示词去重、预算及画布要求。
- _dreamapi_request_json：有限响应、总时间边界。
- _dreamapi_download_image：只下载公共 HTTPS 图片，DNS 解析后固定连接地址，不发送供应商认证头。
- dreamapi_run_image：Images API 请求、Base64 / URL 结果、图片校验、cover / contain 归一化与 PNG 归档。

独立 app.py、界面、SQLite 账本和部署文件为此次分离编写；没有直接复制原生成式大页面、生产 server.py、style-configs 素材包或收费工作流。图片能力仍是 JT 原创作链路，不是假装兼容整个产品。

## 为何不直接复制原后端

该公开快照的 _auth() 直接放行，同时启动逻辑与生产 ComfyUI / RunningHub、工作流和运维恢复耦合。没有访问线上服务，不能据此判断当前线上部署。社区候选版用更小的真实认证边界，避免把快照中的部署假设带给自部署用户。

## 刻意排除

- 原 .git 历史、生产配置、运维脚本、隧道、服务器和部署元信息。
- audit/ 中测试生成图、供应商返回结果、部署记录。
- 私有工作流、上传媒体、客户任务及备份。
- 账户隔离、计费、订阅、后台和正在其他位置开发的商业代码。
- 原模型、LoRA、示例媒体和提示词素材包，直至所有者确认发布权利。

只发布干净目录不能撤回原公开仓库已公开的内容。源仓库历史清理、凭据轮换、隐藏或迁移都是另外的决策，本次没有执行。

## 公开状态与剩余验收

1. 所有者已批准本社区目录采用 AGPL-3.0-only；源码已公开至 [jt-creative-studio-community](https://github.com/gonewiththesakura-creator/jt-creative-studio-community)，原商业版不因此改许可。
2. 用自己选择的供应商做一次有意识的真实生成，确认参数、模型、费用及结果；本次仅验证离线 / 本地模拟。
3. 在目标主机验证 HTTPS、Docker（若使用）、目录权限、备份恢复和重启。
4. 2026-10-23 已在独立的新公开仓库中发布干净社区目录；未迁移原 Git 历史，也未修改原仓库、其可见性或生产服务。

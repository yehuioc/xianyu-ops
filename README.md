---
project_id: xianyu-ops
git_mode: independent
producer: mixed
producer_role: mixed
producer_evidence: 原自有后台实现；用户2026-10-02要求在原项目统一维护并更新GitHub
review_owner: codex-controller
review_state: reviewed
canonical_status: formal
---

# 闲鱼小后台

围绕自己的商品，在本机完成素材准备、线上内容回读、公开指标观察、发布预览、订单核对及付款后交付。数字成品按真实订单交付，定制服务仍需人工制作与验收。它不把挂牌数当销量，也不把公开浏览当独立访客。

唯一维护仓库是本项目目录；本机使用和 [GitHub](https://github.com/yehuioc/xianyu-ops) 使用同一份代码。账号、订单、运营记录、商品交付文件及第三方参考目录留在本机，由 `.gitignore` 排除。没有另一个长期维护的源码导出目录。原工作区的私有历史只在本机归档，不上传。

## 安装与首次使用

使用 Python 3.12。在项目目录建立虚拟环境、安装依赖：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -X utf8 -B scripts/xianyu-console.py start
```

浏览器打开 `http://127.0.0.1:8090`。`start` 管理本项目后台，并启动本项目的 Edge 浏览器资料目录；Windows 需要安装 Edge。手动前台启动使用 `serve`，后台状态及停止使用 `status`、`stop`。停止后台不会关闭 Edge 或删除登录资料。已有同端口实例会先核对身份，不停止未知服务；不要同时运行原第三方后台。

首次启动会建立未连接的本机账号，商品保持空状态。先在该 Edge 中自行登录闲鱼，再到“账号与任务”点击“连接现有登录”，核对成功后刷新在售商品。账号身份不匹配、平台要求验证或发送结果不确定时停止对应动作，不反复重试。首次使用不会默认开启自动回复或付款后交付。

可直接使用默认的 `local-account` 标识，也可在第一次启动前创建本机配置：

```powershell
New-Item -ItemType Directory -Force data\console | Out-Null
Copy-Item console\local-settings.example.json data\console\local-settings.json
```

只修改自己的 `data/console/local-settings.json`。`account` 是本机账号名称；`focus_item` 是默认商品 ID；`managed_items` 是纳管商品列表；`cdp_url` 是本机浏览器调试入口；`browser_profile` 是项目内的浏览器资料目录。`item_profiles` 只用于已有商品的特定服务/阅读资料适配，不是新用户必须填写的配置。配置修改后重启后台；已有运营用户应保留原账号名称和浏览器资料路径。

密码、Cookie、API 密钥和验证码不要放进公开源码或示例配置。登录凭据由本机数据库加密保存。另一个已登录的本机浏览器可通过相应 CDP 配置连接，账号仍需实际核对。

## 商品、观察与交付

- “我的商品”读取自己的在售商品，逐项选择持续观察；定期采集尊重暂停开关。
- “手机素材包”可编辑草稿并生成文案/主图包，手机上传后通过回读确认生效，从真实确认时点开始本轮观察。
- “商品工坊”读取本机商品目录 `products/monetization/catalog.json`。新安装没有商品素材，页面显示空集合；用 `console/catalog.example.json` 作为空目录格式起点，按自己的真实材料填写 `products`。不提供原账号的售卖成品、网盘链接或客户资料。
- 上架与修改先读取预览，人工确认后单次提交；响应不确定先回读，不盲目重发。
- 数字成品打包后可通过夸克适配器上传、下载核对摘要并绑定具体商品；定制服务只自动收集已配置的需求，样例不等于客户成品。
- “交付与回复”逐商品、逐规格核对规则后启用。买家说“已付款”不能触发交付，必须核对平台订单、账号、买家、数量与规格。持久状态用于去重与回查，不能由隔离测试推定真实付款交付已经发生。

夸克使用者须另行安装官方工具并完成自己的授权；固定来源见 `sources/quark-cli.json`。不运行第三方后台或复制其源码。现有入口：

```powershell
python -B scripts/xianyu-commerce.py list
python -B scripts/xianyu-quark.py status
python -B scripts/xianyu-delivery-workflow.py --help
python -B scripts/xianyu-console.py collect --scheduled
```

没有默认安装定时任务。需要按本机运行环境配置调度；电脑、后台和浏览器须可用。平台内部协议可能变化，不承诺长期稳定。

## 本机数据与维护

`data/console/console.sqlite3` 和 `data/console/.account.key` 必须一起备份，丢失密钥不能用新密钥代替。本机配置、浏览器资料、订单、回复/交付规则、素材包、网盘授权与操作回执都留在本项目 `data/` 或既有私有目录中。`products/`、`vendor/`、`upstream/`、内部 `docs/` 及旧工程记录也保留本机，不进入公开 Git 历史。

“需求来源”优先读取本机 `data/console/requirements.json`；公开默认文件为空，不上传原始私人对话。原运营说明和 Git/工作树基线保存在本机 `data/repository-consolidation/20261002/`，用于核对与恢复，不作为第二份活跃项目维护。

只在这个目录修改代码、提交并上传。当前分支跟踪远端 `main`；使用普通 `git push` 更新。只推送公开分支，不将私有归档引用或全部本地引用推送到 GitHub。`.gitignore` 不会删除本机文件。

## 验证与边界

```powershell
python -X utf8 -B -m unittest discover -s tests -p 'test_owned_*.py' -q
python -X utf8 -B -m unittest discover -s tests -p 'test_delivery_workflow.py' -q
python -X utf8 -B -m unittest discover -s tests -p 'test_xianyu_ops_core.py' -q
```

测试使用临时合成商品和隔离数据库，不需要真实售卖素材，不对真实客户发送测试消息。公开浏览、想要及订单的来源、时间和缺失值分别记录；未知不能补零，累计历史不能冒充本轮转化。完整 SKU、真实交付、多账号与多电脑并发仍须各自验证。

后台只监听本机，限制 Host/Origin 和资源类型。未经安全改造不要开放公网。当前未选择开源许可证；第三方工具和依赖按各自许可使用。

# Launch Studio · 跨境商品上新工作台

跨境运营团队录入商品、导入评论、生成上架文案、送审并导出。每一项任务都保留生成时使用的商品版本、评论和目标站点。编辑员与审核员分别操作，主管可以按时间查看谁修改了资料、谁审核了文案以及何时导出。

适用于由管理员开设账号的小团队自托管。当前支持 Amazon 美国站英语、德国站德语的 Listing 草稿。导出文件交给运营人员使用，平台接入和上架操作放在独立集成阶段。

## 在本机体验

安装 Python 3.11–3.13 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。在项目目录运行：

~~~powershell
uv sync --locked
uv run uvicorn studio.main:app --host 127.0.0.1 --port 8000
~~~

打开 [本机工作台](http://127.0.0.1:8000)。选择「三层桌面收纳架」，生成样例后检查「承重待补充」，修改标题、确认并导出。内置样例可以免密钥体验，新录入商品会整理成待填写草稿。页面会标明生成方式。

在本机体验多账号时，把 .env.example 复制为 .env，开启 AUTH_ENABLED，填写首次管理员邮箱和至少 12 字符的密码。使用独立 SQLite 文件开始测试，登录后可以在「成员管理」添加审核员，再导入商品。正式团队空间从空白开始。

## 团队如何使用

1. **建档。** 编辑员录入商品规格、特点和关键词。商品更新时生成修订记录，旧窗口保存前会检查修订编号。评论 CSV 用 UTF‑8，包含 id、rating、text、source 四列，评分 1–5。一个编号对应同一条评论，可再次导入更新。每次任务选取最多 40 条评论，页面显示实际样本和总数。
2. **生成。** 后台读取任务保存的原始资料，汇总带评论编号和原文摘录的发现，生成标题、五点描述、商品描述和搜索词。页面显示处理步骤，刷新后能从保存的进度继续查看。
3. **复核。** 程序检查必填字段、内部长度规则、风险词和带单位的数字。对比资料时能看到规格来源。规则提示仅覆盖项目定义的范围，运营和审核员负责确认品牌、平台要求和实际商品参数。
4. **送审。** 提交最新版本后，由其他有审核权限的成员决定是否通过。退回时填写原因，修改后另存新版本并重新送审。当前通过的版本可以下载 CSV 或 Markdown。
5. **管理。** 所有者可管理工作区成员、角色和操作记录。编辑员可以录入资料、生成和送审。审核员可以查看任务并处理他人提交的版本。查看者可以查阅并下载已通过的版本。

## 部署到服务器

准备好可解析到服务器的域名、Docker Engine 和 Compose。开放服务器的 80、443 端口，Compose 中的数据库和 API 通过内部网络通信。[Compose 启动与健康检查](https://docs.docker.com/compose/how-tos/startup-order/) · [Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)

复制 .env.production.example 为 .env，填写以下配置：

| 配置 | 内容 |
| --- | --- |
| STUDIO_DOMAIN | 访问工作台的域名 |
| PUBLIC_ORIGIN | 该域名的 HTTPS 地址，例如 https://studio.example.com |
| ALLOWED_HOSTS | 该域名和供容器健康检查使用的 127.0.0.1 |
| POSTGRES_PASSWORD | 数据库管理账号密码 |
| STUDIO_DATABASE_PASSWORD | 应用数据库账号密码，使用不同的长随机密码 |
| DATABASE_URL | postgresql://studio:应用密码@postgres:5432/studio，密码中的 URL 特殊字符需要编码 |
| BOOTSTRAP_EMAIL、BOOTSTRAP_PASSWORD | 首次管理员账号和密码。创建成功后可从部署配置中移除，再重建 API 容器 |
| LLM_BASE_URL、LLM_API_KEY、LLM_MODEL | 模型的 HTTPS 兼容地址、密钥和模型名。地址填写到 /v1，程序会追加 /chat/completions |
| WORKER_CONCURRENCY | 单个 Worker 容器的处理并发，默认 2 |

~~~sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 api worker
~~~

用管理员账号进入站点，添加一个独立的审核员。先使用非敏感样例数据提交任务，确认两次模型调用、审核和下载都正常，再导入真实资料。模型请求会传送商品资料、选中的评论和任务说明给所配置的服务商。运行记录保留耗时、用量和错误类型。

## 数据和运行

~~~mermaid
flowchart LR
    H[用户浏览器] --> C[Caddy HTTPS]
    C --> A[FastAPI 登录与业务接口]
    A --> P[(PostgreSQL)]
    W[独立 Worker] --> P
    W --> G[LangGraph 商品上新流程]
    G --> M[模型服务]
    A --> S[SSE 进度与审核结果]
~~~

- PostgreSQL 保存账户、商品版本、任务资料、进度、草稿和操作记录。SQLite 用于本机使用。已有初版 SQLite 文件会原地迁移，原资料和草稿留在演示工作区。迁移前复制数据库文件，容器生产部署使用独立 PostgreSQL 数据库。
- 每个任务同一时刻由一位 Worker 持有租约。Worker 停止续约后，另一位可以重新运行任务。已经过期的 Worker 无法提交结果。恢复可能重复调用外部模型，因此需要留意模型用量。连续三次中断后任务进入失败状态，可按原资料重试。
- 页面「运行状态」显示队列、审核和 Worker 最近心跳。容器内可用 /health/live 和 /health/ready 检查 API 与数据库。生产配置校验数据库、账号、站点和模型，进程启动时会拒绝缺失的关键配置。
- 所有者负责初始账号发放。邮箱是内部登录标识，面向不互信团队开放注册前，需要接入经验证的邀请、身份服务及找回密码流程。
- 使用 PostgreSQL 备份并定期验证恢复。升级前备份，在独立测试数据库试跑迁移和审核。备份导出示例使用 PostgreSQL 官方工具。[备份与恢复](https://www.postgresql.org/docs/current/backup-dump.html)

~~~sh
docker compose exec -T postgres pg_dump -U postgres -Fc studio > studio-backup.dump
~~~

## 验证

~~~powershell
uv run --locked ruff check studio tests
uv run --locked pytest -q
~~~

设置 TEST_DATABASE_URL 可额外运行 PostgreSQL 集成检查。测试在独立临时 schema 执行。另有单独的权限、模型格式、引用校验、任务重复提交、过期接手、审核及导出测试。GitHub Actions 会运行这些检查并构建应用镜像。

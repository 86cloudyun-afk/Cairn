# 执行计划、任务产物与知识摘要

新服务器启动时自动检查 SQLite 列并补齐新表和字段，可重复启动。旧项目的 `plan` 初始为 `{}`，旧动作的 `depends_on=[]`、`priority=0`，旧事实 `kind=other`，摘要由原文规范为空格后截取前 120 字符；原始 description 保留。

如需手工迁移，停止服务并备份数据库后，对**尚未升级的原始 schema**运行一次：

```sh
sqlite3 /path/to/cairn.db '.backup /path/to/cairn-before-upgrade.db'
sqlite3 /path/to/cairn.db < cairn/migrations/001_plans_artifacts.sql
```

自动升级与手工 SQL 二选一；手工 SQL 的 `ALTER TABLE` 不支持重复执行。旧版 bootstrap_mode 数据库由服务器自动兼容转换。

## 计划与依赖

创建项目的 goal 每个非空行对应一个阶段，例如：

```json
{"title":"表单流程","origin":"测试环境已就绪","goal":"注册测试账号\n用新账号登录\n提交表单并验证后续流程","bootstrap_enabled":false}
```

`current_stage` 从 0 开始，等于 `stages` 长度表示全部完成。reason 根据已确认的事实推进阶段，必须先保存 plan 再创建动作；多阶段项目无法提前调用 complete。bootstrap 仅收集当前阶段成果，交由 reason 继续规划。旧的无 plan 返回协议继续可用。

```json
{"accepted":true,"data":{"plan":{"current_stage":1,"stages":["注册测试账号","用新账号登录","提交表单并验证后续流程"],"next_steps":["用已有测试账号登录"],"blockers":[]},"intents":[{"from":["f001"],"description":"用已有测试账号登录","depends_on":["i001"],"priority":0,"session_key":"registration-flow"}]}}
```

每条 description 必须与一个 next_steps 完全一致。depends_on 只能引用已经存在的同项目 intent ID，不能猜测本批新动作的 ID；这些动作全部收尾后才可领取或派发。数值较小的 priority 先执行，同优先级按创建时间、ID 排序。没有可执行动作时可记录明确 blockers；新事实或 hint 到达后重新评估。

`PUT /projects/{id}/plan` 请求为 `{"worker":"当前reason持有者","plan":{...}}`，需要有效 reason 租约。已有阶段不可改序，阶段不能倒退；重开已完成项目时会重置阶段游标，重新评估已有证据。

## 产物与结构化收尾

explore 的执行、兜底收尾均接受旧 description，或下列扩展：

```json
{"accepted":true,"data":{"fact":{"description":"注册成功，已验证账号可登录。","kind":"credential","summary":"测试账号已验证","url":"https://test.example/login","confidence":95},"artifacts":[{"kind":"account","name":"测试账号","value":{"username":"tester","password":"example"}},{"kind":"file","name":"登录脚本","file_path":"/workspace/login.py"},{"kind":"token","name":"访问令牌","value":"example-token","expires_at":"2030-01-01T00:00:00Z"}]}}
```

fact 可选字段：kind（finding/credential/endpoint/negative/other）、summary、ip、port、url、confidence（0–100）。description 保留全文。bootstrap 的 fact 对象也接受这些字段。

产物种类为 account/session/token/file/endpoint；name 必填，value 可以是 JSON，file 必须带 file_path，expires_at 必须有时区。dispatcher 先校验完整输出，再逐条 `POST /projects/{id}/artifacts` 保存产物，最后通过收尾请求的 `artifact_ids` 将产物关联 fact_id；事实创建、动作收尾和关联在同一 SQLite 事务提交。也可直接调用产物接口写入单条数据。`GET /projects/{id}/artifacts` 和项目详情只返回未过期条目，`?include_expired=true` 可查历史。删除项目会级联删除。

任务开始先读取注入的 `{artifacts}` 清单并优先复用。文件仍需保存在项目容器的工作目录中；数据库记录文件路径，不复制文件内容。Codex、Claude Code、Pi 会在成功收尾时自动保存 worker 会话，后续任务使用相同 `session_key`、worker 和后端类型即可恢复；省略 session_key 时只匹配完全相同的任务描述。手工 session 产物的 value 格式为：

```json
{"session_id":"后端会话ID","topic":"registration-flow","worker":"worker名称","backend":"pi"}
```

worker CLI 的会话文件也必须仍在原容器/本地工作目录内。保存标识不会迁移后端会话文件。恢复同一个会话后自动保存的记录会沿用原记录的到期时间；新会话不会继承其他会话的到期时间。mock 不恢复会话。

artifact POST 与 fact 收尾是独立请求；产物写入失败时，任务保持未收尾并释放领取权，调度器可以重试。先前已保存的产物即使尚未关联 fact，也持久保存在清单中供复用。YAML 导出保留事实全文、结构化字段、计划与依赖，但不导出 artifact 凭据；项目 API 和任务清单仍可读取产物值。

## 摘要与验证

reason 默认只注入 `id|kind|summary`。同一完整 `(ip,port)` 的 negative 条目超过 3 条时，只显示最后插入的一条和隐藏计数；旧条目的全文仍在容器中的 graph.yaml 按需读取。

```sh
cd cairn
.venv/bin/python -m pytest -q
```

包含旧 schema 自动/SQL 迁移、三阶段推进与依赖调度、过期过滤、级联删除、模板渲染、两条收尾路径与会话恢复测试。命令包装与本地 HTTP 测试见 [container/README.md](../container/README.md)，Linux Docker/nftables 安装和主机验证见 [deploy/network/README.md](../deploy/network/README.md)。

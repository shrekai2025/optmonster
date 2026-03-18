# OptMonster

Twitter 账号底座 v1/2，现阶段覆盖：

- 账号配置与运行态管理
- 环境与账号级隔离
- 只读推特数据采集
- 预算血条、推文工作台、AI 回复审批
- 页面回写账号 YAML 配置

## Local Development

1. 准备 Python 3.13+ 或 Docker。
2. 复制开发环境文件：
   - `cp config/environments/dev.env.example config/environments/dev.env`
3. 显式指定运行环境文件（必须）：
   - `export APP_ENV_FILE=config/environments/dev.env`
4. 安装依赖：
   - `python3 -m pip install -e '.[dev]'`
5. 启动基础设施：
   - `docker compose --env-file config/environments/dev.env up postgres redis -d`
6. 执行迁移并启动 API：
   - `alembic upgrade head`
   - `uvicorn app.main:app --reload`
7. 启动 worker（新终端，同样需要 `APP_ENV_FILE`）：
   - `python -m app.worker`
8. 打开控制台：
   - `http://127.0.0.1:8000/console`

## Docker Compose

- 开发环境：
  - `docker compose --env-file config/environments/dev.env up --build`
- 生产环境：
  - `cp config/environments/prod.env.example config/environments/prod.env`
  - 编辑 `config/environments/prod.env`，至少替换：
    - `POSTGRES_PASSWORD`
    - `DATABASE_URL`
    - `LLM_API_KEY`（如使用）
    - `LLM_MODEL_ID`（如使用）
  - `docker compose --env-file config/environments/prod.env --project-name optmonster-prod up --build -d`

`docker compose` 会先运行一次 `migrate` 服务，再启动 `api` 和 `worker`，避免首次空库启动时的并发迁移冲突。

## Config Layout

- 账号配置：`config/accounts/*.yaml`
- 账号分组：`config/groups/*.yaml`
- Cookie 文件：`config/cookies/*.json`
- 写作指南：`config/writing_guides/*.md`

应用启动时会自动加载账号配置，并把运行态同步到数据库。

## 首次可用最短路径

1. 把待导入的 cookie 文件（Netscape 或 JSON）放到项目根目录。
2. 启动系统：
   - `docker compose --env-file config/environments/dev.env up --build`
3. 打开控制台：
   - `http://127.0.0.1:8000/console`
4. 在控制台导入 cookie 生成账号。
5. 校验会话：
   - `POST /admin/accounts/{account_id}/validate-session`
6. 手动触发抓取：
   - `POST /admin/accounts/{account_id}/fetch-now`
7. 在推文工作台查看数据与后续动作：
   - `http://127.0.0.1:8000/console/tweets`

## Global LLM Settings

环境文件支持第三方 LLM Provider 的全局配置：

- `APP_TIMEZONE`
- `FETCH_RECENT_WINDOW_HOURS`
- `FETCH_LATEST_FIRST`
- `FETCH_INCLUDE_REPLIES`
- `FETCH_INCLUDE_RETWEETS`
- `POPULAR_TWEET_MIN_VIEWS`
- `POPULAR_TWEET_MIN_LIKES`
- `POPULAR_TWEET_MIN_RETWEETS`
- `POPULAR_TWEET_MIN_REPLIES`
- `LLM_PROVIDER`
  - `mock`
  - `openai_compatible`
  - `anthropic`
- `LLM_BASE_URL`
- `LLM_API_KEY`
- `LLM_MODEL_ID`

其中 `openai_compatible` 适合第三方 `base_url + key + model id` 兼容接口。

## Current Usage Flow

1. 查看已加载账号：
   - `curl http://127.0.0.1:8000/admin/accounts`
2. 手动校验某个账号 Cookie：
   - `curl -X POST http://127.0.0.1:8000/admin/accounts/<account_id>/validate-session`
3. 手动触发一次抓取：
   - `curl -X POST http://127.0.0.1:8000/admin/accounts/<account_id>/fetch-now`
4. 修改 YAML 后热重载：
   - `curl -X POST http://127.0.0.1:8000/admin/config/reload`
5. 在推文工作台里生成 AI 决策：
   - `POST /admin/tweets/{tweet_record_id}/reply/generate`
6. 审批或修改回复：
   - `POST /admin/actions/{action_id}/approve`
   - `POST /admin/actions/{action_id}/modify`
   - `POST /admin/actions/{action_id}/skip`

## API Endpoints

- `GET /healthz`：检查 API / DB / Redis 是否可用
- `GET /admin/accounts`：查看账号配置和运行态
- `GET /admin/cookie-import/candidates`：列出可导入的根目录 cookie 文件
- `POST /admin/cookie-import/accounts`：从 cookie 文件导入新账号
- `GET /admin/accounts/{account_id}/config`：读取可编辑账号配置
- `PUT /admin/accounts/{account_id}/config`：保存账号配置并自动 reload
- `GET /admin/dashboard`：控制台看板数据
- `GET /admin/tweets`：最近抓到的推文
- `GET /admin/tweets/{tweet_record_id}`：推文详情和关联动作
- `POST /admin/tweets/{tweet_record_id}/reply/generate`：生成 AI 点赞/回复决策
- `POST /admin/config/reload`：重新扫描并同步账号配置
- `POST /admin/accounts/{account_id}/validate-session`：校验 Cookie 是否可用
- `POST /admin/accounts/{account_id}/fetch-now`：把该账号加入抓取队列
- `GET /admin/approvals`：查看待审批回复
- `GET /admin/actions`：查看写侧动作历史
- `POST /admin/actions/replies`：创建回复审批单
- `POST /admin/actions/likes`：创建点赞动作
- `POST /admin/actions/{action_id}/approve`：批准动作
- `POST /admin/actions/{action_id}/modify`：提交人工修改版并批准
- `POST /admin/actions/{action_id}/skip`：跳过动作

## 上传 GitHub 前检查清单

1. 确认工作区状态：
   - `git status`
2. 确认敏感文件会被忽略：
   - `git check-ignore -v .env config/environments/dev.env config/environments/prod.env`
   - `git check-ignore -v config/cookies/*.json config/accounts/*.yaml optmonster.db`
3. 确认未被跟踪：
   - `git ls-files | rg "optmonster.db|config/cookies/|config/accounts/|config/environments/prod.env"`
4. 提交前执行本地扫描：
   - `python3 -m pip install pre-commit`
   - `pre-commit install`
   - `pre-commit run --all-files`

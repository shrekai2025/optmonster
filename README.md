# OptMonster

Twitter 账号底座 v1/2，现阶段覆盖：

- 账号配置与运行态管理
- 环境与账号级隔离
- 只读推特数据采集
- 预算血条、推文工作台、AI 回复审批
- 页面回写账号 YAML 配置

## Local Development

1. 准备 Python 3.13+ 或直接使用 Docker。
2. 复制环境文件：
   - `cp config/environments/dev.env.example config/environments/dev.env`
3. 安装依赖：
   - `python3 -m pip install -e '.[dev]'`
4. 启动基础设施：
   - `docker compose --env-file config/environments/dev.env up postgres redis -d`
5. 执行迁移并启动 API：
   - `alembic upgrade head`
   - `uvicorn app.main:app --reload`
6. 启动 worker：
   - `python -m app.worker`

## Docker Compose

- 开发环境：
  - `docker compose --env-file config/environments/dev.env up --build`
- 生产环境：
  - `docker compose --env-file config/environments/prod.env.example --project-name optmonster-prod up --build`

`docker compose` 现在会先运行一次 `migrate` 服务，再启动 `api` 和 `worker`，避免首次空库启动时的并发迁移冲突。

## Config Layout

- 账号配置：`config/accounts/*.yaml`
- Cookie 文件：`config/cookies/*.json`
- 写作指南：`config/writing_guides/*.md`

应用启动时会自动加载账号配置，并把运行态同步到数据库。

### Global LLM Settings

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

当前推荐通过轻量控制台 + YAML 配置使用：

1. 准备账号配置
   - 编辑 `config/accounts/*.yaml`
   - 把账号 Cookie JSON 放到 `config/cookies/*.json`
2. 启动系统
   - `docker compose --env-file config/environments/dev.env up --build`
3. 打开控制台
   - `http://127.0.0.1:8000/console`
   - 推文工作台：`http://127.0.0.1:8000/console/tweets`
4. 查看已加载账号
   - `curl http://127.0.0.1:8000/admin/accounts`
5. 手动校验某个账号 Cookie
   - `curl -X POST http://127.0.0.1:8000/admin/accounts/<account_id>/validate-session`
6. 手动触发一次抓取
   - `curl -X POST http://127.0.0.1:8000/admin/accounts/<account_id>/fetch-now`
7. 修改 YAML 后热重载
   - `curl -X POST http://127.0.0.1:8000/admin/config/reload`
8. 在推文工作台里生成 AI 决策
   - `POST /admin/tweets/{tweet_record_id}/reply/generate`
9. 审批或修改回复
   - `POST /admin/actions/{action_id}/approve`
   - `POST /admin/actions/{action_id}/modify`
   - `POST /admin/actions/{action_id}/skip`
10. 页面里直接编辑账号配置并保存
   - `GET /admin/accounts/{account_id}/config`
   - `PUT /admin/accounts/{account_id}/config`
   - 可启用 `targets.timeline (Following)`、`targets.timeline_recommended (For You)`、`targets.timeline_popular (Hot / Viral)`
11. 页面里从项目根目录导入 cookie 文件生成新账号
   - 控制台会扫描根目录里的 Netscape / JSON cookie 文件
   - 自动提取 `x.com / twitter.com` 登录 cookie，写入 `config/cookies/*.json`
   - 自动生成 `config/accounts/*.yaml` 并立即 reload + validate session

### API Endpoints

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

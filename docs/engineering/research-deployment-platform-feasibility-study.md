# Research Deployment Platform Feasibility Study

## 1. 概述
本项目旨再部署一套“研究部署平台”的可行性，用于将研究成果快速、安全、可控地交付到测试与生产环境。
## 2. 目标与范围
- 目标：
  - 缩短研究成果从原型到可部署版本的周期
  - 确保部署过程可追溯、可审计、可回滚
  - 提升跨团队协作与资源利用效率
- 范围：
  - 研究成果（模型、算法、实验配置、数据管道）
  - 部署目标（测试环境、预发布环境、生产环境）
  - 支撑系统（CI/CD、模型注册、监控与告警）

## 3. 现状与痛点
- 研究与工程流程割裂，缺乏统一交付通道
- 实验环境不一致导致部署失败率高
- 合规与安全要求在后期才介入，增加返工成本
- 缺少统一的版本管理与可复现机制

## 4. 关键需求
- 标准化交付：统一打包、签名、发布
- 可复现：环境与依赖完整记录
- 合规：权限、审计、数据访问控制
- 可观测：性能指标、日志、追踪
- 自动化：从验证到部署全流程自动化

## 5. 方案比较
### 5.1 方案 A：基于现有 CI/CD 平台改造
- 优点：成本低，落地快
- 缺点：需大量定制，对研究资产支持不足

### 5.2 方案 B：引入 MLOps 平台（如 MLflow/Kubeflow）
- 优点：对研究资产友好，支持实验追踪与模型注册
- 缺点：运维复杂，学习成本高

### 5.3 方案 C：自研轻量化部署平台
- 优点：可高度定制，贴合研究流程
- 缺点：研发周期长，维护成本高

## 6. 推荐方案
建议采用“方案 A + 方案 B 的渐进式组合”：
- 短期：基于现有 CI/CD 管道搭建基础交付能力
- 中期：集成轻量级 MLOps 组件（模型注册、实验追踪）
- 长期：按需自研关键模块

## 7. 技术架构（目标状态）
- 研究资产层：代码仓库、数据版本、实验配置
- 管道层：CI/CD、自动测试、模型验证
- 部署层：容器化部署、灰度发布、回滚机制
- 观测层：监控、日志、告警、审计

## 8. 成本评估
- 人力成本：2-3 人团队，3-6 个月
- 基础设施成本：K8s 资源、存储、日志服务
- 维护成本：持续运维与版本升级

## 9. 风险与缓解
- 风险：研究资产标准化难度高
  - 缓解：制定统一模板与规范，培训研究团队
- 风险：合规要求影响效率
  - 缓解：在流程早期嵌入合规检查
- 风险：工具链复杂导致学习成本高
  - 缓解：逐步引入，提供文档与示例

## 10. 实施里程碑
- 第 1 个月：需求梳理与 PoC
- 第 2-3 个月：基础交付流程落地
- 第 4-6 个月：引入模型注册与实验追踪

## 11. 成功指标
- 部署周期缩短 30% 以上
- 部署失败率降低 50% 以上
- 研究成果可复现率达到 90% 以上

## 12. 结论
研究部署平台具备可行性，建议采用渐进式方案快速落地基础能力，同时为后续扩展保留空间。该平台将显著提升研究成果转化效率与部署稳定性。

## 13. 附录：Agent Platform (Minimal FastAPI + Celery)

This is a minimal runnable skeleton that wires FastAPI, Celery, Redis Pub/Sub (with replay),
and docker-py around a simple deployment flow.

### Requirements

- Docker Engine running on the host
- Redis server
- Python 3.10+

### Setup

```bash
cd services/agent_platform
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run

Start Redis (example):

```bash
docker run --rm -p 6379:6379 redis:7
```

Start the API:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Start the worker:

```bash
celery -A app.worker.celery_app worker --loglevel=info
```

### API

- `POST /cases` — enqueue a build/deploy
- `GET /cases/{case_id}` — read status and access info
- `WS /cases/{case_id}/logs` — stream build logs with replay

### Environment

All components default to `redis://localhost:6379/0`. Override with `REDIS_URL`.

#### Port mode

- `PORT_MODE=pool` (default): use a fixed port pool (`PORT_POOL_START`-`PORT_POOL_END`)
- `PORT_MODE=dynamic`: use Docker's dynamic port mapping (development only)

### Minimal smoke test

1. Start Redis, API, and worker.
2. `POST /cases` with a repo that includes a Dockerfile.
3. Connect to `WS /cases/{case_id}/logs` (e.g. with `wscat`).
4. `GET /cases/{case_id}` until status is `RUNNING`.
5. Visit the returned `access_url`.

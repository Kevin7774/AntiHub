# 维护

## 容器清理
仅清理 AntiHub 管理的容器（带 label）：
```bash
docker ps -a --filter label=antihub.managed=true
```
停止并清理：
```bash
docker rm -f $(docker ps -a -q --filter label=antihub.managed=true)
```

## 端口池与冲突
- 端口池默认范围：`PORT_POOL_START`～`PORT_POOL_END`
- 端口冲突：释放旧容器或扩大范围

## 日志与存储
- Redis 中日志保留：受 `LOG_RETENTION_LINES` 影响
- 手动清理 Redis：谨慎执行 `redis-cli FLUSHDB`

## 归档与清理策略
- 支持 `POST /cases/{id}/archive`
- 建议定期归档或清理失败/结束的 case

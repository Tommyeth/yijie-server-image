# yijie-server-image

一解围棋的 GPU 分析节点镜像。它是 `katago-image` 的单用途精简版：

- 只包含 KataGo 1.17.2 TensorRT 10.9/CUDA 12.8 后端。
- 只预载 `b11c768h12nbt3tflrs-fson-silu.bin.gz`，不包含 humanSL 和其它权重。
- 单进程支持 1–10 个局面同时搜索；批量接口一次最多 10 个局面。
- HTTP 默认只监听 `127.0.0.1:2718`，预期由上海控制端通过 SSH 本地端口转发访问。
- 权重已在镜像内，启动时不下载。默认启动暖机，`/health` 可用时已能真正接受搜索。

## 构建与运行

```bash
docker build -t ghcr.io/tommyeth/yijie-server-image:latest .
docker run --rm --gpus all --name yijie-gpu \
  --network host \
  -v yijie-katago-cache:/workspace/katago/.home \
  ghcr.io/tommyeth/yijie-server-image:latest
```

`--network host` 下进程仍只绑定回环地址。如果容器网络模式不是 host，请不要直接暴露 2718 到公网；可根据平台网络方式设置 `YIJIE_LISTEN_HOST=0.0.0.0`，并仅允许 SSH 节点访问。

TensorRT plan 与 GPU 架构/驱动相关，无法在通用 GHCR 镜像中安全地预编译成适配所有显卡的文件。持久化 `/workspace/katago/.home` 后，同一机型后续启动可复用调优缓存；第一次仍可能需要数十秒到数分钟。

## HTTP API

### 单局面

```bash
curl http://127.0.0.1:2718/v1/analyze \
  -H 'content-type: application/json' \
  -d '{"boardXSize":19,"boardYSize":19,"rules":"chinese","komi":7.5,"moves":[],"maxTime":5}'
```

`POST /analyze` 作为旧客户端兼容别名。

### 1–10 局面批量搜索

```json
{
  "positions": [
    {"boardXSize": 19, "boardYSize": 19, "rules": "chinese", "komi": 7.5, "moves": []},
    {"boardXSize": 19, "boardYSize": 19, "rules": "chinese", "komi": 7.5, "moves": [["B", "Q16"]]}
  ]
}
```

发送到 `POST /v1/analyze/batch`。每个局面的 `maxTime` 默认 5 秒，且会被 `YIJIE_MAX_SEARCH_SECONDS` 强制封顶。

## 环境变量

| 变量 | 默认值 | 用途 |
|---|---:|---|
| `YIJIE_MAX_CONCURRENT` | `10` | 同时搜索数，强制限制在 1–10 |
| `YIJIE_MAX_SEARCH_SECONDS` | `30` | 单查询 `maxTime` 封顶 |
| `YIJIE_DEFAULT_MAX_VISITS` | `1000` | 未指定 `maxVisits` 时的默认值；硬上限始终为 5000 |
| `YIJIE_QUERY_TIMEOUT` | `45` | 分析引擎响应超时 |
| `YIJIE_QUEUE_TIMEOUT` | `15` | 10 个槽位全满时的等待时间 |
| `YIJIE_LISTEN_HOST` | `127.0.0.1` | HTTP 监听地址 |
| `YIJIE_LISTEN_PORT` | `2718` | HTTP 监听端口 |

## GHCR

将该目录作为 `Tommyeth/yijie-server-image` 仓库根目录推送后，GitHub Actions 会发布：

- `ghcr.io/tommyeth/yijie-server-image:latest`
- `ghcr.io/tommyeth/yijie-server-image:<commit-sha>`

# 一解云端 GPU 节点：只保留 TensorRT 和 b11c768，不携带人类网络或其它后端。
# 不从 katago-image 继承：在下层删文件无法缩小已经生成的 Docker layer。
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

ARG KATAGO_VERSION=1.17.2
ARG TRT_PKG_VERSION=10.9.0.34-1+cuda12.8
ARG MAIN_NET_TAG=v1.17.1
ARG MAIN_NET=b11c768h12nbt3tflrs-fson-silu.bin.gz

LABEL org.opencontainers.image.source="https://github.com/Tommyeth/yijie-server-image" \
      org.opencontainers.image.description="Yijie b11c768-only KataGo TensorRT analysis node"

ENV DEBIAN_FRONTEND=noninteractive \
    KATAGO_HOME=/workspace/katago \
    HOME=/workspace/katago/.home \
    YIJIE_LISTEN_HOST=127.0.0.1 \
    YIJIE_LISTEN_PORT=2718 \
    YIJIE_MAX_CONCURRENT=10

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip python3 tini libzip4 libgomp1 libprotobuf23 \
    && rm -rf /var/lib/apt/lists/*

# KataGo TRT 加载网络时需要 TensorRT builder。Windows 跨平台 builder
# resource 约 1.8GB，Linux 运行不会使用，必须在同一 layer 内删除。
RUN apt-get update \
    && apt-get install -y --no-install-recommends --allow-downgrades \
        libnvinfer10=${TRT_PKG_VERSION} \
    && rm -f /usr/lib/x86_64-linux-gnu/libnvinfer_builder_resource_win.so.* \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends --allow-downgrades \
        libnvinfer-plugin10=${TRT_PKG_VERSION} \
        libnvonnxparsers10=${TRT_PKG_VERSION} \
    && rm -rf /var/lib/apt/lists/*

# 官方 Linux 包是 AppImage。镜像构建环境没有 FUSE，因此在构建期解包。
RUN set -eux; \
    curl -fL --retry 3 -o /tmp/katago.zip \
      "https://github.com/lightvector/KataGo/releases/download/v${KATAGO_VERSION}/katago-v${KATAGO_VERSION}-trt10.9.0-cuda12.8-linux-x64.zip"; \
    mkdir -p /tmp/kz /opt/katago/bin; \
    unzip -q /tmp/katago.zip -d /tmp/kz; \
    bin="$(find /tmp/kz -type f -name katago | head -1)"; \
    test -n "$bin"; chmod +x "$bin"; \
    mkdir -p /opt/katago/runtime; \
    if (cd /opt/katago/runtime && "$bin" --appimage-extract >/dev/null 2>&1) \
       && [ -x /opt/katago/runtime/squashfs-root/AppRun ]; then \
      ln -s /opt/katago/runtime/squashfs-root/AppRun /opt/katago/bin/katago; \
    else \
      install -m 0755 "$bin" /opt/katago/runtime/katago; \
      ln -s /opt/katago/runtime/katago /opt/katago/bin/katago; \
    fi; \
    /opt/katago/bin/katago version; \
    rm -rf /tmp/katago.zip /tmp/kz

# 只烤入 b11c768；容器启动时不下载权重。
RUN mkdir -p /opt/katago/models \
    && curl -fL --retry 3 -o "/opt/katago/models/${MAIN_NET}" \
      "https://github.com/lightvector/KataGo/releases/download/${MAIN_NET_TAG}/${MAIN_NET}"

COPY image/analysis.cfg /opt/katago/analysis.cfg
COPY image/server.py /opt/katago/server.py
COPY image/start.sh /opt/katago/bin/start.sh
RUN chmod +x /opt/katago/bin/start.sh \
    && mkdir -p /workspace/katago/.home

# 端口只用于文档化；进程默认仅绑定 127.0.0.1，从上海控制端经 SSH -L 进入。
EXPOSE 2718
HEALTHCHECK --interval=15s --timeout=3s --start-period=600s --retries=3 \
    CMD python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:2718/health", timeout=2).read()'

ENTRYPOINT ["tini", "--", "/opt/katago/bin/start.sh"]

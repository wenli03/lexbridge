# =============================================================================
# LexBridge API 服务镜像
# =============================================================================
# 多阶段构建，Java 21（虚拟线程是 4+1 进程视图里承载长 SSE 的关键设计）。
#
# 镜像选择说明：构建阶段用 maven 官方镜像而不是仓库里的 mvnw，
# 因为在 CI 与容器构建里直接复用官方镜像更快也更省事；
# mvnw 的用途是让**本机没有装 Maven 的人**（包括本项目的开发环境）也能构建。
# 两者并存不是冗余，服务的是不同场景。
# =============================================================================

# ---------------------------------------------------------------------------
# 阶段 1：构建
# ---------------------------------------------------------------------------
FROM maven:3.9-eclipse-temurin-21 AS builder

# ---------------------------------------------------------------------------
# 可选：Maven 镜像源
# ---------------------------------------------------------------------------
# 默认留空，走官方 Maven Central——保证这个仓库在任何网络环境下都能构建。
#
# 但在部分网络环境下（例如中国大陆直连 Central）会极慢甚至**静默卡住**，
# 表现为构建进程 CPU 归零、缓存不增长，没有任何错误信息。
# 这类"不是失败、只是不动"的状态最难判断，因此显式提供这个开关。
#
# 用法：在 deploy/.env 里设置 MAVEN_MIRROR_URL，compose 会作为 build arg 传入。
ARG MAVEN_MIRROR_URL=""

RUN if [ -n "$MAVEN_MIRROR_URL" ]; then \
        mkdir -p /root/.m2 && \
        printf '%s' \
          "<settings><mirrors><mirror><id>mirror</id><mirrorOf>central</mirrorOf><url>${MAVEN_MIRROR_URL}</url></mirror></mirrors></settings>" \
          > /root/.m2/settings.xml && \
        echo "已配置 Maven 镜像：${MAVEN_MIRROR_URL}"; \
    else \
        echo "使用官方 Maven Central"; \
    fi

WORKDIR /build

# 先只拷贝 pom.xml 并预下载依赖。这一步让依赖层能被 Docker 缓存复用：
# 改业务代码时不会重新下载几十兆依赖，构建从几分钟降到几秒。
COPY pom.xml .
RUN --mount=type=cache,target=/root/.m2 \
    mvn -B -q dependency:go-offline

COPY src ./src
RUN --mount=type=cache,target=/root/.m2 \
    mvn -B -q clean package -DskipTests

# 按层拆包。Spring Boot 分层 jar 把「依赖」与「业务代码」分开，
# 下面的 COPY 顺序让依赖层单独成层——改一行业务代码只重建最后一层。
#
# 用 -Djarmode=tools 而非已弃用的 -Djarmode=layertools（Spring Boot 3.3 起
# 后者会打印弃用警告）。实测两者的输出结构完全一致：
# application/ / dependencies/ / snapshot-dependencies/ / spring-boot-loader/，
# 因此下面的 COPY 路径不需要改。
RUN java -Djarmode=tools -jar target/*.jar extract --layers --launcher \
        --destination /build/extracted

# ---------------------------------------------------------------------------
# 阶段 2：运行
# ---------------------------------------------------------------------------
FROM eclipse-temurin:21-jre

# curl 供 healthcheck 使用
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行。容器被攻陷时，这一步决定攻击者能不能直接改应用代码。
RUN useradd --create-home --shell /bin/bash --uid 10001 lexbridge

WORKDIR /app

# 顺序有讲究：变化越少的层放越前面
COPY --from=builder --chown=lexbridge:lexbridge /build/extracted/dependencies/ ./
COPY --from=builder --chown=lexbridge:lexbridge /build/extracted/spring-boot-loader/ ./
COPY --from=builder --chown=lexbridge:lexbridge /build/extracted/snapshot-dependencies/ ./
COPY --from=builder --chown=lexbridge:lexbridge /build/extracted/application/ ./

# 上传文件与导出报告的落盘目录（由 compose 挂载卷）
#
# ⚠️ 必须在 USER lexbridge **之前**创建。放在切用户之后会失败在
#    `mkdir: cannot create directory '/data/files': Permission denied` ——
#    在根目录下建目录需要 root 权限。这个顺序错误只在构建时暴露。
RUN mkdir -p /data/files && chown -R lexbridge:lexbridge /data

USER lexbridge

VOLUME ["/data/files"]

EXPOSE 8080

# 容器感知的内存参数：让 JVM 按 cgroup 限额而不是宿主总内存来算堆大小。
# 不设的话，容器里 JVM 会看到宿主机的全部内存并据此分配堆，
# 在编排环境下极易被 OOM Kill，而日志里看不出任何征兆。
ENV JAVA_OPTS="-XX:MaxRAMPercentage=75 -XX:+UseG1GC -XX:+ExitOnOutOfMemoryError"

# 优雅停机交给 Spring 的 graceful shutdown 处理，给它留出排空 SSE 连接的时间
STOPSIGNAL SIGTERM

ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS org.springframework.boot.loader.launch.JarLauncher"]

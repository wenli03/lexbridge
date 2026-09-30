package com.lexbridge.infrastructure.ai;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.lexbridge.application.port.IngestionGateway;
import com.lexbridge.infrastructure.config.AiServiceProperties;
import com.lexbridge.infrastructure.security.InternalTokenIssuer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.LocalDate;

/**
 * 调用 {@code ai} 服务内部端点的客户端。
 *
 * <p><b>用 JDK 自带的 {@code HttpClient} 而不是 Spring 的 RestClient。</b>
 * 这里的诉求只有三条：显式区分建连与读取超时、带一个内部令牌头、把响应体读成 JSON。
 * {@code RestClient} 需要额外装配请求工厂才能做到超时可控，而它带来的抽象在这个
 * "只有两个端点、无重试编排、无拦截器"的场景里没有回报。技术栈里也不该为此多一样东西。
 *
 * <p><b>失败一律抛 {@link IngestionSubmissionException}，不吞。</b> 上传接口若在
 * {@code ai} 不可达时返回 200，使用者会以为法规已进入流水线，
 * 而实际上什么都没发生——这类"假成功"比明确的报错难查得多。
 */
@Component
public class AiServiceClient implements IngestionGateway {

    private static final Logger log = LoggerFactory.getLogger(AiServiceClient.class);

    /** 与《详细设计》§2.3 的契约一致。Python 侧必须按同一路径实现。 */
    private static final String INDEX_DOCUMENT_PATH = "/internal/index/document";

    private static final String INTERNAL_TOKEN_HEADER = "X-Internal-Token";
    private static final String TRACE_ID_HEADER = "X-Request-Id";

    private final AiServiceProperties properties;
    private final ObjectMapper objectMapper;
    private final InternalTokenIssuer tokenIssuer;
    private final HttpClient httpClient;

    public AiServiceClient(AiServiceProperties properties, ObjectMapper objectMapper,
                           InternalTokenIssuer tokenIssuer) {
        this.properties = properties;
        this.objectMapper = objectMapper;
        this.tokenIssuer = tokenIssuer;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(properties.connectTimeout())
                .build();
    }

    @Override
    public String submit(IngestionRequest request) {
        // 租户身份由令牌承载，不进请求体（《详细设计》§2.3）。
        // 入库不属于图执行，没有 runId
        String token = tokenIssuer.issue(null);

        ObjectNode body = objectMapper.createObjectNode();
        body.put("filePath", request.filePath());
        body.put("contentHash", request.contentHash());
        body.put("sourceUrl", request.sourceUrl());
        body.put("effectiveFrom", iso(request.effectiveFrom()));
        // 可空字段不写 null，直接不出现——两种表达对 Python 侧的含义相同，
        // 但省略能让请求体在日志里更短，也少一个"null 还是缺省"的歧义
        if (request.effectiveTo() != null) {
            body.put("effectiveTo", iso(request.effectiveTo()));
        }
        body.put("versionLabel", request.versionLabel());
        body.put("statuteTitle", request.statuteTitle());
        body.put("jurisdictionCode", request.jurisdictionCode());

        HttpRequest httpRequest = HttpRequest.newBuilder()
                .uri(URI.create(properties.baseUrl() + INDEX_DOCUMENT_PATH))
                .timeout(properties.readTimeout())
                .header("Content-Type", "application/json; charset=utf-8")
                .header(INTERNAL_TOKEN_HEADER, token)
                // 追踪 ID 必须透传，否则前后端与 ai 三侧的日志无法对齐；
                // 跨 Java 与 Python 两个运行时，这是唯一能串起来的抓手
                .header(TRACE_ID_HEADER, request.traceId() == null ? "" : request.traceId())
                .POST(HttpRequest.BodyPublishers.ofString(
                        body.toString(), StandardCharsets.UTF_8))
                .build();

        HttpResponse<String> response;
        try {
            response = httpClient.send(httpRequest, HttpResponse.BodyHandlers.ofString(
                    StandardCharsets.UTF_8));
        } catch (IOException e) {
            throw new IngestionSubmissionException(
                    "无法连接 ai 服务（" + properties.baseUrl() + "）：" + e.getMessage(), e);
        } catch (InterruptedException e) {
            // 恢复中断标记：吞掉中断会让上层无法感知取消，是并发场景下的经典缺陷
            Thread.currentThread().interrupt();
            throw new IngestionSubmissionException("调用 ai 服务被中断", e);
        }

        if (response.statusCode() / 100 != 2) {
            log.warn("ai 服务返回非 2xx：status={} traceId={}",
                    response.statusCode(), request.traceId());
            throw new IngestionSubmissionException(
                    "ai 服务返回异常状态 " + response.statusCode() + "：" + truncate(response.body()));
        }

        return extractJobId(response.body());
    }

    /**
     * 从响应体里取 jobId。
     *
     * <p>取不到就抛异常而不是返回空串或 null：调用方拿到一个空的 jobId 后，
     * 前端会显示一个查不到任何状态的任务，而使用者只会认为"上传卡住了"。
     */
    private String extractJobId(String responseBody) {
        try {
            JsonNode root = objectMapper.readTree(responseBody);
            JsonNode jobId = root.path("jobId");
            if (jobId.isMissingNode() || jobId.isNull() || jobId.asText().isBlank()) {
                throw new IngestionSubmissionException(
                        "ai 服务响应里没有 jobId：" + truncate(responseBody));
            }
            return jobId.asText();
        } catch (IOException e) {
            throw new IngestionSubmissionException(
                    "无法解析 ai 服务响应：" + truncate(responseBody), e);
        }
    }

    private static String iso(LocalDate date) {
        return date == null ? null : date.toString();
    }

    /** 响应体可能很长（错误页），截断后再放进异常信息，避免把日志淹掉。 */
    private static String truncate(String text) {
        if (text == null) {
            return "";
        }
        return text.length() <= 300 ? text : text.substring(0, 300) + "…";
    }
}

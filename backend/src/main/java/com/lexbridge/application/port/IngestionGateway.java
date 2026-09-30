package com.lexbridge.application.port;

import java.time.LocalDate;

/**
 * 向 {@code ai} 服务提交入库任务的出口。
 *
 * <p><b>为什么是端口而不是直接调用客户端。</b> 项目的分层规则是
 * 「Infrastructure 不得被任何层访问」（由 ArchUnit 强制）。应用服务若直接依赖
 * {@code infrastructure.ai.AiServiceClient}，那条依赖会被拦下——这不是形式主义：
 * 直接依赖会让应用层无法在不启动 HTTP 客户端的情况下被测试，而
 * {@code TokenIssuer} 已经确立了同样的做法。
 *
 * <p>实现放在 {@code infrastructure.ai}，方向是 infrastructure → application，属于依赖倒置的正常形态。
 *
 * <h3>关于请求体</h3>
 *
 * <p>这里定义的字段是**冻结的契约**，Python 侧的 {@code /internal/index/document}
 * 必须按此实现（对应《详细设计》§2.3）。字段取自 {@code kb} 建 job 所需的最小集合，
 * 由 {@code api} 收集完成后一次性交给 {@code ai}：
 * 一次调用比"api 建一半、ai 建一半"少一个跨服务的半成品状态。
 *
 * <p><b>请求体里没有租户字段。</b> 租户身份由实现签进内部令牌（见
 * {@code infrastructure.security.InternalTokenIssuer}），{@code ai} 侧从令牌解析。
 * 把租户放进请求体意味着接收方要信任一个未签名的字段，那条路径一旦存在，
 * `AC-5.1`「跨租户 0 成功」就不再成立。
 *
 * @param filePath         原件在共享卷内的路径
 * @param contentHash      原件哈希，用于判重（同一份法规重复上传不该产生第二个版本）
 * @param sourceUrl        公开来源 URL。{@code AC-1.6} 要求可溯源
 * @param effectiveFrom    生效起始日
 * @param effectiveTo      失效日，可为空
 * @param versionLabel     版本标签
 * @param statuteTitle     法规名称
 * @param jurisdictionCode 法域代码
 * @param traceId          追踪 ID，透传给 {@code ai} 以便两侧日志对齐
 */
public interface IngestionGateway {

    /**
     * 提交入库任务。
     *
     * @return {@code ai} 侧创建的 jobId
     * @throws IngestionSubmissionException 提交失败（不可达、超时、非 2xx、
     *                                      或响应体里没有 jobId）
     */
    String submit(IngestionRequest request);

    /**
     * 提交入库任务的入参。
     *
     * <p>用 record 承载而不是十个方法参数：十个参数的调用点极易传错顺序，
     * 而其中 {@code effectiveFrom} 与 {@code effectiveTo} 类型相同，
     * 传反了不会编译失败，只会让生效区间变成一段无意义的负区间。
     */
    record IngestionRequest(
            String filePath,
            String contentHash,
            String sourceUrl,
            LocalDate effectiveFrom,
            LocalDate effectiveTo,
            String versionLabel,
            String statuteTitle,
            String jurisdictionCode,
            String traceId
    ) {
    }

    /**
     * 提交失败。
     *
     * <p>定义在端口里而不是基础设施层：调用方（应用服务）需要按类型捕获它并决定
     * 对外返回什么状态码，而它不该认识"是 HTTP 超时还是连接被拒"这类细节——
     * 那是实现的私事。
     */
    class IngestionSubmissionException extends RuntimeException {

        private static final long serialVersionUID = 1L;

        public IngestionSubmissionException(String message) {
            super(message);
        }

        public IngestionSubmissionException(String message, Throwable cause) {
            super(message, cause);
        }
    }
}

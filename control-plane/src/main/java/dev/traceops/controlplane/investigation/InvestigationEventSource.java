package dev.traceops.controlplane.investigation;

import java.util.List;

/**
 * 回程消息的读取抽象：读一批 + 逐条 ACK。
 *
 * <p>为什么把 Redis 读取抽成接口，而不是让 poller 直接持有 {@code StringRedisTemplate}：
 * <ol>
 *   <li><b>测试可覆盖核心逻辑</b>——"哪些消息处理、哪些 ACK"才是本任务真正的行为，
 *       它应该能被真实测试完整覆盖，而不是因为 CI 没有 Redis 就被 {@code @MockBean} 掩盖。</li>
 *   <li><b>读语义与业务语义分层</b>——consumer group 的创建、字段解析、ACK 是基础设施细节；
 *       事件类型过滤与幂等是业务细节。混在一个类里，改 Redis 细节就会碰业务代码。</li>
 * </ol>
 *
 * <p>契约：{@link #read()} 返回的每条消息都已进入本消费组的 PEL（pending entries list），
 * 调用方必须对每条消息最终显式选择 {@link #ack(String)} 或"故意不 ACK（留待下轮重投）"。
 */
public interface InvestigationEventSource {

    /**
     * 拉取一批新消息。没有消息时返回空列表，不返回 null。
     *
     * <p>实现方负责保证 consumer group 已存在（否则 XREADGROUP 会直接报 NOGROUP）。
     * 这个方法本身不阻塞：调度间隔才是"多久看一次"的唯一来源。
     */
    List<StreamMessage> read();

    /** 确认一条消息已处理完毕，把它从 PEL 移除。幂等：重复 ACK 是空操作。 */
    void ack(String id);
}

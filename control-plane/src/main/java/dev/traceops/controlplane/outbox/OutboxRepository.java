package dev.traceops.controlplane.outbox;

import java.time.Instant;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;

public interface OutboxRepository extends JpaRepository<OutboxEvent, String> {

    List<OutboxEvent> findTop50ByPublishedAtIsNullOrderByCreatedAtAsc();

    /**
     * 取出当前**可以投递**的待投递事件（最多 50 条）。
     *
     * <p>与 {@link #findTop50ByPublishedAtIsNullOrderByCreatedAtAsc()} 的区别：
     * 退避中的事件（{@code notBefore} 在未来）不在结果里。
     *
     * <p>为什么必须由数据库过滤而不是 relay 自己判断：relay 取一批后逐条投递，
     * 第一条失败就 {@code break}（保持顺序）。若退避事件混在批次中间，
     * 它会挡住后面所有事件 —— 一条"还没到时间"的重试会卡住整条投递流水线。
     */
    @Query(value = "select * from outbox_events where published_at is null"
            + " and (not_before is null or not_before <= :now)"
            + " order by created_at asc fetch first 50 rows only",
            nativeQuery = true)
    List<OutboxEvent> findDeliverable(Instant now);

    /** 全量计数（含已发布）—— 诊断用：区分"没写进去"与"写了但被当已发布"。 */
    long count();

    List<OutboxEvent> findAll();

    long countByPublishedAtIsNull();

    /** 时间窗内写入的某类事件数（漏斗用：这一窗发生过多少次调查请求）。 */
    long countByCreatedAtAfterAndEventType(Instant since, String eventType);

    /**
     * 最老的待投递事件。
     *
     * <p>为什么要"最老"而不是"总数"：积压 200 条但都是刚写入的是正常突发，
     * 积压 3 条但最老的一条等了 20 分钟才是故障。只看数量会把两者混为一谈。
     */
    java.util.Optional<OutboxEvent> findFirstByPublishedAtIsNullOrderByCreatedAtAsc();

    /**
     * 条件标记已发布：只有当这一行仍是"未发布、且 attempts 与我读到的一致"时才成功。
     *
     * <p>为什么必须是条件更新、而不是 {@code save(event)}：
     * <ul>
     *   <li>两个实例同时 relay 时会取到同一批 {@code published_at is null} 的行，
     *       各自发布一次 —— 消费端幂等能兜住重复，但"投递了两次"在账面上看不出来；</li>
     *   <li>更糟的是竞态窗口：A 发布后把行标成已发布，B 接着把同一行
     *       标成"发布失败、attempts+1"，于是成功的一行被记成失败，还会被重投。</li>
     * </ul>
     * 带 {@code attempts} 的 CAS 把"我读到的那一版"钉进where：行被别人改过就放弃标记，
     * 让下一次 relay 按新状态重新决定 —— 宁可多一次重复投递，也不要记错账。
     *
     * @return 1 = 标记成功（我是发布者）；0 = 已被别人发布或状态已变（我不是发布者）
     */
    @Modifying(clearAutomatically = true, flushAutomatically = true)
    @Query("update OutboxEvent e set e.publishedAt = :now"
            + " where e.eventId = :eventId and e.publishedAt is null and e.attempts = :attempts")
    int markPublishedIfUntouched(String eventId, Instant now, int attempts);
}

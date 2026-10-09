package dev.traceops.controlplane.outbox;

import java.time.Instant;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
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
}

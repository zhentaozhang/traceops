package dev.traceops.controlplane.outbox;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface OutboxRepository extends JpaRepository<OutboxEvent, String> {

    List<OutboxEvent> findTop50ByPublishedAtIsNullOrderByCreatedAtAsc();

    /** 全量计数（含已发布）—— 诊断用：区分"没写进去"与"写了但被当已发布"。 */
    long count();

    List<OutboxEvent> findAll();

    long countByPublishedAtIsNull();
}

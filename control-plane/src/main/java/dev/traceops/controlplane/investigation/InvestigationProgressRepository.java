package dev.traceops.controlplane.investigation;

import java.time.Instant;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;

public interface InvestigationProgressRepository extends JpaRepository<InvestigationProgressRow, String> {

    /** TTL 清理：返回删除行数，好让"清掉了多少"是可观测的。 */
    @Modifying
    @Query("delete from InvestigationProgressRow r where r.updatedAt < :cutoff")
    int deleteUpdatedBefore(Instant cutoff);
}

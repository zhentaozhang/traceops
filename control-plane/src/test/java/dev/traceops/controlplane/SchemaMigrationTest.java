package dev.traceops.controlplane;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;

/**
 * schema 来自 `db/migration/*.sql`，Hibernate 只校验，不建表。
 *
 * <p>这个测试用<b>独立的H2库</b>（不与其它测试共享），把 `ddl-auto` 钉在 `validate`：
 * 如果迁移脚本与实体对不上，Spring 上下文起不来 —— 这正是我们要的失败方式。
 * 在此之前默认值是 `create-drop`，实体改了没人知道，重启把数据删了也没人知道。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.datasource.url=jdbc:h2:mem:schema-check;DB_CLOSE_DELAY=-1",
        "spring.jpa.hibernate.ddl-auto=validate"
})
class SchemaMigrationTest {

    @Autowired
    private JdbcTemplate jdbc;

    @Test
    void flywayCreatedEveryTableTheEntitiesNeed() {
        List<String> tables = jdbc.queryForList(
                "select lower(table_name) from information_schema.tables where lower(table_schema) = 'public'",
                String.class);

        assertThat(tables).contains(
                "incidents",
                "outbox_events",
                "applied_investigation_events",
                "investigation_findings",
                "investigation_finding_history",
                "investigation_progress");
    }

    @Test
    void theMigrationIsRecordedAsApplied() {
        Integer applied = jdbc.queryForObject(
                "select count(*) from \"flyway_schema_history\"", Integer.class);

        assertThat(applied).isGreaterThanOrEqualTo(1);
    }
}

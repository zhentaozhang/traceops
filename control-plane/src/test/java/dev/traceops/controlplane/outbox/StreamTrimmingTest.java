package dev.traceops.controlplane.outbox;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/**
 * 事件流必须带**上限**，否则无限增长。
 *
 * <p>缺陷：Python 侧注释明确写着
 * 「裁剪应该由生产端的 {@code XADD ... MAXLEN} / 运维的 {@code XTRIM} 负责」，
 * 而生产端（本模块）的 XADD **没有 MAXLEN**。
 *
 * <p>后果不是"以后再说"：每个调查约 9 条事件（7 节点进度 + started + completed），
 * 一次规模化运行就能让流涨到几十万条。Redis Stream 的 XACK **不删除数据**
 * （它只从 PEL 移除），所以内存只会涨不会跌 —— 这是 Streams 最常见的内存泄漏。
 *
 * <p>为什么用 {@code MAXLEN ~} 近似裁剪而不是精确值：
 * 精确 XADD MAXLEN 每次插入都要重新计数并可能删除多条；近似形式只在
 * 能整块丢弃 radix tree 节点时才裁剪，成本低得多。代价是长度会略超上限，
 * 对我们"保留最近若干条用于排障"的用途完全可接受。
 */
class StreamTrimmingTest {

    @Test
    void streamKeyIsExposedForTrimmingPolicy() {
        assertThat(RedisStreamEventPublisher.STREAM_KEY).isEqualTo("traceops.events");
    }

    @Test
    void trimmingIsApproximateAndBounded() {
        RedisStreamEventPublisher publisher = new RedisStreamEventPublisher(
                "localhost", 6379, RedisStreamEventPublisher.DEFAULT_MAX_LEN);
        assertThat(publisher.maxLen()).isGreaterThan(0);
    }

    @Test
    void trimmingCanBeDisabledExplicitly() {
        // 允许关闭（例如需要完整重放时），但必须显式选择，
        // 不能靠"忘了配置"就无限增长。
        RedisStreamEventPublisher unlimited = new RedisStreamEventPublisher(
                "localhost", 6379, 0);
        assertThat(unlimited.maxLen()).isZero();
    }
}

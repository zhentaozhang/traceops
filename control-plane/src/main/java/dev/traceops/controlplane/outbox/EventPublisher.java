package dev.traceops.controlplane.outbox;

/** 事件投递端口。P0 有日志实现与 Redis Streams 实现。 */
public interface EventPublisher {

    /** 发布一条完整消息（{envelope, payload} 的 JSON）。失败必须抛异常，由 relay 记账。 */
    void publish(String message) throws Exception;
}

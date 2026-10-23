package dev.traceops.controlplane.notify;

/**
 * 出站通知的出口。
 *
 * <p>做成接口而不是直接调 HTTP：通知是**非关键路径**，它失败不能让状态机回滚，
 * 而测试需要能确认"该通知的时候通知了、不该通知的时候没通知"，不需要真起一个 HTTP 服务。
 */
public interface NotificationSender {

    /**
     * 送出通知。实现**不得**抛出异常：通知失败不是业务失败。
     */
    void send(IncidentNotification notification);
}

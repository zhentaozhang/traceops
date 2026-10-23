package dev.traceops.controlplane.notify;

import java.time.Instant;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.event.TransactionPhase;
import org.springframework.transaction.event.TransactionalEventListener;

/**
 * 把"调查结算了"翻译成"该让某个人知道"。
 *
 * <p>{@code AFTER_COMMIT}：事务回滚了就不该有人收到"事故已诊断"。
 * {@code fallbackExecution = true}：没有事务上下文时（例如被直接调用、或在测试里）
 * 也要送到 —— 否则"通知"会在一条不该丢的路径上静默消失。
 *
 * <p>两条克制：
 * <ol>
 *   <li><b>只通知需要人看的结果</b>：状态真的推进了，或者停在了"需要人工复核"。
 *       每一次成功的中间步骤都推一条通知，等于把通知变成噪声，噪声会被静音，
 *       然后真正需要人的那一条也一起被忽略。</li>
 *   <li><b>异常全部吞掉并记录</b>：通知失败不是业务失败，而事件监听器抛异常会污染
 *       已提交事务后的调用栈。</li>
 * </ol>
 */
@Service
public class IncidentNotifier {

    private static final Logger log = LoggerFactory.getLogger(IncidentNotifier.class);

    /** 这些结算结果值得打扰一个人。 */
    static final List<String> NOTIFIABLE_OUTCOMES = List.of("APPLIED", "NEEDS_HUMAN_REVIEW");

    private final List<NotificationSender> senders;

    public IncidentNotifier(List<NotificationSender> senders) {
        this.senders = List.copyOf(senders);
    }

    @TransactionalEventListener(phase = TransactionPhase.AFTER_COMMIT, fallbackExecution = true)
    public void onIncidentApplied(IncidentAppliedEvent event) {
        deliver(event, Instant.now());
    }

    /** 供直接调用与测试：返回是否真的送出了。 */
    public boolean deliver(IncidentAppliedEvent event, Instant now) {
        if (event == null) {
            return false;
        }
        if (!NOTIFIABLE_OUTCOMES.contains(String.valueOf(event.outcome()))) {
            log.debug("not notifying for outcome={} incident={}", event.outcome(), event.incidentId());
            return false;
        }
        if (event.at() != null && now.isAfter(event.at().plus(IncidentAppliedEvent.DELIVERY_BUDGET))) {
            // 迟到的通知比没有更糟：它描述的是一个已经变了的状态。
            log.warn("dropping stale notification for incident={} (event at {}, now {})",
                    event.incidentId(), event.at(), now);
            return false;
        }
        if (senders.isEmpty()) {
            // 没有配置任何出口是正常状态（默认部署），不报警，但要说清"通知没发出去"。
            log.debug("no notification sender configured; incident={} outcome={}",
                    event.incidentId(), event.outcome());
            return false;
        }

        IncidentNotification notification = new IncidentNotification(
                event.incidentId(), event.service(), event.severity(), event.origin(),
                event.status(), event.outcome(), event.reason(), event.reportRef(), event.at());

        boolean delivered = false;
        for (NotificationSender sender : senders) {
            try {
                sender.send(notification);
                delivered = true;
            } catch (RuntimeException exc) {
                log.warn("notification sender {} failed for incident={}: {}",
                        sender.getClass().getSimpleName(), event.incidentId(), exc.toString());
            }
        }
        return delivered;
    }
}

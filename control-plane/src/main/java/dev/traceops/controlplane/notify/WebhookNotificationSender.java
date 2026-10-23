package dev.traceops.controlplane.notify;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

/**
 * 把通知 POST 到一个 webhook（Slack incoming webhook、工单系统、值班脚本都吃这个形状）。
 *
 * <p>为什么用 JDK 自带的 {@link HttpClient}：这是唯一的出站网络调用，为它引入
 * WebClient/RestTemplate 只增加依赖面。请求体是 JSON，超时是硬上限。
 *
 * <p>为什么默认不启用：没有配置 URL 时**不能**假装通知成功。装配该 bean 的条件就是
 * `traceops.notify.webhook-url` 有值，于是"没配"与"配了但没送到"在日志里是可区分的。
 */
@Component
@ConditionalOnProperty(name = "traceops.notify.webhook-url")
public class WebhookNotificationSender implements NotificationSender {

    private static final Logger log = LoggerFactory.getLogger(WebhookNotificationSender.class);

    private final ObjectMapper mapper;
    private final URI target;
    private final Duration timeout;
    private final HttpClient client;

    public WebhookNotificationSender(
            ObjectMapper mapper,
            @Value("${traceops.notify.webhook-url}") String webhookUrl,
            @Value("${traceops.notify.timeout-ms:3000}") long timeoutMs) {
        if (webhookUrl == null || webhookUrl.isBlank()) {
            throw new IllegalArgumentException("traceops.notify.webhook-url must not be blank");
        }
        this.mapper = mapper;
        this.target = URI.create(webhookUrl);
        this.timeout = Duration.ofMillis(timeoutMs <= 0 ? 3000 : timeoutMs);
        this.client = HttpClient.newBuilder()
                .connectTimeout(this.timeout)
                // 不跟随重定向：一个把 POST 变成 GET 的 302 会让"告警送到"变成"告警丢了"，
                // 而且跟随后的目标地址没被任何人批准过。
                .followRedirects(HttpClient.Redirect.NEVER)
                .build();
    }

    @Override
    public void send(IncidentNotification notification) {
        try {
            String payload = mapper.writeValueAsString(payloadOf(notification));
            HttpRequest request = HttpRequest.newBuilder(target)
                    .timeout(timeout)
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(payload))
                    .build();
            HttpResponse<String> response = client.send(request,
                    HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() / 100 != 2) {
                log.warn("webhook rejected the notification: status={} incident={} body={}",
                        response.statusCode(), notification.incidentId(), truncate(response.body()));
            }
        } catch (InterruptedException exc) {
            // 恢复中断标记，否则上层的停机检查看不到它。
            Thread.currentThread().interrupt();
            log.warn("webhook delivery was interrupted for incident={}", notification.incidentId());
        } catch (Exception exc) {
            // 通知是最外层的事：这里的任何失败都不能往上冒。
            log.warn("webhook delivery failed for incident={}: {}",
                    notification.incidentId(), exc.toString());
        }
    }

    Map<String, Object> payloadOf(IncidentNotification notification) {
        Map<String, Object> payload = new LinkedHashMap<>();
        // 同时给结构化字段与人读文本：接收方若要路由用字段，若要展示用 text。
        payload.put("incident_id", notification.incidentId());
        payload.put("service", notification.service());
        payload.put("severity", notification.severity());
        payload.put("origin", notification.origin());
        payload.put("status", notification.status());
        payload.put("outcome", notification.outcome());
        payload.put("reason", notification.reason());
        payload.put("report_ref", notification.reportRef());
        payload.put("at", notification.at() == null ? null : notification.at().toString());
        payload.put("subject", notification.subject());
        payload.put("text", notification.body());
        return payload;
    }

    private static String truncate(String value) {
        if (value == null) {
            return "";
        }
        return value.length() <= 200 ? value : value.substring(0, 200) + "…";
    }
}

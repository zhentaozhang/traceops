package dev.traceops.controlplane.contracts;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SpecVersion;
import com.networknt.schema.ValidationMessage;
import com.networknt.schema.resource.MapSchemaLoader;
import com.networknt.schema.resource.SchemaLoaders;

import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 用 {@code contracts/} 里的 JSON Schema 校验**将要发出**的事件。
 *
 * <h2>为什么要在生产代码里做</h2>
 *
 * 契约文件存在、测试里也在用，但生产代码从不校验。于是契约与实现可以长期
 * 不一致而无人发现 —— 曾经真的发生过：人工创建的调查传了
 * {@code priority="manual"}，而 schema 的 enum 只有 {@code low/normal/high}。
 * 那个 bug 是靠离线跑契约测试才发现的，运行时不报错、日志不告警、事件照发。
 *
 * <h2>为什么在发事件的地方校验，而不是在发出去之后</h2>
 *
 * Outbox 里出现坏消息就太晚了：它会被投递、会被下游拒绝、会进死信，
 * 而根因（某个字段写了枚举外的值）早在几步之前就产生了。
 * 在组装消息的地方同步校验，坏消息根本进不了 outbox ——
 * 生产它的那次请求直接失败，事务回滚，栈顶就是出错的那一行。
 *
 * <h2>schema 从哪来</h2>
 *
 * {@code control-plane/pom.xml} 把仓库根目录的 {@code contracts/} 复制进
 * classpath，因此**只有一份事实来源**：Java 侧不维护第二份契约副本。
 */
public final class ContractValidator {

    /** 已知的 schema 文件。新增事件类型时这里和 {@link #SCHEMA_IRI_BY_EVENT} 一起改。 */
    private static final List<String> SCHEMA_RESOURCES = List.of(
            "contracts/common/event-envelope.schema.json",
            "contracts/common/time-range.schema.json",
            "contracts/events/v1/investigation-requested.schema.json",
            "contracts/events/v1/investigation-updated.schema.json",
            "contracts/events/v1/investigation-completed.schema.json",
            "contracts/events/v1/investigation-failed.schema.json");

    private static final String BASE_IRI = "https://traceops.dev/contracts/";

    private static final Map<String, String> SCHEMA_IRI_BY_EVENT = Map.of(
            "investigation.requested",
            BASE_IRI + "events/v1/investigation-requested.schema.json",
            "investigation.updated",
            BASE_IRI + "events/v1/investigation-updated.schema.json",
            "investigation.completed",
            BASE_IRI + "events/v1/investigation-completed.schema.json",
            "investigation.failed",
            BASE_IRI + "events/v1/investigation-failed.schema.json");

    private static final Pattern ID_PATTERN =
            Pattern.compile("\"\\$id\"\\s*:\\s*\"([^\"]+)\"");

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final class Holder {
        private static final ContractValidator INSTANCE = new ContractValidator();
    }

    private final JsonSchemaFactory factory;
    /** 编译后的 schema 按 IRI 缓存：每条事件都重新编译会明显拖慢出站路径。 */
    private final Map<String, JsonSchema> compiled = new ConcurrentHashMap<>();

    private ContractValidator() {
        Map<String, String> byIri = new LinkedHashMap<>();
        for (String resource : SCHEMA_RESOURCES) {
            String text = readResource(resource);
            byIri.put(iriOf(text, resource), text);
        }
        MapSchemaLoader loader = new MapSchemaLoader(byIri);
        this.factory = JsonSchemaFactory.getInstance(
                SpecVersion.VersionFlag.V202012,
                builder -> builder.schemaLoaders(
                        (SchemaLoaders.Builder loaders) -> loaders.add(loader)));
    }

    public static ContractValidator getInstance() {
        return Holder.INSTANCE;
    }

    /**
     * 校验一条待发出的事件；不合规时抛 {@link ContractViolationException}。
     *
     * @throws ContractViolationException 消息与契约不符
     */
    public static void requireValid(String eventType, Map<String, Object> message) {
        getInstance().validate(eventType, message);
    }

    public void validate(String eventType, Map<String, Object> message) {
        String iri = SCHEMA_IRI_BY_EVENT.get(eventType);
        if (iri == null) {
            // 未登记的事件类型直接拒绝，而不是"没有 schema 就放行" ——
            // 后者等于给未来所有新事件开一个静默后门。
            throw new ContractViolationException(eventType,
                    List.of("no schema registered for event type '" + eventType + "'"));
        }
        JsonSchema schema = compiled.computeIfAbsent(iri, k -> factory.getSchema(SchemaLocation.of(k)));
        Set<ValidationMessage> errors = schema.validate(MAPPER.valueToTree(message));
        if (!errors.isEmpty()) {
            List<String> messages = new ArrayList<>();
            for (ValidationMessage error : errors) {
                messages.add(error.getMessage());
            }
            messages.sort(String::compareTo);
            throw new ContractViolationException(eventType, messages);
        }
    }

    /** 供测试确认"schema 真的加载到了"，而不是空校验器悄悄放行一切。 */
    public int schemaCount() {
        return SCHEMA_IRI_BY_EVENT.size();
    }

    private static String readResource(String path) {
        try (InputStream in = ContractValidator.class.getClassLoader().getResourceAsStream(path)) {
            if (in == null) {
                throw new IllegalStateException(
                        "contract schema not on classpath: " + path
                                + " (is the contracts/ resource copy configured in pom.xml?)");
            }
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException exc) {
            throw new UncheckedIOException("failed to read contract schema: " + path, exc);
        }
    }

    private static String iriOf(String schemaText, String resource) {
        Matcher matcher = ID_PATTERN.matcher(schemaText);
        if (!matcher.find()) {
            throw new IllegalStateException("schema has no $id: " + resource);
        }
        return matcher.group(1);
    }

    /** Jackson 转换后的树只用于校验，不做其它用途。 */
    static JsonNode toTree(Map<String, Object> message) {
        return MAPPER.valueToTree(message);
    }
}

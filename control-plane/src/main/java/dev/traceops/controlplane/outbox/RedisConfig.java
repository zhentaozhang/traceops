package dev.traceops.controlplane.outbox;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.data.redis.connection.RedisStandaloneConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;
import org.springframework.data.redis.core.StringRedisTemplate;

/**
 * 显式装配 Redis 连接。
 *
 * <p><b>为什么不能依赖 Spring Boot 自动配置</b>：实测环境里
 * {@code RedisAutoConfiguration} 没有生效（启动日志中计数为 0），
 * 于是注入的 {@code StringRedisTemplate} 是一个**未初始化的空壳** ——
 * 调用时报 {@code template not initialized; call afterPropertiesSet()}。
 *
 * <p>更糟的是这个空壳在 {@code opsForStream().add()} 路径上
 * **不抛异常、返回 null**，relay 于是把事件标记为"已发布"，
 * 而 Redis 里什么都没有 —— 静默数据丢失。
 *
 * <p>与其猜自动配置为什么没生效，不如自己装配：连接工厂与模板都是显式的，
 * 出错会立刻在启动期暴露，而不是等到投递时才静默丢事件。
 */
@Configuration
@ConditionalOnProperty(name = "traceops.events.redis.enabled", havingValue = "true")
public class RedisConfig {

    @Bean
    public LettuceConnectionFactory redisConnectionFactory(
            @org.springframework.beans.factory.annotation.Value("${spring.data.redis.host:localhost}") String host,
            @org.springframework.beans.factory.annotation.Value("${spring.data.redis.port:6379}") int port) {
        RedisStandaloneConfiguration config = new RedisStandaloneConfiguration(host, port);
        LettuceConnectionFactory factory = new LettuceConnectionFactory(config);
        // 启动期就握手：连不上要立刻失败，而不是让后续投递静默丢数据
        factory.afterPropertiesSet();
        return factory;
    }

    @Bean
    public StringRedisTemplate stringRedisTemplate(LettuceConnectionFactory factory) {
        StringRedisTemplate template = new StringRedisTemplate(factory);
        template.afterPropertiesSet();
        return template;
    }
}

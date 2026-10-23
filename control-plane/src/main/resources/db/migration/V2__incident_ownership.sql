-- 事故的"人"：谁接了、谁确认了结论。
--
-- 为什么必须是列而不是推导：stage 会说 "waiting for a person"，但在这次迁移之前
-- 系统里没有任何地方记录"这个人是谁"。没有负责人的事故无法分派、无法升级、
-- 也无法判断"这个结论被验证过没有" —— 而验证过的结论才是可信的先验。
--
-- confirmation 只存 ACCEPTED / REJECTED：确认是一个**决定**，不是一个进度。
-- 允许改写会让"当初是谁按的接受"不可追溯。
ALTER TABLE incidents ADD COLUMN assignee VARCHAR(128);
ALTER TABLE incidents ADD COLUMN acknowledged_at TIMESTAMP;
ALTER TABLE incidents ADD COLUMN confirmed_at TIMESTAMP;
ALTER TABLE incidents ADD COLUMN confirmed_by VARCHAR(128);
ALTER TABLE incidents ADD COLUMN confirmation VARCHAR(16);

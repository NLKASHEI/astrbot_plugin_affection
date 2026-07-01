# 更新日志

## v1.3

- 🔄 **完全对齐类脑娘投喂实现**：图片必须（无图片提示上传）、丰富人格注入、3次/日+冷却
- 💰 **经济联动**：投喂奖励棱镜币（跨插件写入 economy DB）
- 🤖 **人格注入**：`bot_persona` 配置驱动，投喂/忏悔提示词均注入 Bot 人设
- 📝 **对齐类脑娘标签格式**：`<is_food:是/否;food_desc:...;scene_desc:...;affection:+N;coins:+N>`
- 🎨 **丰富回复格式**：评价+食物识别+场景描写+数据统计+投喂次数
- ⚖️ **忏悔好感度保护**：好感度 ≥ 20 后正面忏悔不再增加好感度
- 📊 **好感度进度条**：10格可视化进度条
- 🛠️ **新增配置项**：`max_daily_feedings`、`feeding_cooldown_minutes`、`bot_persona`、`bot_name`、`currency_name`、`currency_emoji`

## v1.1

- _conf_schema.json WebUI 配置面板
- 忏悔独立 last_confession 冷却字段
- 忏悔好感度由 AI <affection:+/-N> 标签决定
- 每日好感度浮动后台任务
- 图片投喂支持

## v1.0

- 初始版本

# astrbot_plugin_meme_manager_plus v3.1.0

AI 心情表情管理器 — 自动分析 Bot 回复的情绪与表达欲望，双级概率独立判定，智能生成风格多变的表情图片。

## 功能特性

- **双级概率系统**：触发概率（表达欲望评分）和 LLM 生图概率独立判定，可分别开关和调节
- **先发后生**：触发后立即从图库抽图发送，LLM 生图在后台异步执行，不阻塞发送
- **双引擎生图**：支持 Gemini API 和 Grok (xAI) API，可在配置中切换
- **图生图 / 文生图**：心情目录有参考图时以其为风格参考生成新图，目录为空时纯提示词生成
- **自动图库积累**：生成的图片自动保存到对应心情目录，图库越用越丰富
- **图库上限管理**：每个心情目录达到上限后自动切换为随机抽取模式，节省 API 调用
- **自定义心情**：在 `memes/` 下新建文件夹即可添加心情标签，支持中英文，插件自动识别
- **冷却机制**：按群组或会话独立冷却，防止刷屏
- **自动搜图入库**：定时从 Booru 图站（yande.re / konachan / danbooru）或 **Pixiv** 搜索角色图片，LLM 自动判断心情分类后入库
- **Pixiv 搜索支持**：支持 Pixiv 图源，可配置搜索关键词、3 种搜索模式（标签部分匹配 / 精确匹配 / 标题简介搜索）
- **R-18 开关**：Pixiv 专用，可控制是否搜索 R-18 内容（默认关闭，仅搜全年龄）
- **手动搜图命令**：`/搜图 N` 立即搜索指定数量图片入库（上限 50）
- **鲁棒情绪解析**：4 层解析策略（精确匹配 → 模糊匹配 → 索引回退 → 随机回退），兼容各种 LLM 输出格式
- **NovelAI 生图模式**：独立的角色扮演生图模式，开启后替代心情表情流程。LLM 根据对话内容自动补全角色标签，调用 NovelAI API 生成插画
- **参考图三模式**：Vibe Transfer（风格/角色特征引导）、img2img（底图直接变换）、Precise Reference（精确角色参考，仅 V4.5）
- **NovelAI 全参数暴露**：所有 NovelAI API 参数均可在配置面板调整（种子、CFG Rescale、噪声调度、Decrisper、SMEA、Variety Boost 等）
- **Opus 免费优化**：默认配置（V4.5 Full, 832×1216, 28步）在 Opus 会员下免费无限生图

## 安装

通过 AstrBot 插件市场搜索 `meme_manager_plus` 安装，或手动安装：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/Sloan-YXT/astrbot_plugin_meme_manager_plus
```

重启 AstrBot，依赖会自动安装。

## 快速开始

1. 在 AstrBot 管理面板 → 插件配置中，选择一个 LLM 提供商作为**情绪分析提供商**
2. 选择一个提供商作为**生图模型提供商**，并选择引擎（Gemini 或 Grok）
3. 发送消息触发 Bot 回复，插件会自动分析情绪并按表达欲望决定是否发图
4. （可选）在 `memes/` 目录下放入参考图，启用图生图模式

## 配置说明

### 生图 API 设置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| provider_id | 生图模型提供商（自动读取密钥/端点） | — |
| image_provider_type | 生图引擎：`gemini` 或 `grok` | gemini |
| model | 模型名称（留空用默认） | Gemini: gemini-2.0-flash-exp / Grok: grok-imagine-image |
| timeout | 请求超时（秒） | 60 |
| resolution | 图片分辨率 | 1K |
| aspect_ratio | 图片长宽比 | 1:1 |

### 情绪分析设置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| mood_provider_id | 情绪分析用的 LLM 提供商 | 使用默认提供商 |
| custom_mood_prompt | 自定义分析提示词（需含 `{categories}` 和 `{text}`） | 内置模板 |

### 概率设置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| default_probability | 触发概率 (0-100)，控制表达欲望阈值 | 20 |
| llm_generation_enabled | 是否启用 LLM 生图 | true |
| llm_generation_probability | LLM 生图概率 (0-100)，触发后独立判定 | 30 |

**双级概率机制**：
1. 第一级（触发概率）：LLM 输出表达欲望评分 (0.0-1.0)，当 `score > 1 - probability/100` 时触发发图。例如概率设为 20 时，阈值为 0.8，只有强烈情绪才会触发
2. 第二级（生图概率）：触发后独立判定是否调用 API 生成新图。例如设为 30 表示 30% 概率生图
3. 无论是否生图，都从对应心情目录随机抽取一张发送

### 生图设置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| image_prompt_template | 生图提示词模板（`{mood}` 替换为心情） | 内置模板 |
| reference_prompt_addon | 有参考图时的附加提示词 | 内置模板 |
| max_images_per_mood | 每个心情最大图片数（达到后仅抽取，0=不限） | 20 |

### 冷却设置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| cooldown_seconds | 冷却时间（秒） | 60 |
| per_group | 按群组冷却 | true |

### 自动搜图设置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| auto_update_enabled | 启用自动搜图 | false |
| auto_update_interval_hours | 搜图间隔（小时），最小 0.5 | 6 |
| auto_update_search_tags | Booru 搜索标签（空格分隔） | eris_greyrat solo |
| auto_update_images_per_cycle | 每次搜索图片数（1-50） | 5 |
| auto_update_source | 图片来源：danbooru / yandere / konachan / pixiv | danbooru |
| auto_update_min_score | 最低评分过滤（Pixiv 使用收藏数） | 10 |
| auto_update_filter_prompt | 搜图筛选提示词（留空不筛选，LLM 判断是否入库） | — |
| pixiv_refresh_token | Pixiv Refresh Token（使用 Pixiv 图源时必填） | — |
| pixiv_search_keyword | Pixiv 搜索关键词（填写后替代搜索标签，留空则用搜索标签） | — |
| pixiv_search_target | Pixiv 搜索模式：partial_match_for_tags / exact_match_for_tags / title_and_caption | partial_match_for_tags |
| pixiv_allow_r18 | 允许 Pixiv R-18 内容（仅对 Pixiv 生效，默认关闭只搜全年龄） | false |

### NovelAI 生图设置（独立模式）

开启后替代心情表情流程，Bot 回复时按概率触发 NovelAI 角色插画生成。Opus 会员（$25/月）使用默认配置可**免费无限生图**。

#### 基础配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| novelai_enabled | 启用 NovelAI 模式（开启时心情表情流程不生效） | false |
| novelai_api_key | NovelAI API Token | — |
| novelai_model | 生图模型（V4.5 Full 最新最强） | nai-diffusion-4-5-full |
| novelai_base_tags | 角色基础标签（逗号分隔） | 1girl, {{{solo}}}, {{{eris boreas greyrat}}}, mushoku tensei |
| novelai_negative_prompt | 负面提示词 | lowres, bad anatomy... |
| novelai_probability | 触发概率 (0-100) | 30 |
| novelai_width | 图片宽度（Opus 免费: 宽×高 ≤ 1048576） | 832 |
| novelai_height | 图片高度 | 1216 |
| novelai_steps | 生成步数（Opus 免费上限 28） | 28 |
| novelai_scale | 提示词引导强度 | 5.0 |
| novelai_sampler | 采样器 | k_euler_ancestral |
| novelai_tag_prompt | LLM 标签补全提示词模板（需含 `{base_tags}` 和 `{bot_reply}`） | 内置模板 |
| novelai_cooldown_seconds | 独立冷却时间（秒） | 60 |

#### 高级生图参数

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| novelai_seed | 随机种子（-1=每次随机，固定值可复现结果） | -1 |
| novelai_quality_toggle | 质量标签开关 | true |
| novelai_uc_preset | UC 预设（0=Heavy, 1=Light, 2=Human Focus, 3=None） | 0 |
| novelai_cfg_rescale | CFG Rescale（0-1，减少过饱和，仅 V4+） | 0.0 |
| novelai_noise_schedule | 噪声调度（karras/native/exponential/polyexponential，仅 V4+） | karras |
| novelai_dynamic_thresholding | Decrisper 动态阈值（仅 V4+） | false |
| novelai_smea | SMEA 采样增强（仅 V3） | false |
| novelai_smea_dyn | 动态 SMEA（仅 V3） | false |
| novelai_variety_boost | Variety Boost / skip_cfg_above_sigma（0=关闭，推荐 19-26，仅 V4+） | 0.0 |

#### 参考图设置

在配置面板上传参考图后，开启「启用参考图」即可使用。三种模式可选：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| novelai_reference_image | 角色参考图（配置面板上传，只取第一张） | — |
| novelai_use_reference | 启用参考图（关闭时即使有图也不使用） | false |
| novelai_reference_mode | 参考图模式：vibe_transfer / img2img / director | vibe_transfer |

**Vibe Transfer 参数**（提取风格/角色特征引导生图，V4+ 编码收 2 Anlas）：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| novelai_reference_strength | 引导强度（0-1） | 0.6 |
| novelai_reference_info_extracted | 信息提取量（0-1） | 1.0 |

**img2img 参数**（以参考图为底图直接变换，收费）：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| novelai_img2img_strength | 变换强度（0-1，越大改动越大） | 0.6 |
| novelai_img2img_noise | 噪声强度（0-1） | 0.0 |

**Precise Reference 参数**（精确角色参考，仅 V4.5 模型，收 5 Anlas）：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| novelai_director_strength | 参考强度（0-1） | 0.5 |
| novelai_director_fidelity | 保真度（0-1） | 0.5 |
| novelai_director_info_extracted | 信息提取量（0-1） | 1.0 |

> **注意**：选择 director 模式但模型不是 V4.5 时，会自动降级为 Vibe Transfer。

**NovelAI 模式工作流程：**

```
Bot 回复文本
    ↓
概率判定 (novelai_probability)
    ↓ 命中
LLM 分析对话内容，补全角色标签（表情/动作/场景）
    ↓
合并: 基础标签 + 补全标签
    ↓
调用 NovelAI API 生图（+ 参考图引导，如已启用）
    ↓
发送图片 + 保存到 novelai/ 目录
```

## 图库

图库位于插件目录下的 `memes/`，首次运行自动创建预设心情目录。

### 自定义心情

直接在 `memes/` 下新建文件夹即可：

```
memes/
├── happy/        # 有图 → 图生图
├── sad/          # 空目录 → 文生图
├── 摸鱼/         # 支持中文名
└── sleepy/       # 随意添加
```

- 文件夹名 = 心情标签名，LLM 会从所有文件夹名中选择匹配的心情
- 放入参考图 → 图生图模式（以参考图风格生成）
- 不放图片 → 文生图模式（纯提示词生成）
- 生成的图片自动保存到对应目录，逐步积累图库
- 支持格式：jpg, jpeg, png, gif, webp, bmp

### 工作流程

```
Bot 回复文本
    ↓
冷却检查 → 通过
    ↓
独立 LLM 分析 → 输出 score|mood（如 0.85|happy）
    ↓
第一级：表达欲望判定 score > 1 - p ？
    ↓ 通过
从该心情目录随机抽取一张 → 立即发送
    ↓
第二级：LLM 生图概率判定（独立，不阻塞发送）
    ├─ 命中 + 图库未满 → 调用 Gemini/Grok API 生图 → 保存到图库
    └─ 未命中或图库已满 → 跳过
```

### 自动搜图流程

```
定时触发（每 T 小时）
    ↓
Booru API 或 Pixiv API 搜索
    ↓
过滤：评分/收藏数 >= min_score，去重，R-18 开关（仅 Pixiv）
    ↓
并发下载图片（信号量=8，大图自动压缩）
    ↓
（可选）LLM 筛选：根据 filter_prompt 判断 PASS/REJECT
    ↓
LLM Vision 分析每张图片的表情/心情
    ↓
匹配到心情标签 → 保存到对应目录
    ↓
不受 max_images_per_mood 限制，持续积累
```

## 命令

| 命令 | 说明 |
|------|------|
| `心情表情状态` | 查看插件状态和图库统计 |
| `心情表情刷新` | 重新扫描图库目录 |
| `搜图 N` | 手动搜索 N 张图片并分类入库（默认 5，上限 50） |
| `自动搜图开启` | 开启自动搜图入库 |
| `自动搜图关闭` | 关闭自动搜图 |
| `自动搜图立即执行` | 立即执行一次搜图（不等间隔） |

## v3.1.0 更新内容

- **参考图三模式**：NovelAI 参考图支持 Vibe Transfer、img2img、Precise Reference 三种模式
  - **Vibe Transfer**：提取参考图的风格/角色特征引导生图，保真度高
  - **img2img**：以参考图为底图直接变换，可调 strength 和 noise
  - **Precise Reference (Director)**：V4.5 专属精确角色参考，可调 strength 和 fidelity
  - 参考图通过配置面板上传，独立开关控制是否启用
  - 非 V4.5 模型选择 director 时自动降级为 Vibe Transfer
- **全参数暴露**：NovelAI API 所有可调参数均可在配置面板设置
  - 新增：种子、质量标签、UC 预设、CFG Rescale、噪声调度、Decrisper、SMEA/SMEA DYN、Variety Boost
  - 参数按模型版本自动过滤（V4+ 专用参数不发给 V3，反之亦然）
- **默认免费最高配**：默认模型改为 V4.5 Full，默认配置（832×1216, 28步）在 Opus 会员下免费无限生图
- **模型列表更新**：支持 V4.5 Full/Curated、V4 Full/Curated、V3 共 5 个模型
- **配置面板提示优化**：关键配置项标注 Opus 免费限制和各参考模式的 Anlas 费用

## v3.0.0 更新内容

- **NovelAI 生图模式**：全新独立模式，开启后替代心情表情流程
  - LLM 根据 Bot 对话内容自动补全角色标签（表情/动作/场景）
  - 调用 NovelAI Image Generation API 生成角色插画
  - 生成的图片保存到独立的 `novelai/` 目录
  - 所有配置完全独立：概率、冷却、模型、分辨率等
  - 支持 nai-diffusion-4/3 等多个模型
  - 自定义 LLM 标签补全提示词

## v2.1.0 更新内容

- **Pixiv 图源支持**：搜图来源新增 Pixiv，支持通过 Pixiv API 搜索插画
- **Pixiv 搜索关键词**：可单独配置 Pixiv 搜索词，留空时使用通用搜索标签
- **3 种搜索模式**：标签部分匹配（推荐）、标签精确匹配、标题和简介搜索
- **R-18 开关**：Pixiv 专用，关闭时自动过滤 R-18/R-18G 作品（默认关闭）
- **并发下载优化**：两阶段并发流水线（下载信号量=8，LLM 处理信号量=4），搜图速度提升约 4 倍
- **大图自动压缩**：>5MB 的图片自动压缩至 1600px JPEG（Q85），不再跳过大图
- **Gemini 安全过滤**：请求自动携带 safetySettings，减少动漫图被误拦
- **筛选逻辑修正**：LLM 筛选改为首词判定，避免 "reject" 出现在说明文字中导致误拒
- **拒绝日志增强**：被拒绝的图片日志包含作品页面链接（Pixiv / Danbooru / Yandere / Konachan）
- **连接复用**：aiohttp Session 复用，减少 TCP/TLS 握手开销

## v2.0.0 更新内容

- 双级概率系统：触发概率和 LLM 生图概率独立控制
- LLM 生图可单独开关
- 先发后生：发图不再被生图阻塞
- `/搜图 N` 手动搜图命令
- 搜图上限提升至 50
- 搜图筛选提示词：可配置 LLM 内容过滤条件，不满足条件的图片不入库
- 情绪解析 4 层回退策略，兼容各种 LLM 输出
- 强化 Gemini 情绪分析 prompt 和参数
- 已下载图片 ID 从磁盘恢复，重启不丢失
- 全链路 info 级别日志，方便排查

## 常见问题

**图片不发送？**
- 检查日志中 `[MemeMemPlus]` 相关信息，定位卡在哪一步
- 确认触发概率不是 0，冷却时间不过长
- 确认对应心情目录下有图片（空目录不发送）
- 情绪分析提供商的 API Key 是否正确

**情绪检测不准？**
- 换一个更强的 LLM 作为情绪分析提供商
- 调整 `custom_mood_prompt` 自定义提示词

**Grok 报 413 错误？**
- 插件已内置图片压缩（800px, JPEG q80），通常不会出现
- 如仍出现，减少参考图尺寸

**可以和 meme_manager 共存吗？**
- 可以，两个插件独立运行，互不影响

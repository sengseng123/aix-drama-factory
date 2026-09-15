# AIX爱客 MiniMax H3 短剧工厂

AIX 短剧工厂是面向本地 AI 视频生产的 Web 工作台，把故事/剧本、角色与场景资产、MiniMax H3 提示词、ComfyUI 生成、断点恢复和成片整理放在同一套流程中。

> **项目来源与致谢**
>
> 本项目在 B 站 UP 主 **盖伦TA哥** 开发的“纯本地全自动短剧工厂”基础上继续开发与扩展。感谢原作者提供的项目基础与创作思路，原项目介绍视频：[minimax-h3全自动纯本地短剧工厂](https://www.bilibili.com/video/BV1SpbX6uEoS/)。

**当前版本：`v2.0.0-dev.12`（开发预览）**

本开源仓库从 AIX V2 开始建立独立历史，不包含私人客户项目的源码、分支、标签、发布记录或客户资料。

## v2.0.0-dev.12 重点

- 设置页可扫描并选择本地 GGUF 大语言模型、ComfyUI 视频 UNET，以及生图工作流适配器支持的模型。
- H3 的“文生/首帧工作流”和“多参考工作流”是两个独立用途槽位，共用完整候选列表；允许同模型复用、交叉选择和社区命名模型。
- 显式选择的 H3 模型会精确注入对应工作流；文件缺失时明确报错，不静默替换为其他模型。
- 本地文本 GGUF 可不带 `mmproj`；配置了有效 `mmproj` 时继续自动加载。
- 生图模型按 `workflows/t2i_*.json` 适配器分组，当前内置 Qwen Image 2512，避免跨架构只替换模型文件名。
- 页面顶部显示 `VERSION` 中的实际安装版本。
- 修复页面状态轮询与失败片段重试竞争项目锁时，偶发误报“已有生成任务”的问题。

同时包含 `v2.0.0-dev.11` 已发布的低显存、安全停止与断点恢复改进：

- 三套 H3 工作流移除 TE-Speed 层缓存，保留 H3 SageAttention，并加入低显存注意力与前馈分块。
- 正式制作、资产、失败片段重试、大纲、剧本和提示词生成均可按项目安全停止，并保留已完成结果。
- 视频片段生成失败后可单独重新生成，不必重做已经成功的片段。
- 生成状态可在页面刷新或服务重启后恢复，并防止同一项目重复启动生成任务。
- 片段生成成功但最终合成失败时，保留成功片段并允许从断点继续。

- 通用 V2 分阶段生产：故事、大纲、剧本、资产、H3 提示词、图片/视频和成片整理。
- 同时支持 `managed` 托管模式和 `external` 外部服务模式；外部模式不会启停用户已有的 ComfyUI/Qwen。
- Windows/Linux 跨平台模型、LoRA、VAE 和文本编码器路径精确匹配。
- H3 帧数对齐、队列安全的 OOM 释放与单次受控重试。
- 提示词批处理保留已成功片段，后续只重试失败或无效片段。
- 纯动作/无台词标记与真实台词分离，源台词确定性放入 Storyboard 且只出现一次。
- 浏览器通过后端 `/api/comfy/queue` 代理读取队列，不再直连写死的 ComfyUI 端口。
- 设置页可扫描并选择本地 GGUF 大语言模型，以及 ComfyUI 已安装的 H3 FL2VA、REF2VA/Remix 视频模型。
- H3 的两个选择框代表“文生/首帧工作流”和“多参考工作流”用途槽位，共用同一份完整 UNET 候选；允许同模型复用、交叉选择和 Feihou、Dsiwa 等社区命名模型，兼容性由实际运行结果确认。
- 生图模型按 `workflows/t2i_*.json` 工作流适配器分组选择；不同架构必须配套正确的文本编码器、VAE 和采样图，不能仅把 Qwen 模型文件名替换为 Flux、Z-Image 或 Krea。

完整变更见 [CHANGELOG.md](CHANGELOG.md)。

## 仓库边界

本仓库只包含可版本化、可审查的项目层：

- Python/Flask 后端和 Web 前端；
- ComfyUI 工作流 JSON；
- 启动、停止与配置示例；
- 测试、发布模板和开发文档。

不进入 Git 或源码发布包的内容：

- ComfyUI/Python/LLM/FFmpeg 运行环境与模型权重；
- `config.json`、`.env`、API Key、私钥和本机绝对路径；
- 用户项目、素材、生成图片/视频、日志、缓存和备份；
- 个人收款/赞赏二维码以及其他个人识别信息；
- ZIP/7z/RAR/TAR 发布产物；这些仅作为 GitHub Release 附件或通过其他发布渠道提供。

## 本地运行

1. 准备可用的 ComfyUI、MiniMax H3 模型、本地 LLM（可选）和 FFmpeg。
2. 安装 Web 依赖：`pip install -r requirements-web.txt`。
3. 复制 `config.example.json` 为 `config.json`，只在本机文件中填写服务地址与凭据。
4. 如需扩展模型目录，复制 `extra_model_paths.example.yaml` 为 `extra_model_paths.yaml`。
5. Windows 上双击 `start-web.bat`，或运行 `python app.py`，然后打开 `http://127.0.0.1:7861`。

正式 Web 默认使用 7861。需要与正式实例并行开发时请运行 `start-dev.bat`，开发 Web 固定使用本机 7862；端口已被占用时启动器会阻止启动，避免误连其他实例。

只使用完整 H3 提示词入口时可不安装 Qwen 文案模型；自动故事、剧本和提示词功能需要 OpenAI 兼容 LLM。更详细的开发目录规则见 [DEV-README.md](DEV-README.md)，三模块发布方式见 [DISTRIBUTION.md](DISTRIBUTION.md)。

## 从 dev.9 / dev.10 / dev.11 更新

GitHub Release 附件 `AIX-DramaFactory-V2-2.0.0-dev.12-update.zip` 面向已安装 `v2.0.0-dev.9`、`v2.0.0-dev.10` 或 `v2.0.0-dev.11` 的 V2 Web Windows 用户，不包含模型、ComfyUI、Qwen、项目、素材或成片。下载后先用同名 `.sha256.txt` 校验，关闭 AIX Web 后再按包内说明更新。

## 视频参考创作（一期）

首页「视频参考创作」支持导入视频、通过独立本地或云端视觉服务分析镜头、编辑目标故事与角色，再导入现有制作流程。配置、停止重试、时长映射和当前限制见 [视频参考说明](VIDEO-REFERENCE.md)。动作驱动属于后续阶段。

## 参与贡献

AIX 短剧工厂欢迎社区共同开发。当前项目处于快速开发阶段，当前开发预览版为 `v2.0.0-dev.12`。

```text
Fork / Clone
↓
创建 Branch
↓
开发
↓
Commit
↓
Push
↓
Pull Request
```

请不要直接在 `main` 开发。功能认领、分支命名、Commit、测试与 PR 要求详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 安全

- 默认只监听本机；不要将 ComfyUI、Qwen 或开发 Flask 服务器直接暴露到公网。
- 提交前运行 `git status` 并检查大文件、凭据、私有地址和用户生成内容。
- 漏洞报告方式见 [SECURITY.md](SECURITY.md)。

## 许可证

本项目自 `v2.0.0-dev.9` 起以 **GNU Affero General Public License v3.0 only**（`AGPL-3.0-only`）授权，详见 [LICENSE](LICENSE)。修改版通过网络向用户提供服务时，需按 AGPLv3 第 13 条向这些用户提供对应源代码。

仓库中保留的第三方组件不改变其各自许可证，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。模型、ComfyUI、自定义节点与独立 Runtime 包不由本许可声明自动重新授权，发布前必须分别核对来源与再分发条款。

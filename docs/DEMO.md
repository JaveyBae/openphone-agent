# 演示视频录制（1–2 分钟）

题目要求视频同时证明三件事：**用户全程在操作手机**、**同一时间能看到 Agent 的实际执行画面**、**最终结果**。

## 准备（约 5 分钟）

1. 模拟器开机，确认后台屏和模型都就绪：
   ```bash
   python3 scripts/workspace_runtime.py start
   curl -s http://127.0.0.1:18086/v1/models     # 模型可用（Qwen3.8-27B）
   python3 scripts/chat_bridge.py --provider qwen --base-url http://127.0.0.1:18086/v1 \
     --model qwen3.8-27b --max-steps 40
   ```
2. 清理上次留下的测试数据（例如名为 `Test OpenPhone` 的联系人），确认主屏上**没有**打开通讯录。
3. 在 Mac 上打开并排查看窗口：`python3 scripts/display_viewer.py --both --always-on-top`
   左边是主屏（你在用），右边是后台独立屏（Agent 在用），都是只读预览。
4. 另开一个终端运行真人并发观察：`python3 scripts/owner_observer.py`
   它只记录主屏和后台屏的焦点信息（每秒两次），不碰你的屏幕，任务结束后输出报告。
5. 用 macOS 自带录屏（⇧⌘5 → 录制所选部分）框住：Android Studio 里的模拟器窗口 + 并排查看窗口。

## 录制脚本（建议 90 秒）

| 时间 | 主屏（你） | 后台屏（Agent） |
|---|---|---|
| 0–10 s | 在 OpenPhone 输入：“在通讯录里新建一个联系人：名字 Test，姓氏 OpenPhone，电话 +41 44 555 02 46，邮箱 openphone.demo2@example.com，公司 Whale Demo，然后保存”，确认底部是「⚡ 经验模式」，发送 | 开始清理并打开通讯录 |
| 10–70 s | **切去别的 App 持续操作**：在 Chrome 里滚动网页、在搜索框打字（体现键盘没被抢） | 依次填表、滚动、保存（右侧实时画面可见） |
| 70–85 s | 回到 OpenPhone，展开“执行过程”：能看到哪些步骤“按经验直接执行”、哪些由模型决定 | 停在保存后的联系人页 |
| 85–90 s | 结果消息里的“已在应用里核对到：……”；（可选）打开通讯录展示这个联系人 | — |

可选加分镜头（另录 20–30 秒）：
- **接管**：发一个会停下的任务（例如定闹钟时说“7 点”，Agent 会问早上还是晚上；或网页 Cookie 页），在 App 里点「接管到主屏」。
- **对比**：同一任务切到「🧠 纯 Agent」再跑一次，时长和模型调用次数的差别。

## 录完之后

- `owner_observer` 的报告（终端输出）截图放进 README 或答辩材料：主屏焦点全程在主屏、Agent 的 App 从未出现在主屏。
- 每次运行的完整证据在 `runs/<时间>/`：每步截图、模型原始输出、耗时、核对结果（`run.json` 里的 `verification` 和 `experience`）。这些含个人数据，只用于本地答辩展示，不提交仓库。

# Rebase 進度記錄 — 本地 fork vs upstream

Last updated: **2026-04-19**

---

## 當前狀態

| 項目 | 狀態 |
|------|------|
| 上游跟進 | ✅ 完全同步 → `origin/main` @ `bf5d7462b` |
| 本地功能 | ✅ 全部 20 commits 保留，無丟失 |
| 解決衝突 | ✅ 全部解決 |
| 健康檢查 | ✅ 所有模塊導入成功，slash commands 註冊正常 |

---

## 本地額外功能（20 commits）

按 commit 順序：

1. `eb132cad` **feat(gateway): channel personality binding via lightweight hook system**
   - 新增 `gateway/extensions/` 鉤子擴展框架
   - 實現 `channel_binding.py` — 通道綁定人格/模型/技能/記憶作用域
   - 修改少量核心文件添加鉤子調用點

2. `0cb41e7e` **fix: critical memory scope bugs - wrong session key + lost global merge**
   - 修復記憶作用域關鍵bug：session key錯誤 + global 合併丟失

3. `5c4d0810` **feat: MemPalace memory provider plugin**
   - 新增 `plugins/memory/mempalace/` 完整插件
   - 實現結構化長期記憶，支持 Wing/Room/Drawer 層級
   - 集成 Ollama bge-m3 雙語嵌入

4. `d73c5195` **feat(mempalace): use Ollama bge-m3 for bilingual zh+en embeddings**
   - 切換到 Ollama 本地嵌入，更好支持中英雙語

5. `f7ea0ce2b` **fix: code review critical+high issues**
   - 修復代碼review提出的關鍵問題：線程安全、路徑遍歷防護、去重

6. `79ea635b5` **fix: round 2 review — 8 additional issues**
   - 第二輪修復

7. `ccddae754` **feat: Hall extension — inter-soul messaging board**
   - 新增 `gateway/extensions/hall.py` 跨靈魂消息板
   - 新增 `tools/hall_tool.py` 工具包裝

8. `a7c3d9d97` **fix: MemPalace scope resolution for Discord threads + memory manager init timing**
   - 修復 Discord thread 作用域解析 + 初始化時機

9. `f59a6dea4` **fix: thread chat_id parent resolution + auto-dedup + mempalace_delete tool**
   - 修復 thread 父ID解析 + 自動去重 + 添加 `mempalace_delete` 工具

10. `2e84eea4b` **feat: auto-mine personal facts from conversation turns**
    - 自動從對話挖掘個人信息，背景線程保存

11. `0cfe81a1d` **feat: semantic gate for auto-mine facts with cold start bootstrap**
    - 語義門過濾不重要事實，冷啓動對比soul內容

12. `9d917ae3a` **chore: remove debug logs, unrelated Playwright docs, MemDebug logging**
    - 清理日誌，刪除不小心進來的第三方文檔

13. `36189ea2a` **fix: thread safety for channel_binding + hall.py JSONL + cache MemoryStore global entries**
    - 綫程安全修復

14. `7162ea1e2` **refactor: extract /bind + inject_cross_channel from run.py into extensions**
    - 重構抽出，核心更乾淨

15. `246f959de` **feat: enhanced /bind command — save/list/unbind + cross-platform support (Telegram/WhatsApp)**
    - `/bind` 增強：保存/列錶/解除綁定

16. `e27387666` **feat: admin guard, cron binding inheritance, soul frontmatter, isolation mode**
    - 管理員命令保護
    - cron 任務繼承通道绑定
    - soul 文件支持 YAML frontmatter
    - 隔離模式 `-` 不讀取 global

17. `e3eca2d94` **revert: grok-2-vision change — not our concern, avoid upstream merge conflict**
    - 還原上游修改避免未來衝突

18. `87ae19c95` **feat: Hall communication commands + cron memory inheritance**
    - 添加 `/hall-send`, `/hall-read`, `/hall-status`, `/hall-report` slash commands
    - cron 記憶作用域繼承

19. `f900b596b` **feat: Hall auto-dispatch with pending file + gateway watcher**
    - Hall 自動調度：admin 發送立即喚醒目標靈魂
    - 子進程掛靠機制，保證消息不丟失

20. `7ebaff54a` **feat: add /reload hot-reload command (config, souls, extensions, env)**
    - 熱重載命令，不用重啓 gateway 更新配置

---

## 衝突解決記錄（本次 rebase）

| 文件 | 衝突原因 | 解決方案 |
|------|----------|----------|
| `gateway/platforms/discord.py` | 你加了 `extra['channel_binding']`，上游保留了原 `channel_prompt` | **保留雙方** — `channel_prompt` 保留 + `extra` 添加 |
| `gateway/run.py` | 你加了 `fire_hooks_first("get_ephemeral")`，上游保留了原 `event_channel_prompt` | **保留雙方** — 原有 channel_prompt 保留 + hook 添加 |
| `run_agent.py` | 你加了 `memory_scope` 參數，上游也改了附近代碼 | **合併** — 你的參數傳遞保留 |
| `agent/model_metadata.py` | 你加了新型號上下文，上游也加新型號 | **合併** — 雙方新型號都保留 |
| `gateway/run.py` (第二次) | 你添加了 admin guard / hall auto-dispatch 代碼，上游也改 | **合併** — 雙方代碼保留 |
| `run_agent.py` (第二次) | 你加了 MemPalace 初始化，上游也改了流式 | **合併** — 雙方都保留 |

---

## 未來 Merge 回上游建議順序

從最容易到最難：

1. bug fixes + 新型號 context 補全 → 肯定過
2. Admin Guard + Channel Directory → 小改動，改善可用性
3. Cron binding 繼承 + Cross-channel dispatch 抽像 → 架構改進
4. `/reload` 熱重載 → 核心小改動，功能性強
5. extension hook 系統 + gateway/extensions 框架 → 基礎設施
6. channel-binding extension → 依賴鉤子系統
7. hall extension → 依賴 channel-binding 和 cross-channel
8. mempalace memory plugin → 完全獨立插件，用戶可選

---

## 保持同步方法

未來繼續跟進上游：

```bash
git fetch origin
git rebase origin/main
# 如果有衝突，參考這個文件解決，同樣策略
# 解完跑健康檢查：
python -c "import run_agent; import gateway.run; from gateway.extensions.channel_binding import _on_new_session; from gateway.extensions.hall import hall_send; import plugins.memory.mempalace; from hermes_cli.commands import COMMAND_REGISTRY; print('OK')"
```

如果健康檢查輸出 `OK` 就是成功。
# Bug Fix: Artifacts Not Showing After Session Resume

## 问题描述

当用户 resume 一个已经产出 artifacts 的 session 后，切换到 `/outputs` 标签页时看不到任何产出内容，即使该 session 在之前运行时确实生成了表格、图表等 artifacts。

## 根本原因

Session resume 流程中缺少 artifacts 的恢复逻辑：

1. **后端返回的数据可能包含 artifacts**，但前端的类型定义和恢复逻辑没有处理它
2. **SessionConversation 类型**没有 `artifacts` 字段
3. **restoreSessionConversation 函数**只恢复了 messages 和 toolCalls，忽略了 artifacts
4. **StateStore.restoreSession 方法**没有接受和设置 artifacts

结果：resume session 后，`state.artifacts` 始终是空数组 `[]`，导致 OutputsView 没有内容显示。

## 修复方案

### 1. 更新 SessionConversation 类型

**文件**: `apps/tui/src/config/config-client.ts`

```typescript
const SessionConversationSchema = z.object({
  sessionId: z.string(),
  title: z.string().optional(),
  titleSource: z.string().optional(),
  updatedAt: z.string().optional(),
  messages: z.array(ConversationMessageSchema),
  summary: ConversationSummarySchema.optional(),
  runEventRefs: z.array(ConversationRunEventRefSchema),
  checkpoints: z.array(ConversationCheckpointSchema).optional(),
  toolCalls: z.array(ConversationToolCallSchema),
  artifacts: z.array(z.any()).optional(), // ✅ 添加 artifacts 字段
});
```

### 2. 更新 RestoredSessionConversation 类型

**文件**: `apps/tui/src/state/session-restore.ts`

```typescript
export type RestoredSessionConversation = {
  threadId: string;
  title?: string | undefined;
  messages: DisplayMessage[];
  toolCalls: LiveToolCallRecord[];
  artifacts: any[]; // ✅ 添加 artifacts 字段
};
```

### 3. 更新 restoreSessionConversation 函数

**文件**: `apps/tui/src/state/session-restore.ts`

```typescript
export function restoreSessionConversation(
  dto: SessionConversation,
): RestoredSessionConversation {
  return {
    threadId: dto.sessionId,
    ...(dto.title ? { title: dto.title } : {}),
    messages: conversationToDisplayMessages(dto),
    toolCalls: conversationToToolCalls(dto.toolCalls),
    artifacts: dto.artifacts || [], // ✅ 从 session 数据中恢复 artifacts
  };
}
```

### 4. 更新 StateStore.restoreSession 方法

**文件**: `apps/tui/src/state/store.ts`

```typescript
restoreSession(input: {
  threadId: string;
  messages: DisplayMessage[];
  toolCalls?: LiveToolCallRecord[] | undefined;
  artifacts?: any[] | undefined; // ✅ 接受 artifacts 参数
}): void {
  const initial = createInitialTuiState();
  this.setState({
    ...this.state,
    plan: initial.plan,
    events: initial.events,
    artifacts: input.artifacts ?? initial.artifacts, // ✅ 恢复 artifacts
    audits: initial.audits,
    runStatus: initial.runStatus,
    runId: undefined,
    agentResponseComplete: undefined,
    errorMessage: undefined,
    toolCalls: input.toolCalls ?? [],
    runStartedAt: undefined,
    runFinishedAt: undefined,
    tokenUsage: undefined,
    messages: input.messages,
    sessionStats: createInitialSessionUsage(),
    threadId: input.threadId,
  }, true);
}
```

## 数据流

### Resume Session 完整流程（修复后）

```
1. 用户操作
   └─> 输入 `/resume <session-id>` 或从 Session Picker 选择

2. App.tsx: restoreHistoricalSession()
   └─> configClient.getSessionConversation(sessionId)
       返回: {
         sessionId, messages, toolCalls, artifacts, ...
       }

3. session-restore.ts: restoreSessionConversation(conversation)
   └─> 解析并返回: {
         threadId, messages, toolCalls,
         artifacts: conversation.artifacts || [] ✅
       }

4. store.ts: store.restoreSession(restored)
   └─> setState({
         ...state,
         messages: restored.messages,
         toolCalls: restored.toolCalls,
         artifacts: restored.artifacts ✅
       })

5. UI 更新
   └─> OutputsView 接收到 state.artifacts
       └─> 显示历史产出的表格、图表等
```

## 验证方法

### 测试步骤

1. **创建一个包含产出的 session**
   ```bash
   # 启动 TUI
   npm run start
   
   # 执行生成数据的查询
   > 查询用户数据表，返回前10条记录
   
   # 切换到 Outputs 标签查看产出
   # 应该能看到表格数据
   ```

2. **Resume 该 session**
   ```bash
   # 退出并重新启动，或新开一个 TUI
   npm run start
   
   # Resume session
   > /resume <session-id>
   ```

3. **验证 artifacts 恢复**
   ```bash
   # 切换到 Outputs 标签
   # ✅ 应该能看到之前生成的表格数据
   # ❌ 修复前：显示 "暂无产出"
   ```

### 预期结果

- **修复前**: OutputsView 显示 "暂无产出。Agent 生成 SQL 结果、图表、报告或文件后会显示在这里。"
- **修复后**: OutputsView 正确显示所有历史 artifacts，包括表格、图表等

## 后端兼容性

### 后端需要支持

后端 API `/api/sessions/{sessionId}/conversation` 应该在返回的数据中包含 `artifacts` 字段：

```typescript
{
  sessionId: "session-123",
  messages: [...],
  toolCalls: [...],
  artifacts: [  // ✅ 需要返回这个字段
    {
      id: "artifact-1",
      title: "用户数据查询结果",
      kind: "csv",
      type: "dataset",
      summary: "查询返回 10 行数据",
      detail: {
        type: "dataset",
        columns: ["id", "name", "email"],
        rows: [["1", "Alice", "alice@example.com"], ...]
      },
      ...
    }
  ]
}
```

### 降级处理

如果后端暂时不返回 `artifacts` 字段：
- `dto.artifacts` 为 `undefined`
- `dto.artifacts || []` 返回空数组
- UI 显示 "暂无产出"（与修复前一致）
- **不会报错**，保持向后兼容

## 影响范围

### 修改的文件
1. `apps/tui/src/config/config-client.ts` - 添加 artifacts 字段到 schema
2. `apps/tui/src/state/session-restore.ts` - 添加 artifacts 恢复逻辑
3. `apps/tui/src/state/store.ts` - 更新 restoreSession 接受 artifacts

### 不影响的功能
- ✅ 新 session 的 artifact 生成和显示（通过实时事件）
- ✅ 当前 session 的 `/outputs` 显示
- ✅ 其他 resume 功能（messages、toolCalls）
- ✅ Demo 模式

### 改进的功能
- ✅ Resume session 后能看到历史 artifacts
- ✅ 更完整的 session 状态恢复
- ✅ 更好的用户体验

## 额外改进建议

### 未来可以考虑的增强

1. **Events 恢复**
   - 目前 events 也没有被恢复
   - 可以添加 events 字段到 SessionConversation
   - 恢复 timeline events 以显示完整的执行历史

2. **增量同步**
   - 如果 session 在其他地方继续运行
   - 可以定期同步最新的 artifacts
   - 避免 resume 后看到的是旧数据

3. **Artifacts 持久化优化**
   - 大型 artifacts（如大表格）可以延迟加载
   - 只在用户查看 `/outputs` 时才获取详细数据
   - 减少 resume 时的数据传输量

## 总结

这个修复确保了 session resume 功能的完整性，用户现在可以在 resume session 后查看历史产出的数据。修改最小化且向后兼容，即使后端暂时不返回 artifacts 字段也不会报错。

**关键改动**: 添加 artifacts 字段到整个 session restore 数据流中，从类型定义、恢复函数到状态管理都支持 artifacts 的恢复。

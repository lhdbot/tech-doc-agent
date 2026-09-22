# 核心逻辑伪代码规范

## 格式
- 伪代码统一使用 ```` ```pseudo ```` 代码块。
- 采用类 Pascal 风格：`PROCEDURE`/`END PROCEDURE`、`IF ... THEN ... ELSE ... END IF`、`FOR ... DO ... END FOR`、`RETURN`。
- 变量和函数名用 snake_case，中文注释说明意图。

## 必备要素
每段伪代码必须包含：
1. **输入**：开头的注释中列出输入参数及含义。
2. **输出**：开头的注释中列出返回值及含义。
3. **关键步骤注释**：核心分支、边界处理、异常路径都要有行内注释。

## 示例骨架

```pseudo
// 输入：user_id（用户标识），items（待写入条目列表）
// 输出：write_result（成功条数与失败明细）
PROCEDURE batch_write(user_id, items):
    shard <- route_to_shard(user_id)          // 按分片键路由
    successes <- 0
    failures <- []
    FOR EACH item IN items DO
        IF NOT validate(item) THEN            // 先校验，快速失败
            failures.append(item, "校验失败")
            CONTINUE
        END IF
        result <- shard.write(item)           // 单条写入目标分片
        IF result.ok THEN
            successes <- successes + 1
        ELSE
            failures.append(item, result.error)
        END IF
    END FOR
    RETURN {successes, failures}
END PROCEDURE
```

## 数量要求
- 详细设计类章节（详细设计、核心流程、关键算法）必须至少包含 1 段伪代码，表达本章最核心的处理逻辑。
- 伪代码之后配一段文字，说明时间复杂度、失败路径和重试策略。

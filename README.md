# tech-doc-agent：技术文档多 Agent 写作系统

输入任意主题，系统自动走完 **需求澄清 → 大纲确认 → 多 agent 并行撰写各章节 → 合并审校交付** 的完整流水线，产出一份结构化、可落地的技术文档。模型厂商可自由配置，任何 OpenAI 兼容 API 都能接入。

## 安装

```bash
cd tech-doc-agent
pip install -r requirements.txt   # 仅依赖 openai SDK
cp config.example.json config.json
```

## 配置模型

编辑 `config.json`，每个 profile 是一个模型厂商。api_key 支持 `${环境变量}` 引用：

```json
{
  "default": "deepseek",
  "profiles": {
    "deepseek": { "base_url": "https://api.deepseek.com", "api_key": "${DEEPSEEK_API_KEY}", "model": "deepseek-chat" }
  }
}
```

已预置 deepseek / kimi / qwen / openai / local(Ollama) 五个示例，增删改随意。然后设置对应环境变量，例如：

```bash
export DEEPSEEK_API_KEY=sk-xxx
```

## 使用

```bash
# 交互模式（推荐）：系统会先问你几个澄清问题，再出大纲让你确认/提修改意见，确认后并行写作
python main.py "Kubernetes 集群高可用方案"

# 指定模型
python main.py "RAG 检索增强方案设计" --profile kimi

# 编排和写作用不同模型（大纲/审校用强模型，章节写作用便宜模型）
python main.py "..." --orchestrator-profile kimi --writer-profile deepseek

# 非交互模式：跳过提问和确认，一键生成到底（适合脚本调用）
python main.py "..." --auto

# 调整并行度 / 输出目录
python main.py "..." --workers 8 --output my_docs
```

## 产出

```
docs/<主题>-<日期>/
├── README.md        # 最终合并文档：摘要 + 目录 + 全部章节 + 结论与风险
├── 01-xxx.md        # 各章节独立文件（写作 agent 直接产出）
├── 02-xxx.md
└── outline.json     # 大纲存档
```

## 流水线说明

| 阶段 | 执行者 | 行为 |
|---|---|---|
| 1. 需求澄清 | orchestrator | 生成 2-4 个关键问题，交互收集回答 |
| 2. 大纲 | orchestrator | 产出 4-8 章大纲，用户确认或提意见迭代 |
| 3. 章节撰写 | writer × N | 每章一个 agent 并行撰写，写文件 + 返回摘要/待验证项 |
| 4. 合并审校 | orchestrator | 基于各章摘要生成全文摘要与"结论与风险"，合并为 README.md |

章节写作失败不会拖垮整体：失败章节在最终文档中显式标注，可重跑或手动补写。

## 环境要求

Python 3.8+。

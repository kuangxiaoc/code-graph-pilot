# ✈️ CodeGraph Pilot - 基于知识图谱的代码智能助手

![Python](https://img.shields.io/badge/Python-3.11-blue)
![Neo4j](https://img.shields.io/badge/Neo4j-GraphDB-green)
![LangGraph](https://img.shields.io/badge/LangGraph-Agent-orange)
![Qwen](https://img.shields.io/badge/Model-Qwen_Plus-violet)

> **"不再盲人摸象"** —— 结合 AST 静态分析与 LLM 语义理解，为复杂代码重构提供上帝视角。

## 📖 项目背景
传统 RAG (检索增强生成) 在处理代码任务时，往往只能找到语义相似的代码片段，却丢失了**结构化依赖**（如：修改 A 函数会影响 B 和 C）。本项目通过构建 **Code Knowledge Graph**，实现了精准的 **2-hop 影响范围分析**。

## 🚀 核心功能
- **🕷️ 批量全库建图**：支持上传多文件/文件夹，基于 Python AST 解析 Class/Function/Method 及调用链。
- **🕸️ 交互式图谱**：在 Streamlit 中实时渲染代码依赖拓扑，支持物理引擎拖拽，清晰展示“谁调用了谁”。
- **🤖 循环 Agent 工作流**：基于 LangGraph 构建 `Retrieve -> Generate -> Review -> Refine` 闭环，自动修正幻觉。
- **🇨🇳 中文深度分析**：集成 Qwen-Plus 模型，提供中文的风险评估与重构建议。

## 🛠️ 技术架构
1.  **解析层**: `Python ast` (提取实体与关系)
2.  **存储层**: `Neo4j` (存储图谱结构)
3.  **推理层**: `Qwen-Plus` (逻辑分析) + `LangGraph` (状态机编排)
4.  **交互层**: `Streamlit` + `Streamlit-Agraph` (前端可视化)

## 📸 效果演示


## ⚡️ 快速开始

### 1. 环境准备
使用 uv 进行极速安装：
```bash
git clone https://github.com/yourname/code-graph-pilot.git
cd code-graph-pilot
uv sync

### 2. 启动数据库
```bash
docker-compose up -d

### 3. 运行
```bash
uv run streamlit run src/app.py

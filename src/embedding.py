"""
向量嵌入和语义搜索模块

使用 OpenAI 兼容 API 调用阿里百炼 embedding 模型，FAISS 进行相似度搜索
"""

import os
from typing import List, Optional
from dotenv import load_dotenv

# 可选依赖：numpy
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    np = None

# 可选依赖：FAISS 向量搜索
try:
    import faiss
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False
    faiss = None

# 可选依赖：OpenAI（兼容阿里百炼）
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    OpenAI = None

# 加载环境变量
load_dotenv()


class EmbeddingStore:
    """向量嵌入存储和语义搜索"""
    
    def __init__(self, embedding_dim: int = 1024):
        """
        初始化向量存储
        
        Args:
            embedding_dim: 向量维度（text-embedding-v3 默认 1024）
        """
        self.embedding_dim = embedding_dim
        self.metadata = []  # 存储对应的元数据
        self._available = HAS_FAISS and HAS_NUMPY and HAS_OPENAI
        
        if self._available:
            self.index = faiss.IndexFlatIP(embedding_dim)  # 内积相似度
            
            # 使用 OpenAI 兼容 API（复用 Qwen 配置）
            self._client = OpenAI(
                api_key=os.getenv("QWEN_API_KEY"),
                base_url=os.getenv("QWEN_BASE_URL")
            )
        else:
            self.index = None
            self._client = None
        
        if not HAS_OPENAI:
            print("⚠️ Warning: openai 未安装，语义搜索功能不可用")
        if not HAS_FAISS:
            print("⚠️ Warning: faiss 未安装，语义搜索功能不可用")
        if not os.getenv("QWEN_API_KEY"):
            print("⚠️ Warning: QWEN_API_KEY 未设置")
    
    def get_embedding(self, text: str) -> Optional['np.ndarray']:
        """
        调用 embedding API 获取文本向量
        
        Args:
            text: 待嵌入的文本
        
        Returns:
            向量数组，失败返回 None
        """
        if not self._available or self._client is None:
            return None
        
        try:
            response = self._client.embeddings.create(
                model="text-embedding-v3",
                input=text,
                dimensions=self.embedding_dim
            )
            
            embedding = response.data[0].embedding
            if HAS_NUMPY:
                return np.array(embedding, dtype=np.float32)
            return embedding
                
        except Exception as e:
            print(f"⚠️ Embedding error: {e}")
            return None
    
    def add_function(self, name: str, docstring: str, source: str, 
                     filepath: str = "", lineno: int = 0) -> bool:
        """
        添加函数到向量索引
        
        Args:
            name: 函数名
            docstring: 函数文档字符串
            source: 函数源码
            filepath: 文件路径
            lineno: 行号
        
        Returns:
            是否成功添加
        """
        # 检查依赖是否可用
        if not self._available:
            # 仍然保存元数据，但跳过向量索引
            self.metadata.append({
                "name": name,
                "docstring": docstring,
                "source": source,
                "filepath": filepath,
                "lineno": lineno
            })
            return False
        
        # 构建嵌入文本：函数名 + docstring + 简短源码片段
        text = f"{name}\n{docstring}\n{source[:500]}"  # 限制源码长度
        
        embedding = self.get_embedding(text)
        if embedding is None:
            return False
        
        # 归一化向量（用于内积相似度）
        if HAS_NUMPY:
            faiss.normalize_L2(embedding.reshape(1, -1))
        
        # 添加到索引
        self.index.add(embedding.reshape(1, -1))
        
        # 保存元数据
        self.metadata.append({
            "name": name,
            "docstring": docstring,
            "source": source,
            "filepath": filepath,
            "lineno": lineno
        })
        
        return True
    
    def add_entities(self, entities: List[dict]) -> int:
        """
        批量添加实体到向量索引
        
        Args:
            entities: 实体列表，每个实体包含 name, docstring, source, filepath, lineno
        
        Returns:
            成功添加的数量
        """
        success_count = 0
        for entity in entities:
            # 只处理函数和方法
            if entity.get("type") in ("Function", "Method"):
                if self.add_function(
                    name=entity.get("name", ""),
                    docstring=entity.get("docstring", ""),
                    source=entity.get("source", ""),
                    filepath=entity.get("filepath", ""),
                    lineno=entity.get("lineno", 0)
                ):
                    success_count += 1
        
        return success_count
    
    def search(self, query: str, top_k: int = 5) -> List[dict]:
        """
        语义搜索：根据自然语言查询最相似的函数
        
        Args:
            query: 自然语言查询
            top_k: 返回结果数量
        
        Returns:
            匹配结果列表，每个结果包含 metadata 和 similarity score
        """
        # 依赖不可用时返回空列表
        if not self._available or self.index is None:
            return []
        
        if self.index.ntotal == 0:
            return []
        
        # 获取查询向量
        query_embedding = self.get_embedding(query)
        if query_embedding is None:
            return []
        
        # 归一化
        if HAS_NUMPY:
            faiss.normalize_L2(query_embedding.reshape(1, -1))
        
        # 搜索
        top_k = min(top_k, self.index.ntotal)
        scores, indices = self.index.search(query_embedding.reshape(1, -1), top_k)
        
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx >= 0:  # FAISS 返回 -1 表示无效结果
                result = self.metadata[idx].copy()
                result["similarity"] = float(score)
                results.append(result)
        
        return results
    
    def clear(self):
        """清空向量索引"""
        if self._available:
            self.index = faiss.IndexFlatIP(self.embedding_dim)
        self.metadata = []
    
    def __len__(self):
        if self._available and self.index is not None:
            return self.index.ntotal
        return len(self.metadata)


# 全局单例
_embedding_store: Optional[EmbeddingStore] = None


def get_embedding_store() -> EmbeddingStore:
    """获取全局向量存储实例"""
    global _embedding_store
    if _embedding_store is None:
        _embedding_store = EmbeddingStore()
    return _embedding_store


def semantic_search(query: str, top_k: int = 5) -> List[dict]:
    """
    语义搜索便捷函数
    
    Args:
        query: 自然语言查询
        top_k: 返回结果数量
    
    Returns:
        匹配结果列表
    """
    store = get_embedding_store()
    return store.search(query, top_k)


def build_embedding_index(entities: List[dict]) -> int:
    """
    从实体列表构建向量索引
    
    Args:
        entities: 实体列表
    
    Returns:
        成功索引的函数数量
    """
    store = get_embedding_store()
    store.clear()
    return store.add_entities(entities)
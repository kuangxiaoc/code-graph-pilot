import ast
import os
from neo4j import GraphDatabase

class CodeAnalyzer(ast.NodeVisitor):
    def __init__(self, filepath):
        self.filepath = filepath
        self.entities = []
        self.relations = []
        self.current_class = None     # 追踪当前类
        self.current_function = None  # 追踪当前函数

    def visit_ClassDef(self, node):
        class_name = node.name
        # 1. 记录类节点
        self.entities.append({
            "type": "Class",
            "name": class_name,
            "filepath": self.filepath,
            "lineno": node.lineno
        })
        
        # 2. 处理继承关系 (INHERITS)
        for base in node.bases:
            if isinstance(base, ast.Name):
                self.relations.append({
                    "src": class_name,
                    "rel": "INHERITS",
                    "dst": base.id
                })

        # 进入类作用域
        prev_class = self.current_class
        self.current_class = class_name
        self.generic_visit(node)
        self.current_class = prev_class

    def visit_FunctionDef(self, node):
        func_name = node.name
        # 如果在类里面，名字改成 "ClassName.method_name"
        full_name = f"{self.current_class}.{func_name}" if self.current_class else func_name
        
        node_type = "Method" if self.current_class else "Function"
        
        self.entities.append({
            "type": node_type,
            "name": full_name,
            "filepath": self.filepath,
            "lineno": node.lineno
        })
        
        # 如果是方法，建立 BELONGS_TO 关系 (Method -> Class)
        if self.current_class:
            self.relations.append({
                "src": full_name,
                "rel": "BELONGS_TO",
                "dst": self.current_class
            })

        prev_func = self.current_function
        self.current_function = full_name
        self.generic_visit(node)
        self.current_function = prev_func

    def visit_Call(self, node):
        if not self.current_function:
            return

        target_name = None
        
        # Case 1: 普通函数调用 func()
        if isinstance(node.func, ast.Name):
            target_name = node.func.id
            
        # Case 2: 方法调用 self.method()
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                # self.method() -> Class.method
                if node.func.value.id == 'self' and self.current_class:
                     target_name = f"{self.current_class}.{node.func.attr}"
                # obj.method() -> 暂存 method 名
                else:
                     target_name = node.func.attr

        if target_name:
            self.relations.append({
                "src": self.current_function,
                "rel": "CALLS",
                "dst": target_name
            })
        
        self.generic_visit(node)

class GraphLoader:
    def __init__(self, uri, user, pwd):
        self.driver = GraphDatabase.driver(uri, auth=(user, pwd))

    def close(self):
        self.driver.close()

    def clean_db(self):
        """清空数据库 (开发测试用)"""
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")

    def load_data(self, entities, relations):
        with self.driver.session() as session:
            # 1. 创建节点 (支持 Function, Class, Method)
            for entity in entities:
                # 动态设置 Label: :Function 或 :Class
                label = entity['type'] 
                session.run(
                    f"""
                    MERGE (e:{label} {{name: $name}})
                    ON CREATE SET e.filepath = $filepath, e.lineno = $lineno
                    ON MATCH SET e.filepath = $filepath, e.lineno = $lineno
                    """,
                    name=entity['name'], filepath=entity['filepath'], lineno=entity['lineno']
                )
            
            # 2. 创建关系
            for rel in relations:
                # 注意：这里我们匹配所有 Label 的节点，只要名字对上就行
                session.run(
                    """
                    MATCH (src {name: $src})
                    MATCH (dst {name: $dst})
                    MERGE (src)-[r:REL_TYPE]->(dst)
                    """,
                    src=rel['src'], 
                    dst=rel['dst']
                ).consume() 
                # 注意：上面 Cypher 里的 REL_TYPE 是占位符，neo4j python driver 不支持参数化 Relationship Type
                # 所以我们用 Python 字符串替换来动态处理关系类型
                
                query = f"""
                    MATCH (src {{name: $src}})
                    MATCH (dst {{name: $dst}})
                    MERGE (src)-[:{rel['rel']}]->(dst)
                """
                session.run(query, src=rel['src'], dst=rel['dst'])

# ==========================================
# 🚨 这一部分就是你之前缺失的入口函数
# ==========================================
def build_graph_from_file(filepath, uri, user, pwd):
    """
    Main entry point used by app.py
    """
    # 1. AST 解析
    analyzer = CodeAnalyzer(filepath)
    with open(filepath, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
        analyzer.visit(tree)
    
    # 2. Neo4j 入库
    loader = GraphLoader(uri, user, pwd)
    # 注意：为了演示方便，每次构建都清空图库。
    # 如果想保留历史数据，请注释掉 loader.clean_db()
    loader.clean_db() 
    loader.load_data(analyzer.entities, analyzer.relations)
    loader.close()
    
    return len(analyzer.entities), len(analyzer.relations)

def build_graph_for_batch(file_paths, uri, user, pwd):
    """
    一次性处理多个文件：
    1. 先清空数据库 (只清一次)
    2. 循环解析每个文件
    3. 循环存入数据库
    """
    loader = GraphLoader(uri, user, pwd)
    
    # 1. 只有开始时清空一次数据库！
    print("🧹 Cleaning Database for batch import...")
    loader.clean_db() 
    
    total_nodes = 0
    total_rels = 0
    
    # 2. 循环处理每个文件
    for filepath in file_paths:
        try:
            print(f"Analyzing: {filepath}")
            analyzer = CodeAnalyzer(filepath)
            with open(filepath, "r", encoding="utf-8") as f:
                source = f.read()
                if not source.strip(): continue # 跳过空文件
                tree = ast.parse(source)
                analyzer.visit(tree)
            
            # 3. 追加写入数据 (不要再 clean 了)
            loader.load_data(analyzer.entities, analyzer.relations)
            
            total_nodes += len(analyzer.entities)
            total_rels += len(analyzer.relations)
            
        except Exception as e:
            print(f"⚠️ Error parsing {filepath}: {e}")
            continue # 遇到错误跳过当前文件，继续下一个
            
    loader.close()
    return total_nodes, total_rels
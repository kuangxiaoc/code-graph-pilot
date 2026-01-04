import ast
import os
from neo4j import GraphDatabase

class CodeAnalyzer(ast.NodeVisitor):
    def __init__(self, filepath):
        self.filepath = filepath
        self.entities = []
        self.relations = []
        self.current_scope = None # 追踪当前在哪个函数/类里

    def visit_FunctionDef(self, node):
        func_name = node.name
        # 记录函数节点
        self.entities.append({
            "type": "Function",
            "name": func_name,
            "filepath": self.filepath,
            "lineno": node.lineno
        })
        
        # 记录作用域并继续遍历
        parent = self.current_scope
        self.current_scope = func_name
        self.generic_visit(node)
        self.current_scope = parent

    def visit_Call(self, node):
        # 简单的调用关系提取
        if isinstance(node.func, ast.Name) and self.current_scope:
            called_func = node.func.id
            self.relations.append({
                "src": self.current_scope,
                "rel": "CALLS",
                "dst": called_func
            })
        self.generic_visit(node)

class GraphLoader:
    def __init__(self, uri, user, pwd):
        self.driver = GraphDatabase.driver(uri, auth=(user, pwd))

    def close(self):
        self.driver.close()

    def clean_db(self):
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")

    def load_data(self, entities, relations):
        with self.driver.session() as session:
            # 1. 创建 Function 节点
            for entity in entities:
                if entity['type'] == 'Function':
                    session.run(
                        """
                        MERGE (f:Function {name: $name})
                        ON CREATE SET f.filepath = $filepath, f.lineno = $lineno
                        ON MATCH SET f.filepath = $filepath, f.lineno = $lineno
                        """,
                        name=entity['name'], filepath=entity['filepath'], lineno=entity['lineno']
                    )
            
            # 2. 创建 CALLS 关系
            for rel in relations:
                session.run(
                    """
                    MATCH (src:Function {name: $src})
                    MATCH (dst:Function {name: $dst})
                    MERGE (src)-[:CALLS]->(dst)
                    """,
                    src=rel['src'], dst=rel['dst']
                )

def build_graph_from_file(filepath, uri, user, pwd):
    analyzer = CodeAnalyzer(filepath)
    with open(filepath, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
        analyzer.visit(tree)
    
    loader = GraphLoader(uri, user, pwd)
    # 演示用：每次重建前清空库，实际项目可以改造成增量更新
    loader.clean_db() 
    loader.load_data(analyzer.entities, analyzer.relations)
    loader.close()
    return len(analyzer.entities), len(analyzer.relations)
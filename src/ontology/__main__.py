"""本体层命令行入口。

用法:
    uv run python -m ontology validate --fixture webapp_py   # 校验 fixture 图
    uv run python -m ontology schema                          # 打印本体
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_fixture_graph(fixture):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))
    from benchmarks.runner import build_graph

    fixture_dir = ROOT / "benchmarks" / "fixtures" / fixture
    return build_graph(fixture_dir)


def cmd_validate(args):
    from .export import graph_to_rdf
    from .schema import build_schema
    from .validate import validate_graph

    graph = _load_fixture_graph(args.fixture)
    rdf = graph_to_rdf(
        {"entities": graph["entities"], "relations": graph["relations"]},
        schema=build_schema(),
    )
    report = validate_graph(rdf, entities=graph["entities"])

    print(f"fixture: {args.fixture}")
    print(f"triples: {len(rdf)}  violations: {report.count}")
    print()
    print(report.describe())

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))

    # 违规非零即非零退出码, 可接 CI
    return 0 if report.count == 0 else 1


def cmd_schema(args):
    from .schema import build_schema

    print(build_schema().serialize(format="turtle"))
    return 0


def main():
    parser = argparse.ArgumentParser(prog="ontology", description="本体约束校验")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_validate = sub.add_parser("validate", help="校验 fixture 图的约束")
    p_validate.add_argument("--fixture", default="webapp_py")
    p_validate.add_argument("--json", action="store_true")
    p_validate.set_defaults(func=cmd_validate)

    p_schema = sub.add_parser("schema", help="打印本体 (turtle)")
    p_schema.set_defaults(func=cmd_schema)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

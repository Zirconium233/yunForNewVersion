"""离线路径生成入口：不读取 config.ini，不导入 main 或网络模块。"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yun_route


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='路线 JSON 配置路径')
    parser.add_argument('--output', required=True, help='GeoJSON 输出；另存同名 .task.json 供检查')
    args = parser.parse_args()
    task = yun_route.generate(args.config)
    output = Path(args.output).resolve()
    task_output = output.with_suffix('.task.json')
    if output == task_output or output.exists() or task_output.exists():
        parser.error('输出路径冲突或已存在，请选择新的文件名')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(yun_route.geojson(task), ensure_ascii=False, indent=2), encoding='utf-8')
    task_output.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding='utf-8')
    data = task['data']
    print(f"{len(data['pointsList'])} 点 / {data['recordMileage']:.4f} km / {data['duration']} 秒")
    print(f"几何：{output}\n明细：{task_output}\n仅完成离线生成，未验证围栏、成绩或服务端接受情况。")


if __name__ == '__main__':
    main()

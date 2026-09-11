# -*- coding: utf-8 -*-
"""历史记录导出（A 阶段改造）。

评审 2.1.4 / §3-A.3 落地：
- 不再 `from main import *` 蹭全局；改为显式加载配置并注入 YunClient。
- crsReocordInfo 的 SM4+gzip 双层解码收敛到 yun_http.decode_response 一处，
  删除本文件的 key_ctx + 二次 gzip/decrypt 逻辑（这是重构，不是新功能）。
- HTTP 状态错误 / 解码错误 / 业务错误分别报告。
"""
import argparse
import json
import os

import main as _main
from yun_http import (
    BusinessException,
    DecodeException,
    HttpStatusException,
    YunClient,
    mask_secret,
)


def build_arg_parser():
    parser = argparse.ArgumentParser(description='云运动历史记录提取工具')
    # 旧参数名保持：--history_path / --conf_path；缺省改为按“缺省资源=项目根”契约解析
    parser.add_argument('--history_path', type=str, default=None,
                        help='历史记录保存路径（相对路径按调用者工作目录解析；缺省为项目根 tasks_else）')
    parser.add_argument("--conf_path", type=str, default=None,
                        help="配置文件路径（相对路径按调用者工作目录解析；缺省为项目根 config.ini）")
    return parser


def print_header():
    print("=" * 50)
    print("云运动历史记录提取工具")
    print("=" * 50)
    print("该工具将获取您的历史跑步记录，并保存为任务文件\n")


def select_option(options, title="请选择一个选项", show_indexes=True, per_page=10):
    if not options:
        print("没有可选项")
        return None

    total_pages = (len(options) + per_page - 1) // per_page
    current_page = 1

    while True:
        print(f"\n{title} (第{current_page}/{total_pages}页):")
        start_idx = (current_page - 1) * per_page
        end_idx = min(start_idx + per_page, len(options))

        for i in range(start_idx, end_idx):
            prefix = f"[{i+1}] " if show_indexes else ""
            print(f"{prefix}{options[i]}")

        if total_pages > 1:
            print("\n导航: [n]下一页 [p]上一页", end="")

        print(" [q]退出")

        choice = input("请输入选项编号: ").strip().lower()

        if choice == 'q':
            return None
        elif choice == 'n' and current_page < total_pages:
            current_page += 1
            continue
        elif choice == 'p' and current_page > 1:
            current_page -= 1
            continue

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return idx
            else:
                print("无效的选择，请重试")
        except ValueError:
            print("请输入有效的数字")


def save_history_record(text, history_path):
    """保存历史记录到指定路径"""
    os.makedirs(history_path, exist_ok=True)

    files = os.listdir(history_path)
    last = 0

    # 找出最后一个文件编号
    for file in files:
        if file.startswith("tasklist_") and file.endswith(".json"):
            try:
                num = int(file.replace("tasklist_", "").replace(".json", ""))
                last = max(last, num + 1)
            except ValueError:
                pass

    target = os.path.join(history_path, f"tasklist_{last}.json")

    with open(file=target, mode='w+', encoding="utf-8") as f:
        print(f"保存记录到: {target}")
        f.write(text)
        f.flush()

    return target


def format_run_info(run):
    """格式化跑步信息为可读字符串"""
    end_time = run.get("endTime", "")
    record_mileage = run.get("recordMileage", "0")
    # recode_pace = run.get("recodePace", "0") # server这里有bug，返回的是0
    # recode_cadence = run.get("recodeCadence", "0")
    # duration = run.get("duration", "0")

    return f"{end_time} | {record_mileage}公里"


def his(client: YunClient, history_path: str):
    """交互式导出。client 由调用方显式注入（构造函数不联网）。"""
    print_header()

    # 确认信息（A.4：默认脱敏）
    profile = client.profile
    print("\n请确认以下信息:")
    print(("Token: ").ljust(15) + mask_secret(profile.token))
    print(("设备ID: ").ljust(15) + profile.device_id)
    print(("设备名称: ").ljust(15) + profile.device_name)
    print(("UUID: ").ljust(15) + (mask_secret(profile.uuid) if profile.uuid else "<每请求随机>"))

    confirm = input("\n信息是否正确? [y/n]: ")
    if confirm.lower() != 'y':
        print("已取消操作")
        return

    # 获取学期列表
    print("\n正在获取学期列表...")
    try:
        term_list = client.post_json("/run/listXnYearXqByStudentId", "")
        if term_list.get("code") != 200:
            print(f"获取学期列表失败: {term_list.get('msg', '未知错误')}")
            return
        terms = term_list.get("data", [])
        if not terms:
            print("没有找到任何学期数据")
            return
    except (HttpStatusException, DecodeException, BusinessException) as e:
        print(f"获取学期列表失败: {e}")
        return

    term_options = [f"{term['key']} ({term['sjd']})" for term in terms]
    term_idx = select_option(term_options, title="请选择学期")
    if term_idx is None:
        print("已取消操作")
        return

    selected_term = terms[term_idx]
    print(f"\n已选择: {selected_term['key']}")

    # 获取选定学期的跑步记录列表
    print("\n正在获取跑步记录...")
    try:
        run_list = client.post_json("/run/crsReocordInfoList",
                                    json.dumps({"tableName": selected_term['value']}))
        if run_list.get("code") != 200:
            print(f"获取跑步记录失败: {run_list.get('msg', '未知错误')}")
            return
        all_runs = []
        for month_data in run_list.get("data", {}).get("rank", []):
            all_runs.extend(month_data.get("rankList", []))
        if not all_runs:
            print(f"在{selected_term['key']}没有找到任何跑步记录")
            return
    except (HttpStatusException, DecodeException, BusinessException) as e:
        print(f"获取跑步记录失败: {e}")
        return

    run_options = [format_run_info(run) for run in all_runs]
    run_idx = select_option(run_options, title="请选择一条跑步记录")
    if run_idx is None:
        print("已取消操作")
        return

    selected_run = all_runs[run_idx]
    print(f"\n已选择: {run_options[run_idx]}")

    # 获取详细跑步记录：decode_response 统一处理 明文/SM4/SM4+gzip，无二次解码
    print("\n正在获取详细记录...")
    try:
        text = client.post("/run/crsReocordInfo",
                           json.dumps({"id": selected_run['id'],
                                       "tableName": selected_term['value']}))
        run_detail = json.loads(text)
        if run_detail.get("code") != 200:
            print(f"获取详细记录失败: {run_detail.get('msg', '未知错误')}")
            return
    except (HttpStatusException, DecodeException, BusinessException) as e:
        print(f"获取或解析详细记录失败: {e}")
        return

    # 保存记录
    try:
        saved_path = save_history_record(text, history_path)
        print(f"\n记录已成功保存到: {saved_path}")

        data = run_detail.get("data", {})
        print("\n记录摘要:")
        print(f"日期时间: {data.get('recordStartTime', '')} ~ {data.get('recordEndTime', '')}")
        print(f"跑步距离: {data.get('recordMileage', '0')}公里")
        print(f"配速: {data.get('recodePace', '0')}/公里")
        print(f"步频: {data.get('recodeCadence', '0')}")
        print(f"用时: {data.get('duration', '0')}秒")
        print(f"打卡点数: {data.get('recodeDislikes', '0')}")
        print(f"运动轨迹点: {len(data.get('pointsList', []))}个")
    except OSError as e:
        print(f"保存记录失败: {e}")
        return


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    conf_path = (_main.resolve_cli_path(args.conf_path) if args.conf_path
                 else _main.project_resource("config.ini"))
    history_path = (_main.resolve_cli_path(args.history_path) if args.history_path
                    else _main.project_resource("tasks_else"))
    _main.set_args(conf_path)
    his(_main.default_client(), history_path)

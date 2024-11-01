import json
import random

# 从文件读取数据
with open('../std_path.json', 'r', encoding='utf8') as f:
    std_path = json.load(f)

# 是否使用随机起点
rd_start = input("是否使用随机起点 (Y/n): ").lower() != 'n'
# 里程数（km）
mileage = float(input("请输入里程数 (km): "))
# 平均配速（min/km）
avg_pace = float(input("请输入平均配速 (min/km): "))
# 平均步频（步/分钟）
avg_cadence = float(input("请输入平均步频 (步/分钟): "))

dislike = random.randint(2, 5)
duration = mileage * avg_pace * 60
avg_speed = mileage * 1000 / duration

# 模拟数据
# 暂时没看懂dislike怎么用的，直接写死成5了
result = {
    "msg": "操作成功",
    "code": 200,
    "data": {
        "recordMileage": round(mileage, 2),
        "recodePace": round(avg_pace, 2),
        "recodeCadence": round(avg_cadence),
        "recodeDislikes": 5,
        "duration": round(duration),
        "pointsList": [],
        "schoolId": 100,
        "manageList": [
            {"point": "117.206317,31.773892", "marked": "Y", "index": 0},
            {"point": "117.208436,31.773412", "marked": "Y", "index": 1},
            {"point": "117.205992,31.773411", "marked": "Y", "index": 2},
            {"point": "117.20632,31.774412", "marked": "Y", "index": 3},
            {"point": "117.208752,31.773909", "marked": "Y", "index": 4}
        ]
    }
}

def blur(data, radio):
    return data + (random.random() - 0.5) * radio * data

# 若 rd_start 为 true，则随机生成起点，否则从第一个点开始
path_index = random.randint(0, len(std_path) - 1) if rd_start else 0

# 初始数据
speed = blur(avg_speed, 0.2)
run_time = random.random() * 10
run_step = blur(avg_cadence, 0.06) * run_time / 60
run_mileage = avg_speed * run_time * (random.random() * 0.1 + 0.8)

tmp_duration = random.random() * 0.4 + 1.3

# 路径点生成
while run_time < duration:
    # 生成数据
    result['data']['pointsList'].append({
        "id": 0,
        "point": f"{std_path[path_index][0]},{std_path[path_index][1]}",
        "speed": round(speed, 2),
        "runStatus": 1,
        "runRecordId": 0,
        "runTime": str(int(run_time)),
        "isFence": "Y",
        "runStep": str(int(run_step)),
        "runMileage": str(run_mileage),
    })
    # 走步模拟
    d_mileage = blur(speed * tmp_duration, 0.05)
    run_mileage += d_mileage
    # 步数模拟
    run_step += blur(avg_cadence, 0.06) * tmp_duration / 60
    # 速度模拟
    speed = blur(avg_speed, 0.2)
    # 耗时模拟
    tmp_duration = random.random() * 0.4 + 1.3
    # 点位模拟
    path_index += int(d_mileage / 400 * len(std_path))
    path_index %= len(std_path)
    # 累加时间
    run_time += tmp_duration

# 随机写入tasklist_XXXX.json中
file_name = f'tasklist_{random.randint(1000, 9999)}.json'
with open('../tasks_fch/' + file_name, 'w', encoding='utf8') as f:
    json.dump(result, f, ensure_ascii=False, indent=4)

print(f"生成数据已保存至 {file_name}")
import numpy as np
import matplotlib.pyplot as plt
import pyproj
import json
from scipy.interpolate import UnivariateSpline

# 定义投影，将经纬度转换为平面坐标系
wgs84 = pyproj.CRS('EPSG:4326')
utm = pyproj.CRS('EPSG:32651')
project = pyproj.Transformer.from_crs(wgs84, utm, always_xy=True)

# 所有路径的平面坐标
all_paths = []

# 汇总所有坐标点用于计算中心点
all_x = []
all_y = []

# 读取所有tasklist中的路径点
for i in range(9):
    # 提取经纬度
    lons, lats = [], []
    with open(f'../tasks_fch/tasklist_{i}.json', 'r', encoding='utf-8') as file:
        data = json.load(file)
    for point in data['data']['pointsList']:
        lon, lat = map(float, point['point'].split(','))
        lons.append(lon)
        lats.append(lat)
    # 转换为平面坐标
    x, y = project.transform(lons, lats)
    coords = np.vstack((x, y)).T
    all_paths.append(coords)
    all_x.extend(x)
    all_y.extend(y)

# 计算跑道中心点
center_x = np.mean(all_x)
center_y = np.mean(all_y)

# 将每条路径转换为极坐标并展开角度
all_polar_paths = []

for coords in all_paths:
    dx = coords[:,0] - center_x
    dy = coords[:,1] - center_y
    r = np.sqrt(dx**2 + dy**2)
    theta = np.arctan2(dy, dx)
    # 对角度进行展开
    theta_unwrapped = np.unwrap(theta)
    # 将角度归一化到 [0, 2pi)
    theta_normalized = np.mod(theta_unwrapped, 2*np.pi)
    # 存储归一化的极坐标
    polar_coords = np.vstack((theta_normalized, r)).T
    all_polar_paths.append(polar_coords)

# 定义统一的角度序列 [0, 2pi)
theta_uniform = np.linspace(0, 2*np.pi, 1000)

# 在统一角度上插值半径
interpolated_radii = []

for polar_coords in all_polar_paths:
    theta = polar_coords[:,0]
    r = polar_coords[:,1]
    # 对 theta 进行排序
    sorted_indices = np.argsort(theta)
    theta_sorted = theta[sorted_indices]
    r_sorted = r[sorted_indices]
    # 插值半径
    r_interp = np.interp(theta_uniform, theta_sorted, r_sorted, period=2*np.pi)
    interpolated_radii.append(r_interp)

# 对半径取平均
average_r = np.mean(interpolated_radii, axis=0)

# 对平均半径进行平滑处理
spline = UnivariateSpline(theta_uniform, average_r, s=5)
average_r_smooth = spline(theta_uniform)

# 将平均的极坐标转换回平面坐标
avg_dx = average_r_smooth * np.cos(theta_uniform)
avg_dy = average_r_smooth * np.sin(theta_uniform)
avg_x = avg_dx + center_x
avg_y = avg_dy + center_y

# 将平均路径转换回经纬度
inverse_project = pyproj.Transformer.from_crs(utm, wgs84, always_xy=True)
avg_lons, avg_lats = inverse_project.transform(avg_x, avg_y)

# 绘制结果
plt.figure(figsize=(10, 8))
for coords in all_paths:
    plt.plot(coords[:,0], coords[:,1], color='gray', alpha=0.5)
plt.plot(avg_x, avg_y, color='red', linewidth=2)
plt.legend()
plt.axis('equal')
plt.grid(True)
plt.show()

# 将标准路径保存为列表
average_path_points = list(zip(avg_lons, avg_lats))

# 将标准路径保存为JSON文件
with open('../std_path.json', 'w', encoding='utf-8') as f:
    json.dump(average_path_points, f, ensure_ascii=False, indent=4)
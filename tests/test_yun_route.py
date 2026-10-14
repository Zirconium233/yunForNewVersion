import copy
import json
from pathlib import Path

import pytest

import main as M
import yun_route as R
import yun_http as H
from test_main_phase_a import CFG, _home, _responder

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'tasks_fch/tasklist_0.json'        # 任意来源皆可：2.2km 任务表
DEMO_SOURCE = ROOT/'tasks_fch/tasklist_1.json'   # 历史演示包基准来源


def config(tmp_path, **updates):
    # 默认关掉三个抽签（偏移 0 + 精确里程），让流程用例可复现；
    # 抽签行为由 test_rhythm_offsets_* 专门覆盖。
    cfg = {'source_json': str(SOURCE), 'coordinate_system': 'GCJ-02', 'distance_m': 1000,
           'pace_min_km': 6.0, 'cadence_spm': 160, 'sample_seconds': 1, 'seed': 31337,
           'pace_offset_pct': 0, 'cadence_offset_pct': 0, 'length_offset_pct': 0}
    cfg.update(updates)
    path = tmp_path/'route.json'
    path.write_text(json.dumps(cfg), encoding='utf-8')
    return path


def source_dict():
    return json.loads(SOURCE.read_text(encoding='utf-8-sig'))


def test_deform_task_consistency(tmp_path):
    task = R.generate(config(tmp_path))
    meta = task['metadata']
    assert meta['engine'] == 'deform'
    assert meta['coordinate_system'] == 'GCJ-02'
    assert meta['seed'] == 31337
    assert meta['source'] == SOURCE.name
    data = task['data']
    pts = data['pointsList']
    assert data['duration'] == 360
    assert len(pts) == 361
    assert data['recordMileage'] == pytest.approx(1.0)
    # 等时间间隔 + 等里程增量：逐点配速恒等于总体配速，间隔与里程/配速自洽。
    gaps_m = [b['runMileage']-a['runMileage'] for a, b in zip(pts, pts[1:])]
    gaps_t = [b['runTime']-a['runTime'] for a, b in zip(pts, pts[1:])]
    assert max(gaps_m)-min(gaps_m) < 1e-6
    assert set(gaps_t) == {1}
    assert ({p['speed'] for p in pts[1:]}) == {f"{data['recodePace']:.2f}"}
    assert data['recodePace'] == pytest.approx(data['duration']/60/data['recordMileage'])
    assert gaps_m[0] == pytest.approx(meta['step_m'], rel=1e-5)
    assert meta['step_m'] == pytest.approx(1e3/(data['recodePace']*60)*meta['interval_s'], rel=1e-4)
    for a, b in zip(pts, pts[1:]):
        assert b['runTime'] > a['runTime']
        assert b['runMileage'] > a['runMileage']
        assert b['runStep'] >= a['runStep']
    assert data['recodeCadence'] == pytest.approx(meta['cadence_spm'], rel=0.01)
    assert R.generate(config(tmp_path)) == task
    assert R.generate(config(tmp_path, seed=123))['data']['pointsList'] != pts


def test_deform_matches_demo_scale():
    """历史演示包复现基准（GPT 复现包 verify_demo 的 fch 行）：686 点 / 3.877km。

    这条断言把"演示包视觉尺度"钉死在回归里：改动 DEMO_PROFILE 默认值或形变算法
    都会让它失败，必须是有意为之。
    """
    out = R.deform_geometry(R.load_points(DEMO_SOURCE), seed=31337)
    gaps = [R.distance(a, b) for a, b in zip(out, out[1:])]
    assert len(out) == 686
    assert sum(gaps)/1000 == pytest.approx(3.877, abs=0.01)
    assert sum(gaps)/len(gaps) == pytest.approx(5.66, abs=0.3)   # 5.5m 等弧长重采样


@pytest.mark.parametrize('updates', [
    {'distance_m': 50000}, {'pace_min_km': float('nan')}, {'cadence_spm': True},
    {'sample_seconds': 0}, {'coordinate_system': 'guess'}, {'unexpected_option': 1},
    {'seed': 2.5}, {'source_json': None}, {'allowed_polygon_geojson': 123},
    {'deform_profile': 'x'}, {'deform_profile': {'spacing_m': 0}},
    {'deform_profile': {'nope': 1}}, {'pace_offset_pct': 0.9}, {'length_offset_pct': -1},
    {'min_distance_m': 3000, 'max_distance_m': 2000}, {'distance_m': 5000, 'max_distance_m': 4000},
])
def test_invalid_config_fails(tmp_path, updates):
    with pytest.raises(ValueError):
        R.generate(config(tmp_path, **updates))


def test_rhythm_offsets_within_ten_percent_and_consistent(tmp_path):
    """三个参数各自 ±10% 抽签，且每点配速/间隔与总体值自洽（不是逐点乱偏）。"""
    _, metrics = R.load_source(SOURCE)
    lengths = set()
    for seed in (31337, 31338, 31339):
        cfg = {'source_json': str(SOURCE), 'coordinate_system': 'GCJ-02', 'sample_seconds': 2, 'seed': seed}
        task = R.generate_cfg(cfg, Path('.'))
        meta, data = task['metadata'], task['data']
        pts = data['pointsList']
        lengths.add(round(data['recordMileage'], 3))
        assert abs(data['recordMileage']*1000/metrics['length_m']-1) <= 0.10
        assert abs(meta['pace_min_km']/metrics['pace_min_km']-1) <= 0.10
        assert abs(meta['cadence_spm']/meta['source_cadence_spm']-1) <= 0.10 if meta['source_cadence_spm'] else True
        gaps_m = [b['runMileage']-a['runMileage'] for a, b in zip(pts, pts[1:])]
        gaps_t = {b['runTime']-a['runTime'] for a, b in zip(pts, pts[1:])}
        assert max(gaps_m)-min(gaps_m) < 1e-6 and gaps_t == {2}
        assert {p['speed'] for p in pts[1:]} == {f"{data['recodePace']:.2f}"}
        assert gaps_m[0] == pytest.approx(meta['step_m'], rel=1e-5)   # 间隔 ↔ 里程 ↔ 配速 三向自洽
        assert meta['interval_s']*1e3/(data['recodePace']*60) == pytest.approx(meta['step_m'], rel=1e-4)
        assert 1 <= meta['attempt'] <= R.MAX_ATTEMPTS
    assert len(lengths) > 1, '总里程应当随种子在 ±10% 内变化'


def test_distance_window_retry_then_failure(tmp_path):
    """里程区间：区间内抽中即用；抽不中换种子重抽，5 次失败后如实报错。"""
    _, metrics = R.load_source(SOURCE)
    base = metrics['length_m']
    cfg = {'source_json': str(SOURCE), 'coordinate_system': 'GCJ-02', 'sample_seconds': 2,
           'seed': 31337, 'min_distance_m': base*0.8, 'max_distance_m': base*1.2}
    task = R.generate_cfg(cfg, Path('.'))
    assert 1 <= task['metadata']['attempt'] <= R.MAX_ATTEMPTS
    assert base*0.8 <= task['data']['recordMileage']*1000 <= base*1.2
    with pytest.raises(ValueError, match='重抽'):
        R.generate_cfg(dict(cfg, min_distance_m=base*3, max_distance_m=base*3.1), Path('.'))
    with pytest.raises(ValueError, match='不在给定里程区间'):
        R.generate_cfg(dict(cfg, distance_m=100, min_distance_m=base*0.9,
                            max_distance_m=base*1.1), Path('.'))


def test_source_cadence_gate_ignores_implausible_field():
    """fch 表的 runStep 约 489 spm（来源字段异常）→ 不采信，退回名义步频。"""
    _, fch = R.load_source(ROOT/'tasks_fch/tasklist_1.json')
    _, xc = R.load_source(ROOT/'tasks_xc/tasklist_4.json')
    assert fch['cadence_spm'] is None and fch['pace_min_km'] is not None
    assert 110 <= xc['cadence_spm'] <= 220
    task = R.generate_cfg({'source_json': str(ROOT/'tasks_fch/tasklist_1.json'),
                           'coordinate_system': 'GCJ-02', 'cadence_spm': 160,
                           'distance_m': 1500, 'seed': 5}, Path('.'))
    meta = task['metadata']
    assert meta['source_cadence_spm'] is None
    assert abs(meta['cadence_spm']/160-1) <= 0.10


@pytest.mark.parametrize('legacy', [
    {'base_geojson': 'base.geojson'}, {'base_task': 'task.json'}, {'max_offset_m': 10},
    {'lane_change_indices': [90]}, {'detour_enabled': True}, {'telemetry_task': 't.json'},
    {'start_trim': 10}, {'mode': 'deform'},
])
def test_legacy_v4_keys_rejected(tmp_path, legacy):
    """V4 已移除：旧 cfg 必须得到明确的迁移报错，而不是含糊的未知键。"""
    with pytest.raises(ValueError, match='V4 合成参数已移除'):
        R.generate(config(tmp_path, **legacy))


def test_source_formats_all_accepted(tmp_path):
    """tasklist / GeoJSON / 裸坐标列表三种来源等价（同一批点 -> 同一结果）。"""
    rows = source_dict()['data']['pointsList']
    coords = [[float(x) for x in r['point'].split(',')] for r in rows]
    (tmp_path/'as_task.json').write_text(json.dumps(source_dict()), encoding='utf-8')
    (tmp_path/'as_geo.json').write_text(json.dumps({
        'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {},
         'geometry': {'type': 'LineString', 'coordinates': coords}}]}), encoding='utf-8')
    (tmp_path/'as_list.json').write_text(json.dumps(coords), encoding='utf-8')
    tasks = [R.generate(config(tmp_path, source_json=name))
             for name in ('as_task.json', 'as_geo.json', 'as_list.json')]
    assert all(t['data']['pointsList'] == tasks[0]['data']['pointsList'] for t in tasks)
    assert tasks[0]['metadata']['source'] == 'as_task.json'
    assert tasks[0]['data']['recordMileage'] == pytest.approx(1, rel=0.05)


def test_polygon_is_optional_but_validated(tmp_path):
    route = R.deform_geometry(R.load_points(SOURCE), seed=31337)
    lons = [p[0] for p in route]
    lats = [p[1] for p in route]
    assert R.generate(config(tmp_path))['data']['recordMileage'] > 0      # 不给围栏：允许
    tiny = {'type': 'Polygon', 'coordinates': [[
        [lons[0]-0.00005, lats[0]-0.00005], [lons[0]+0.00005, lats[0]-0.00005],
        [lons[0]+0.00005, lats[0]+0.00005], [lons[0]-0.00005, lats[0]+0.00005],
        [lons[0]-0.00005, lats[0]-0.00005]]]}
    (tmp_path/'tiny.geojson').write_text(json.dumps(tiny), encoding='utf-8')
    with pytest.raises(ValueError, match='超出'):
        R.generate(config(tmp_path, allowed_polygon_geojson='tiny.geojson'))
    broad = {'type': 'Polygon', 'coordinates': [[
        [min(lons)-0.001, min(lats)-0.001], [max(lons)+0.001, min(lats)-0.001],
        [max(lons)+0.001, max(lats)+0.001], [min(lons)-0.001, max(lats)+0.001],
        [min(lons)-0.001, min(lats)-0.001]]]}
    (tmp_path/'broad.geojson').write_text(json.dumps(broad), encoding='utf-8')
    task = R.generate(config(tmp_path, allowed_polygon_geojson='broad.geojson'))
    assert task['data']['recordMileage'] > 0.9


def client_and_run(responder=_responder, home=None):
    M.set_args(CFG)
    clock = [0.0]
    fake = H.FakeTransport(responder, sm2box=H.SM2Box(M.PUBLIC_KEY, M.PRIVATE_KEY))
    client = H.YunClient(M.build_profile(M._CONF), base_url=M.my_host, transport=fake,
                         sleep=lambda s: clock.__setitem__(0, clock[0]+s),
                         now=lambda: 1700000000+clock[0], mono=lambda: clock[0])
    run = M.Yun_For_New(client=client, home_info=home or _home(
        raDislikes=0, raSingleMileageMin=0.0, raSingleMileageMax=10.0))
    run.raSingleMileageMin, run.raSingleMileageMax = 0, 10
    run.raCadenceMin, run.raCadenceMax = 1, 350
    return clock, fake, run


def test_generated_end_chain_and_virtual_clock(tmp_path):
    task = R.generate(config(tmp_path, distance_m=100))
    baseline = copy.deepcopy(task)
    clock, fake, run = client_and_run()
    run.validate_generated_route(task)
    run.start()
    run.do_generated_route(task)
    run.finish_by_points_map()
    routers = [c['router'] for c in fake.calls]
    assert routers[-3:] == ['/run/isStandard', '/run/splitPointCheating', '/run/finish']
    assert clock[0] == task['data']['duration']
    assert task == baseline
    pts = run.task_map['data']['pointsList']
    assert int(pts[-1]['ts'])-int(pts[0]['ts']) == run.task_map['data']['duration']
    assert run.task_map['data']['recodeCadence'] == pytest.approx(pts[-1]['runStep']*60/pts[-1]['runTime'])


def test_processing_time_included_without_catchup(tmp_path):
    task = R.generate(config(tmp_path, distance_m=100))
    clock, fake, run = client_and_run()
    run.start()
    original = run.split_by_points_map
    def slow(points):
        original(points)
        clock[0] += 2
    run.split_by_points_map = slow
    run.do_generated_route(task)
    data = run.task_map['data']
    assert data['duration'] > task['data']['duration']
    for a, b in zip(data['pointsList'], data['pointsList'][1:]):
        assert b['runTime'] > a['runTime']
        assert float(b['speed']) == pytest.approx((b['runTime']-a['runTime'])/60/((b['runMileage']-a['runMileage'])/1000), abs=0.0051)


def test_client_speed_is_pace_not_velocity():
    assert R.client_speed(10, 3) == '5.00'
    assert R.client_speed(0, 1) == '0.0'
    assert R.client_speed(0.001, 1) == '900.00'


def test_school_constraints_before_start(tmp_path):
    task = R.generate(config(tmp_path))
    _, fake, run = client_and_run()
    run.raDislikes = 1
    with pytest.raises(ValueError, match='踩点'):
        run.validate_generated_route(task)
    assert fake.calls == []
    run.raDislikes = 0
    run.raSingleMileageMin = 3
    with pytest.raises(ValueError, match='里程'):
        run.validate_generated_route(task)
    assert fake.calls == []


def test_generation_error_precedes_account_read(tmp_path, monkeypatch):
    path = config(tmp_path, distance_m=50000)
    monkeypatch.setattr('sys.argv', ['main.py', '--route-config', str(path), '-a'])
    monkeypatch.setattr(M, 'set_args', lambda *a: pytest.fail('must not read account configuration'))
    with pytest.raises(ValueError, match='超过生成几何'):
        M.main()


def test_batch_rejection_stops_generated_playback(tmp_path):
    task = R.generate(config(tmp_path, distance_m=100))
    def rejected(router, envelope):
        if router.endswith('/run/splitPointCheating'):
            return {'code': 500, 'msg': 'rejected'}
        return _responder(router, envelope)
    clock, fake, run = client_and_run(rejected)
    run.start()
    with pytest.raises(H.BusinessException):
        run.do_generated_route(task)
    assert clock[0] < task['data']['duration']
    assert [c['router'] for c in fake.calls] == ['/run/start', '/run/splitPointCheating']


def test_face_failure_stops_before_later_samples(tmp_path):
    task = R.generate(config(tmp_path, distance_m=100))
    clock, fake, run = client_and_run()
    run.start()
    def stopped(meters):
        if meters > 30:
            raise M.FaceRunStopError('test face stop')
    run._face_on_mileage = stopped
    with pytest.raises(M.FaceRunStopError):
        run.do_generated_route(task)
    assert clock[0] < task['data']['duration']
    assert all(c['router'] not in ('/run/finish', '/run/isStandard') for c in fake.calls)


def test_exact_batch_has_no_extra_tail(tmp_path, monkeypatch):
    task = R.generate(config(tmp_path, distance_m=25, pace_min_km=6))
    clock, fake, run = client_and_run()
    monkeypatch.setattr(M, 'split_count', 10)
    assert len(task['data']['pointsList']) == 10
    run.start()
    run.do_generated_route(task)
    run.finish_by_points_map()
    assert [c['router'] for c in fake.calls] == [
        '/run/start', '/run/splitPointCheating', '/run/isStandard', '/run/finish']


def test_school_point_threshold_is_strict_and_under_ten_uses_sixty(tmp_path):
    task = R.generate(config(tmp_path, distance_m=200))
    _, fake, run = client_and_run(home=_home(raDislikes=0, passPointNum=9))
    assert run.upload_batch_size == 61
    sent = []
    original = run.split_by_points_map
    def collect(points):
        sent.append(len(points))
        return original(points)
    run.split_by_points_map = collect
    run.start()
    run.do_generated_route(task)
    assert sent == [61]
    run.finish_by_points_map()
    assert sent == [61, len(task['data']['pointsList'])-61]
    assert [c['router'] for c in fake.calls][-3:] == [
        '/run/isStandard', '/run/splitPointCheating', '/run/finish']


def test_school_mileage_cap_stops_before_extra_points(tmp_path):
    task = R.generate(config(tmp_path, distance_m=2100))
    _, fake, run = client_and_run(home=_home(
        raDislikes=0, raSingleMileageMin=1.5, raSingleMileageMax=2.0, passPointNum=10))
    run.raSingleMileageMin, run.raSingleMileageMax = 1.5, 2.0
    assert run.distance_cap_m == 2000
    assert run.upload_batch_size == 11
    run.validate_generated_route(task)
    run.start()
    run.do_generated_route(task)
    assert run.task_map['data']['recordMileage'] == 2.0
    assert run.task_map['data']['pointsList'][-1]['runMileage'] == 2000
    assert len(run.task_map['data']['pointsList']) < len(task['data']['pointsList'])
    run.finish_by_points_map()
    assert fake.calls[-1]['router'] == '/run/finish'
    assert sum(1 for c in fake.calls if c['router'] == '/run/isStandard') == 1


def test_map_point_timestamps_follow_run_time(tmp_path):
    task = R.generate(config(tmp_path, distance_m=100))
    # 打表入口只用所给字段，要求 ts 与逐点 runTime 同步。
    d = tmp_path/'tasks'
    d.mkdir()
    (d/'one.json').write_text(json.dumps(task), encoding='utf-8')
    clock, fake, run = client_and_run()
    recorded = []
    original = run.split_by_points_map
    def collect(points):
        recorded.extend(copy.deepcopy(points))
        return original(points)
    run.split_by_points_map = collect
    run.start()
    run.do_by_points_map(path=str(d), random_choose=True)
    run.finish_by_points_map()
    assert len(recorded) == len(task['data']['pointsList'])
    assert all(int(b['ts'])-int(a['ts']) == b['runTime']-a['runTime']
               for a, b in zip(recorded, recorded[1:]))
    assert run.task_map['data']['duration'] >= recorded[-1]['runTime']


def test_auto_seed_recorded_and_changes_geometry(tmp_path):
    path = config(tmp_path, seed='auto')
    a, b = R.generate(path), R.generate(path)
    assert a['metadata']['seed'] != b['metadata']['seed']
    assert a['data']['pointsList'] != b['data']['pointsList']


def test_deform_profile_override_changes_geometry_only_within_limits(tmp_path):
    base = R.generate(config(tmp_path))['data']['pointsList']
    bold = R.generate(config(tmp_path, deform_profile={'jump_sigma_m': 20.0,
                      'length_gain_pct': 0.06}))['data']['pointsList']
    assert bold != base
    assert R.generate(config(tmp_path, deform_profile={'spacing_m': 8.0}))['metadata'][
        'geometry_spacing_m'] == 8.0


@pytest.mark.parametrize('cap', [float('nan'), float('inf'), 0, -1])
def test_invalid_school_cap_rejected_before_start(cap):
    with pytest.raises(H.DecodeException, match='有限正数'):
        client_and_run(home=_home(raDislikes=0, raSingleMileageMax=cap))


def test_legacy_map_without_run_step_keeps_batch_and_finish_consistent(tmp_path):
    task = R.generate(config(tmp_path, distance_m=100))
    for point in task['data']['pointsList']:
        point.pop('runStep')
    directory = tmp_path/'tasks'
    directory.mkdir()
    (directory/'one.json').write_text(json.dumps(task), encoding='utf-8')
    _, fake, run = client_and_run()
    run.start()
    run.do_by_points_map(path=str(directory), random_choose=True)
    assert run.task_map['data']['pointsList'][-1]['runStep'] > 0
    assert run.task_map['data']['recodeCadence'] == pytest.approx(
        run.task_map['data']['pointsList'][-1]['runStep']*60/
        run.task_map['data']['duration'])
    run.finish_by_points_map()
    assert fake.calls[-1]['router'] == '/run/finish'


@pytest.mark.parametrize('field,value', [
    ('runMileage', -1), ('runTime', -1), ('runStep', -1),
])
def test_invalid_map_is_rejected_before_first_batch(tmp_path, field, value):
    task = R.generate(config(tmp_path, distance_m=100))
    task['data']['pointsList'][20][field] = value
    directory = tmp_path/'tasks'
    directory.mkdir()
    (directory/'one.json').write_text(json.dumps(task), encoding='utf-8')
    _, fake, run = client_and_run()
    run.start()
    with pytest.raises(ValueError, match='回退或非法'):
        run.do_by_points_map(path=str(directory), random_choose=True)
    assert [c['router'] for c in fake.calls] == ['/run/start']


def test_polygon_detects_narrow_concavity_between_samples():
    ring = [(117.000000, 30.999990), (117.000050, 30.999990),
            (117.000050, 31.000010), (117.000019, 31.000010),
            (117.000019, 30.999999), (117.000017, 30.999999),
            (117.000017, 31.000010), (117.000000, 31.000010)]
    with pytest.raises(ValueError, match='超出'):
        R.validate_polygon_route([(117.000010, 31.0),
                                  (117.000040, 31.0)], ring)
    R.validate_polygon_route([(117.000025, 31.0),
                              (117.000040, 31.0)], ring)

import copy
import json
from pathlib import Path

import pytest

import main as M
import yun_route as R
import yun_http as H
from test_main_phase_a import CFG, _home, _responder

EXAMPLE = Path(__file__).resolve().parents[1]/'examples/routes/v4.json'


def config(tmp_path, **updates):
    cfg = json.loads(EXAMPLE.read_text())
    cfg['base_geojson'] = str(EXAMPLE.parent/'base_v3.geojson')
    cfg.update(updates)
    path = tmp_path/'route.json'
    path.write_text(json.dumps(cfg), encoding='utf-8')
    return path


def test_geometry_and_telemetry_consistency(tmp_path):
    task = R.generate(config(tmp_path))
    data = task['data']
    pts = data['pointsList']
    assert data['duration'] == 756
    assert len(pts) == 757
    coords = [tuple(map(float, p['point'].split(','))) for p in pts]
    measured = sum(R.distance(a, b) for a, b in zip(coords, coords[1:]))
    assert measured == pytest.approx(data['recordMileage']*1000)
    assert measured == pytest.approx(2100, rel=0.01)
    assert pts[-1]['runMileage'] == pytest.approx(measured)
    assert data['recodePace'] == pytest.approx(data['duration']/60/data['recordMileage'])
    for a, b in zip(pts, pts[1:]):
        assert b['runTime'] > a['runTime']
        assert b['runMileage'] > a['runMileage']
        assert b['runStep'] >= a['runStep']
        assert float(b['speed']) == pytest.approx((b['runTime']-a['runTime'])/60/((b['runMileage']-a['runMileage'])/1000), abs=0.0051)
    assert R.generate(config(tmp_path)) == task
    assert R.generate(config(tmp_path, seed=123))['data']['pointsList'] != pts


@pytest.mark.parametrize('updates', [
    {'distance_m': 3000}, {'start_trim': 450}, {'end_trim': 460},
    {'pace_min_km': float('nan')}, {'cadence_spm': True}, {'sample_seconds': 0},
    {'coordinate_system': 'guess'}, {'lane_change_choices_m': []},
    {'detour_enabled': 'false'}, {'lane_change_indices': [2, 2]},
    {'unexpected_option': 1}, {'seed': 2.5},
])
def test_invalid_config_fails(tmp_path, updates):
    with pytest.raises(ValueError):
        R.generate(config(tmp_path, **updates))


def test_v4_geometry_matches_supplied_result(tmp_path):
    # 原 V4 固定种子/裁剪/绕行应能复现；终点修复允许额外保留原末点。
    cfg = json.loads(config(tmp_path, start_trim=27, end_trim=41, detour_enabled=True, lane_transition_points=1).read_text())
    base = json.loads((EXAMPLE.parent/'base_v3.geojson').read_text())['features'][0]['geometry']['coordinates']
    points = R.geometry(R.coordinates(base), cfg)
    assert points[0] == pytest.approx([117.20617893, 31.77463768], abs=1e-8)
    assert sum(R.distance(a, b) for a, b in zip(points, points[1:])) == pytest.approx(1878.045, abs=0.1)


def client_and_run(responder=_responder):
    M.set_args(CFG)
    clock = [0.0]
    fake = H.FakeTransport(responder, sm2box=H.SM2Box(M.PUBLIC_KEY, M.PRIVATE_KEY))
    client = H.YunClient(M.build_profile(M._CONF), base_url=M.my_host, transport=fake,
                         sleep=lambda s: clock.__setitem__(0, clock[0]+s),
                         now=lambda: 1700000000+clock[0], mono=lambda: clock[0])
    run = M.Yun_For_New(client=client, home_info=_home(raDislikes=0))
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
